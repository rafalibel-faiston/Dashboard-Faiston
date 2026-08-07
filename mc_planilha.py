"""
Extração de MC a partir da planilha .xlsx — feita pelo Ops, não pelo modelo.

## Por que existe

A primeira versão do fluxo tinha o Claude lendo a planilha e mandando os
números por JSON. Isso não se sustentou: `.xlsb` é binário, a leitura variava,
e — pior — o modelo não mandava os totais declarados na planilha, então a
conferência do Ops ficava desligada e número errado entrava sem ninguém ver.

Aqui o Ops lê o arquivo. Mesmo arquivo, mesmo resultado, sempre. E os totais
declarados são lidos da própria planilha, então a conferência passa a rodar
sozinha — que era o ponto todo.

Só `.xlsx`: é o que o `openpyxl` (já usado no Ops pelo Forecast) lê. `.xlsb`
não é lido por ele nem aceito pelo conector do Microsoft 365, então a conversão
para `.xlsx` é pré-requisito do fluxo.

## Como acha as coisas

Sem posição fixa de célula. O template de MC tem várias abas e versões desde
2021, então cravar "linha 12, coluna D" quebraria na primeira planilha
diferente. O parser procura a **linha de cabeçalho** por nome de coluna (mesma
estratégia que `_parse_forecast_workbook` usa no Forecast) e lê dali para
baixo até as linhas acabarem.

Quando não reconhece nada, devolve um **diagnóstico** com as abas vistas e os
cabeçalhos candidatos encontrados. É deliberado: calibrar contra uma planilha
real fica uma iteração, em vez de adivinhação.
"""

import io
import re
import unicodedata

# ── vocabulário de colunas ───────────────────────────────────────────────────
# Cada campo lista as grafias já vistas ou prováveis. A comparação é feita sobre
# o texto normalizado (sem acento, minúsculo, sem pontuação), então "Custo
# Mensal", "CUSTO MENSAL" e "custo_mensal" caem no mesmo lugar.

COLUNAS_EQUIPE = {
    "funcao": ["funcao", "cargo", "recurso", "perfil", "colaborador", "posicao",
               "maodeobra", "descricao", "profissional"],
    "quantidade": ["quantidade", "qtd", "qtde", "qt", "headcount", "hc", "volume"],
    "salario": ["salario", "salariobase", "remuneracao", "salariomensal", "valorhora",
                "vlrhora", "custounitario"],
    "encargos": ["encargos", "encargossociais", "encargo"],
    "beneficios": ["beneficios", "beneficio"],
    "custo_mensal": ["customensal", "custome", "customes", "valormensal", "mensal",
                     "custommes"],
    "meses": ["meses", "qtdmeses", "periodo", "periodomeses", "vigencia", "vigenciameses",
              "mes"],
    "custo_total": ["custototal", "total", "valortotal", "custoanual", "totalanual",
                    "custoperiodo"],
}

COLUNAS_INVEST = {
    "item": ["item", "descricao", "investimento", "equipamento", "material", "recurso",
             "produto", "servico"],
    "categoria": ["categoria", "tipo", "grupo", "classificacao", "natureza"],
    "quantidade": ["quantidade", "qtd", "qtde", "qt", "volume"],
    "valor_unitario": ["valorunitario", "unitario", "vlunit", "vlrunitario",
                       "precounitario", "valorunit", "custounitario"],
    "valor_total": ["valortotal", "total", "valor", "custototal", "totalgeral"],
}

# Dicas pelo nome da aba: "descrição + total" aparece nos dois blocos, então o
# nome da aba desempata quando as colunas não são conclusivas.
ABA_EQUIPE = ["equipe", "maodeobra", "mo", "pessoal", "rh", "quadro", "headcount",
              "custopessoal", "recursos"]
ABA_INVEST = ["investimento", "capex", "material", "equipamento", "ativo", "compras",
              "despesa", "outroscustos"]

# Colunas que só fazem sentido em equipe: se aparecerem, é equipe.
EXCLUSIVAS_EQUIPE = ("salario", "encargos", "beneficios", "custo_mensal", "meses")

MAX_LINHAS_CABECALHO = 40   # até onde procurar a linha de cabeçalho em cada aba
MAX_VAZIAS_SEGUIDAS = 8     # quantas linhas em branco encerram um bloco


def _norm(v) -> str:
    if v is None:
        return ""
    s = str(v).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s)


def _num(v):
    """Número tolerante. Espelha `_mc_num` do main.py de propósito: os dois
    caminhos de ingestão têm que interpretar '1.234,56' igual."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("R$", "").replace("\xa0", " ").strip()
    s = re.sub(r"[^\d,.\-]", "", s)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        n = float(s)
    except ValueError:
        return 0.0
    return -n if neg else n


def _tem_numero(v) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if v is None:
        return False
    return bool(re.search(r"\d", str(v)))


def _mapear_cabecalho(linha, vocabulario):
    """Casa as células de uma linha com os campos do vocabulário.

    Devolve {campo: índice_da_coluna}. Primeira coluna que casa ganha: em
    planilha de MC as colunas de resumo repetem 'total' à direita, e pegar a
    da esquerda é o que corresponde à linha."""
    achado = {}
    for idx, cel in enumerate(linha):
        chave = _norm(cel)
        if not chave:
            continue
        for campo, apelidos in vocabulario.items():
            if campo in achado:
                continue
            if chave in apelidos:
                achado[campo] = idx
                break
        else:
            # Não bateu exato: tenta prefixo, pra pegar "custo total (r$)" →
            # "custototalr" começando com "custototal".
            for campo, apelidos in vocabulario.items():
                if campo in achado:
                    continue
                if any(chave.startswith(a) and len(a) >= 4 for a in apelidos):
                    achado[campo] = idx
                    break
    return achado


def _classificar(nome_aba, mapa_equipe, mapa_invest):
    """Decide se um cabeçalho é de equipe, de investimento, ou nada."""
    aba = _norm(nome_aba)
    dica_equipe = any(t in aba for t in ABA_EQUIPE)
    dica_invest = any(t in aba for t in ABA_INVEST)

    tem_exclusiva_equipe = any(c in mapa_equipe for c in EXCLUSIVAS_EQUIPE)
    equipe_ok = "funcao" in mapa_equipe and (
        tem_exclusiva_equipe or "custo_total" in mapa_equipe)
    invest_ok = "item" in mapa_invest and (
        "valor_total" in mapa_invest or "valor_unitario" in mapa_invest)

    if equipe_ok and tem_exclusiva_equipe:
        return "equipe"          # salário/meses só existe em equipe
    if equipe_ok and dica_equipe:
        return "equipe"
    if invest_ok and (dica_invest or not equipe_ok):
        return "investimentos"
    if equipe_ok:
        return "equipe"
    return None


def _ler_bloco(ws, inicio, mapa, campos_valor):
    """Lê as linhas abaixo do cabeçalho até o bloco acabar."""
    linhas = []
    vazias = 0
    max_col = max(mapa.values()) + 1
    for row in ws.iter_rows(min_row=inicio + 1, values_only=True):
        if not row:
            vazias += 1
            if vazias >= MAX_VAZIAS_SEGUIDAS:
                break
            continue
        celulas = list(row) + [None] * (max_col - len(row))
        d = {campo: celulas[idx] for campo, idx in mapa.items() if idx < len(celulas)}
        rotulo = str(d.get("funcao") or d.get("item") or "").strip()
        rot_norm = _norm(rotulo)
        tem_valor = any(_num(d.get(c)) for c in campos_valor)

        if not rotulo and not tem_valor:
            vazias += 1
            if vazias >= MAX_VAZIAS_SEGUIDAS:
                break
            continue
        vazias = 0

        # Linha de total dentro do bloco encerra a leitura: somá-la junto
        # dobraria o valor do bloco.
        if rot_norm.startswith("total") or rot_norm.startswith("subtotal"):
            break
        if not rotulo:
            continue
        linhas.append({k: v for k, v in d.items()})
    return linhas


def _achar_totais(ws):
    """Procura os totais declarados na planilha.

    É o dado mais importante do arquivo: é contra ele que o Ops confere a soma
    das linhas. Uma célula tipo 'TOTAL EQUIPE' e o número mais próximo à
    direita (ou logo abaixo)."""
    out = {}
    for row in ws.iter_rows(values_only=True):
        if not row:
            continue
        for idx, cel in enumerate(row):
            chave = _norm(cel)
            if not chave.startswith("total"):
                continue
            if "equipe" in chave or "maodeobra" in chave or "pessoal" in chave:
                campo = "equipe"
            elif "investimento" in chave or "capex" in chave:
                campo = "investimentos"
            else:
                continue
            if campo in out:
                continue
            for viz in row[idx + 1:]:
                if _tem_numero(viz):
                    out[campo] = _num(viz)
                    break
    return out


def extrair_mc_de_xlsx(conteudo: bytes, nome_arquivo: str = "") -> dict:
    """Lê um .xlsx de MC e devolve o payload no formato de `/api/mc/importar`.

    Sempre devolve `_diagnostico`: as abas vistas e o que foi reconhecido em
    cada uma. Quando a extração vem vazia, é por ali que se calibra o
    vocabulário de colunas — sem precisar adivinhar o layout."""
    try:
        import openpyxl
    except ImportError as e:                                  # pragma: no cover
        raise ValueError("openpyxl não está disponível para ler a planilha.") from e

    if not conteudo or len(conteudo) < 50:
        raise ValueError("Arquivo vazio ou muito pequeno para ser uma planilha.")
    # .xlsx é um zip: começa com 'PK'. .xlsb também, então a checagem não
    # substitui a extensão -- mas pega XLS antigo e HTML de página de erro do
    # SharePoint, que é o engano comum quando o link pede login.
    if conteudo[:2] != b"PK":
        if conteudo[:1] in (b"<", b"\xd0"):
            raise ValueError(
                "O conteúdo baixado não é um .xlsx. Se veio de link do OneDrive, "
                "provavelmente o link pede login e voltou uma página HTML — use um "
                "link de compartilhamento que permita acesso direto, ou envie o "
                "arquivo.")
        raise ValueError("Arquivo não parece ser um .xlsx.")

    try:
        # data_only: queremos o valor calculado das fórmulas, não a fórmula.
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True, read_only=True)
    except Exception as e:
        raise ValueError(
            f"Não foi possível abrir a planilha como .xlsx ({e}). Se o arquivo "
            f"original é .xlsb, abra no Excel e salve como .xlsx.") from e

    equipe, invest, totais, diag = [], [], {}, []
    try:
        for ws in wb.worksheets:
            info = {"aba": ws.title, "equipe": 0, "investimentos": 0, "cabecalhos": []}
            primeiras = []
            for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
                primeiras.append((i, list(row) if row else []))
                if i >= MAX_LINHAS_CABECALHO:
                    break

            for num, linha in primeiras:
                if not any(_norm(c) for c in linha):
                    continue
                mapa_e = _mapear_cabecalho(linha, COLUNAS_EQUIPE)
                mapa_i = _mapear_cabecalho(linha, COLUNAS_INVEST)
                tipo = _classificar(ws.title, mapa_e, mapa_i)
                if not tipo:
                    continue
                if tipo == "equipe" and not equipe:
                    brutas = _ler_bloco(ws, num, mapa_e,
                                        ("quantidade", "salario", "custo_mensal", "custo_total"))
                    equipe = brutas
                    info["equipe"] = len(brutas)
                    info["cabecalhos"].append({"linha": num, "tipo": "equipe",
                                               "colunas": sorted(mapa_e)})
                elif tipo == "investimentos" and not invest:
                    brutas = _ler_bloco(ws, num, mapa_i,
                                        ("quantidade", "valor_unitario", "valor_total"))
                    invest = brutas
                    info["investimentos"] = len(brutas)
                    info["cabecalhos"].append({"linha": num, "tipo": "investimentos",
                                               "colunas": sorted(mapa_i)})

            for campo, valor in _achar_totais(ws).items():
                totais.setdefault(campo, valor)

            # Só reporta aba que disse algo, pra o diagnóstico não virar ruído
            # numa planilha com 20 abas de instrução.
            if info["cabecalhos"] or any(_norm(t) in _norm(ws.title) for t in ABA_EQUIPE + ABA_INVEST):
                diag.append(info)
    finally:
        try:
            wb.close()
        except Exception:
            pass

    payload = {
        "arquivo": nome_arquivo or "",
        "equipe": equipe,
        "investimentos": invest,
        "_diagnostico": {
            "abas": [ws.title for ws in wb.worksheets] if wb.worksheets else [],
            "reconhecido": diag,
            "linhas_equipe": len(equipe),
            "linhas_investimentos": len(invest),
            "totais_encontrados": sorted(totais),
        },
    }
    if totais:
        payload["totais"] = totais

    contrato, ano = _contrato_ano_do_nome(nome_arquivo)
    if contrato:
        payload["contrato"] = contrato
    if ano:
        payload["ano"] = ano
    return payload


def nome_do_arquivo_da_url(url: str) -> str:
    """Nome do arquivo a partir da URL, com percent-encoding desfeito.

    Link do OneDrive traz o nome escapado ('MC%20F260015-8%20-%20ANO%2001.xlsx').
    Sem desescapar, o '%20' cola um dígito na frente do código e a detecção do
    contrato falha — o '0' do '%20' impede o limite de palavra do regex."""
    from urllib.parse import unquote
    bruto = (url or "").split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    return unquote(bruto)


def _contrato_ano_do_nome(nome: str):
    """Tira contrato e ano do nome do arquivo.

    Os arquivos seguem 'MC F260015-7 <descrição> - ANO 01.xlsx', então dá pra
    inferir os dois — mas quem chama pode sobrescrever, porque o nome do
    arquivo nem sempre traz o código de faturamento correto (aconteceu: o
    assunto do e-mail dizia F260301 e o certo era F260534)."""
    if not nome:
        return None, None
    contrato = None
    m = re.search(r"\b([Ff]\d{6}(?:-\d+)?)\b", nome)
    if m:
        contrato = m.group(1).upper()
    ano = None
    m = re.search(r"ANO\s*(\d{1,2})", nome, re.IGNORECASE)
    if m:
        ano = m.group(1).zfill(2)
    return contrato, ano
