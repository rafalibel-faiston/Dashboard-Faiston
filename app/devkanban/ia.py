"""IA do Kanban da Área de Dev: captura, briefing, comandos e standup.

Princípios (a IA propõe, o backend dispõe):
  * a prioridade do dia vem de regras (foco.py) -- o modelo só narra;
  * tudo que o modelo devolve em JSON é validado/sanitizado aqui antes de
    chegar ao usuário (tipos, enums, ids existentes, limites);
  * comandos em linguagem natural viram uma lista de ações de uma WHITELIST
    (sem excluir nada) e só são aplicadas depois da confirmação do usuário,
    revalidadas no servidor;
  * sem chave de LLM / com falha do provedor, briefing e standup caem num
    texto por regras -- o recurso nunca fica indisponível.
"""
import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.assistente.llm import ModeloIndisponivel, completar_texto
from app.devkanban import db as dkdb
from app.devkanban import foco

DIAS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
NOMES_STATUS = {"backlog": "Backlog", "todo": "A fazer", "doing": "Em progresso", "done": "Concluído"}
TIPOS_ACAO = ("filtrar", "mover", "prioridade", "adicionar_tag", "remover_tag", "renomear_tag", "atribuir", "prazo", "criar")

AVISO_MOTIVO = {
    "sem_chave": "IA não configurada neste ambiente — usei as regras do quadro",
    "chave_invalida": "chave da IA inválida — usei as regras do quadro",
    "limite_taxa": "limite de uso da IA atingido — usei as regras do quadro",
    "timeout": "a IA demorou demais — usei as regras do quadro",
    "sem_rede": "sem acesso ao provedor de IA — usei as regras do quadro",
}


async def _llm(mensagens: list[dict], temperature: float = 0.2) -> str:
    """Único ponto de chamada ao modelo neste módulo (os testes trocam este)."""
    texto, _ = await completar_texto(mensagens, temperature)
    return texto


# Mensagem pro usuário quando o recurso NÃO tem fallback (captura e comando dependem do modelo).
ERRO_MOTIVO = {
    "sem_chave": "A IA não está configurada neste ambiente.",
    "chave_invalida": "A chave da IA configurada é inválida.",
    "limite_taxa": "O limite de uso da IA foi atingido. Tente de novo em instantes.",
    "timeout": "A IA demorou demais para responder. Tente de novo.",
    "sem_rede": "Não consegui acessar o provedor de IA agora.",
}


def erro_da_falha(e: ModeloIndisponivel) -> str:
    return ERRO_MOTIVO.get(e.motivo, "A IA não respondeu agora. Tente de novo.")


def aviso_da_falha(e: ModeloIndisponivel) -> str:
    return AVISO_MOTIVO.get(e.motivo, "a IA não respondeu — usei as regras do quadro")


# ── JSON do modelo -> dados confiáveis ────────────────────────────

def extrair_json(texto: str) -> dict:
    t = (texto or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        raise ValueError("a resposta da IA não trouxe JSON")
    obj = json.loads(t[i:j + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON da IA em formato inesperado")
    return obj


def _limpa(s, n: int) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()[:n]


def _data_iso(v) -> Optional[str]:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v).strip()[:10]).isoformat()
    except ValueError:
        return None


def _tags_limpas(brutas, existentes: list[str]) -> list[str]:
    """Até 5 tags; reaproveita a grafia que já existe no quadro (evita Dispatcher x Dispacher)."""
    por_chave = {t.lower(): t for t in existentes}
    out, vistas = [], set()
    for t in (brutas if isinstance(brutas, list) else []):
        t = _limpa(t, 40)
        if not t or t.lower() in vistas:
            continue
        vistas.add(t.lower())
        out.append(por_chave.get(t.lower(), t))
        if len(out) == 5:
            break
    return out


def sanitizar_rascunho(r, tags_existentes: list[str]) -> Optional[dict]:
    """Uma tarefa vinda da IA (ou do cliente, na confirmação) -> dict seguro, ou None se inútil."""
    if not isinstance(r, dict):
        return None
    titulo = _limpa(r.get("titulo"), 200)
    if not titulo:
        return None
    prio = r.get("prioridade")
    status = r.get("status")
    atrib = r.get("atribuido_a")
    return {
        "titulo": titulo,
        "descricao": str(r.get("descricao") or "").strip()[:4000],
        "prioridade": prio if prio in dkdb.PRIORIDADES_VALIDAS else "media",
        "status": status if status in dkdb.STATUS_VALIDOS else "todo",
        "prazo": _data_iso(r.get("prazo")),
        "tags": _tags_limpas(r.get("tags"), tags_existentes),
        "checklist": [c for c in (_limpa(x, 300) for x in (r.get("checklist") if isinstance(r.get("checklist"), list) else [])) if c][:12],
        "link": str(r.get("link") or "").strip()[:500],
        "atribuido_a": int(atrib) if isinstance(atrib, (int, str)) and str(atrib).isdigit() else None,
    }


# ── Captura inteligente ───────────────────────────────────────────

def prompt_captura(hoje: date, tags_existentes: list[str]) -> str:
    return (
        "Você transforma texto bruto (e-mail, ata de reunião, mensagem de chat) em tarefas para o quadro "
        "de trabalho de um engenheiro de software. Responda SOMENTE com um JSON, sem texto fora dele, neste formato:\n"
        '{"tarefas":[{"titulo":"verbo no infinitivo + objeto, até 120 caracteres","descricao":"contexto útil, 1-3 frases",'
        '"prioridade":"urgente|alta|media|baixa","prazo":"AAAA-MM-DD ou null","tags":["Projeto","tag"],'
        '"checklist":["passo concreto 1","passo 2"]}]}\n'
        "Regras:\n"
        "- No máximo 5 tarefas; junte o que for a mesma entrega. Se não houver nada acionável, devolva {\"tarefas\":[]}.\n"
        f"- Hoje é {DIAS_PT[hoje.weekday()]}, {hoje.isoformat()}. Só preencha prazo se o texto der uma data explícita "
        "ou relativa clara (\"até sexta\", \"dia 20\"); senão use null.\n"
        "- prioridade: urgente só se o texto indicar urgência real ou prazo imediato; na dúvida, media.\n"
        "- A PRIMEIRA tag é o projeto/assunto principal. "
        + (f"Reaproveite estas tags já existentes quando fizer sentido: {', '.join(tags_existentes[:30])}. " if tags_existentes else "")
        + "Não invente tags demais (máx. 3).\n"
        "- checklist: só passos que o texto sustenta; pode ficar vazio.\n"
        "- Não invente fatos, nomes, números ou datas. Não copie dados pessoais sensíveis (CPF, senhas, tokens) para o resultado."
    )


async def capturar(texto: str, hoje: date, tags_existentes: list[str]) -> list[dict]:
    texto = (texto or "").strip()[:8000]
    bruto = await _llm([
        {"role": "system", "content": prompt_captura(hoje, tags_existentes)},
        {"role": "user", "content": texto},
    ], temperature=0.1)
    obj = extrair_json(bruto)
    brutas = obj.get("tarefas") if isinstance(obj.get("tarefas"), list) else []
    rascunhos = [r for r in (sanitizar_rascunho(x, tags_existentes) for x in brutas[:5]) if r]
    for r in rascunhos:
        r["status"] = "todo"           # captura sempre cai em A fazer; o usuário move depois
    return rascunhos


# ── Briefing ("meu dia") ──────────────────────────────────────────

def _resumo_numeros(tarefas: list[dict], hoje: date) -> dict:
    abertas = [t for t in tarefas if t["status"] != "done"]
    iso = hoje.isoformat()
    return {
        "abertas": len(abertas),
        "doing": sum(1 for t in abertas if t["status"] == "doing"),
        "atrasadas": sum(1 for t in abertas if t.get("prazo") and t["prazo"] < iso),
        "hoje": sum(1 for t in abertas if t.get("prazo") == iso),
        "paradas": sum(1 for t in abertas if any(m.startswith("parada há") for m in t.get("motivos", []))),
    }


def briefing_regras(tarefas: list[dict], top: list[dict], hoje: date, nome: str = "") -> str:
    n = _resumo_numeros(tarefas, hoje)
    abertura = f"{nome.split()[0] + ', você' if nome else 'Você'} tem {n['abertas']} tarefa{'s' if n['abertas'] != 1 else ''} em aberto"
    extras = []
    if n["atrasadas"]:
        extras.append(f"{n['atrasadas']} atrasada{'s' if n['atrasadas'] != 1 else ''}")
    if n["hoje"]:
        extras.append(f"{n['hoje']} vence{'m' if n['hoje'] != 1 else ''} hoje")
    if n["paradas"]:
        extras.append(f"{n['paradas']} parada{'s' if n['paradas'] != 1 else ''} há dias")
    linhas = [abertura + (" (" + ", ".join(extras) + ")" if extras else "") + "."]
    if not top:
        linhas.append("Nada pressionando hoje — bom momento para puxar algo do backlog.")
        return "\n".join(linhas)
    linhas.append("Comece por:")
    for i, t in enumerate(top[:3], 1):
        linhas.append(f"{i}. {t['titulo']} — {', '.join(t.get('motivos') or ['prioridade do quadro'])}")
    return "\n".join(linhas)


async def briefing(tarefas: list[dict], hoje: date, nome: str = "") -> dict:
    foco.anexar(tarefas, hoje)
    top = foco.ranquear(tarefas, 6, hoje)
    base = {"foco": [{"id": t["id"], "titulo": t["titulo"], "motivos": t["motivos"]} for t in top]}
    regras = briefing_regras(tarefas, top, hoje, nome)
    if not top:
        return {**base, "texto": regras, "gerado_com": "regras"}
    n = _resumo_numeros(tarefas, hoje)
    dados = "\n".join(
        f"{i}. {t['titulo']} [{NOMES_STATUS[t['status']]}, prioridade {t['prioridade']}"
        f"{', prazo ' + t['prazo'] if t.get('prazo') else ''}] — {', '.join(t['motivos']) or 'sem alerta'}"
        for i, t in enumerate(top, 1))
    sistema = (
        "Você é o copiloto de um engenheiro de software e escreve o briefing do dia a partir do estado do quadro de tarefas. "
        "Português do Brasil, tom direto e informal (\"você\"), até 110 palavras, sem markdown e sem emojis. "
        "Diga por onde começar e por quê, cite no máximo 3 tarefas pelo título exato e mencione o principal risco "
        "(atrasadas/paradas) se houver. Use APENAS os dados fornecidos; não invente tarefas, prazos ou números."
    )
    usuario = (
        f"Hoje: {DIAS_PT[hoje.weekday()]}, {hoje.strftime('%d/%m/%Y')}. Pessoa: {nome.split()[0] if nome else 'você'}.\n"
        f"Números: {n['abertas']} em aberto, {n['doing']} em progresso, {n['atrasadas']} atrasadas, "
        f"{n['hoje']} vencem hoje, {n['paradas']} paradas há dias.\n"
        f"Ordem de prioridade calculada pelo sistema:\n{dados}"
    )
    try:
        texto = (await _llm([{"role": "system", "content": sistema}, {"role": "user", "content": usuario}], 0.3)).strip()
        if not texto:
            raise ModeloIndisponivel("resposta vazia", motivo="erro_modelo")
        return {**base, "texto": texto, "gerado_com": "ia"}
    except ModeloIndisponivel as e:
        return {**base, "texto": regras, "gerado_com": "regras", "aviso": aviso_da_falha(e)}


# ── Standup / resumo ──────────────────────────────────────────────

def inicio_periodo(periodo: str, hoje: date) -> date:
    if periodo == "semana":
        return hoje - timedelta(days=7)
    return hoje - timedelta(days=3 if hoje.weekday() == 0 else 1)     # "ontem" na segunda = desde sexta


def dados_standup(tarefas: list[dict], periodo: str, hoje: date) -> dict:
    foco.anexar(tarefas, hoje)
    ini = inicio_periodo(periodo, hoje)
    feitas = []
    for t in tarefas:
        if t["status"] == "done" and t.get("concluido_em"):
            try:
                if datetime.fromisoformat(t["concluido_em"]).date() >= ini:
                    feitas.append(t)
            except ValueError:
                pass
    feitas.sort(key=lambda t: t["concluido_em"])
    andamento = [t for t in tarefas if t["status"] == "doing"]
    proximas = [t for t in foco.ranquear([t for t in tarefas if t["status"] == "todo"], 5, hoje)]
    iso = hoje.isoformat()
    bloqueios = [t for t in tarefas if t["status"] != "done" and (
        t.get("pausado") or (t.get("prazo") and t["prazo"] < iso) or any(m.startswith("parada há") for m in t.get("motivos", [])))]
    return {"feitas": feitas, "andamento": andamento, "proximas": proximas, "bloqueios": bloqueios, "desde": ini}


def standup_regras(d: dict, periodo: str) -> str:
    def lista(ts, extra=lambda t: ""):
        return "\n".join(f"- {t['titulo']}{extra(t)}" for t in ts) or "- (nada)"
    titulo_feito = "O que fiz na semana" if periodo == "semana" else "O que fiz"
    return (
        f"{titulo_feito}:\n{lista(d['feitas'])}\n\n"
        f"Em andamento:\n{lista(d['andamento'])}\n\n"
        f"O que vou fazer:\n{lista(d['proximas'], lambda t: ' (' + ', '.join(t['motivos'][:2]) + ')' if t.get('motivos') else '')}\n\n"
        f"Bloqueios e pontos de atenção:\n{lista(d['bloqueios'], lambda t: ' (' + ', '.join(t.get('motivos') or ['pausada']) + ')')}"
    )


async def standup(tarefas: list[dict], periodo: str, hoje: date, nome: str = "") -> dict:
    d = dados_standup(tarefas, periodo, hoje)
    regras = standup_regras(d, periodo)
    if not (d["feitas"] or d["andamento"] or d["proximas"] or d["bloqueios"]):
        return {"texto": regras, "gerado_com": "regras"}
    def bloco(ts):
        return "\n".join(f"- {t['titulo']} [{', '.join(t.get('motivos') or []) or t['prioridade']}]" for t in ts) or "- (nada)"
    sistema = (
        "Você escreve o standup de um engenheiro de software a partir das tarefas do quadro dele. Português do Brasil, "
        "primeira pessoa, tom objetivo, sem markdown e sem emojis. Use EXATAMENTE estas quatro seções, nesta ordem, "
        "cada uma com o título seguido de dois pontos e itens iniciados por '- ': 'O que fiz', 'Em andamento', "
        "'O que vou fazer', 'Bloqueios e pontos de atenção'. Reescreva cada item de forma curta e clara, sem inventar "
        "nada além dos dados. Se uma seção estiver vazia, escreva '- (nada)'."
    )
    usuario = (
        f"Período: desde {d['desde'].strftime('%d/%m/%Y')} até hoje ({hoje.strftime('%d/%m/%Y')}).\n"
        f"Concluídas no período:\n{bloco(d['feitas'])}\n\nEm andamento:\n{bloco(d['andamento'])}\n\n"
        f"Próximas (por prioridade):\n{bloco(d['proximas'])}\n\nBloqueios / atenção:\n{bloco(d['bloqueios'])}"
    )
    try:
        texto = (await _llm([{"role": "system", "content": sistema}, {"role": "user", "content": usuario}], 0.2)).strip()
        if not texto:
            raise ModeloIndisponivel("resposta vazia", motivo="erro_modelo")
        return {"texto": texto, "gerado_com": "ia"}
    except ModeloIndisponivel as e:
        return {"texto": regras, "gerado_com": "regras", "aviso": aviso_da_falha(e)}


# ── Comando em linguagem natural ──────────────────────────────────

def _linha_tarefa(t: dict) -> str:
    return " | ".join([
        str(t["id"]), t["status"], t["prioridade"], t.get("prazo") or "-",
        ",".join(t.get("tags") or []) or "-", t.get("atribuido_a_nome") or "-", t["titulo"][:90],
    ])


def prompt_comando(hoje: date, tarefas: list[dict], usuarios: list[dict], tags: list[str]) -> str:
    linhas = "\n".join(_linha_tarefa(t) for t in tarefas)
    return (
        "Você controla um quadro kanban de tarefas a partir de pedidos em português. Responda SOMENTE com um JSON:\n"
        '{"resposta":"texto curto para o usuário ou null","acoes":[ ... ]}\n'
        "Tipos de ação permitidos (e NENHUM outro; nunca exclua tarefas):\n"
        '- {"tipo":"filtrar","tag":"?","atribuido":"nome?","prioridade":"?","atrasadas":true|false,"busca":"?"}  (só muda o que o usuário vê)\n'
        '- {"tipo":"mover","ids":[..],"status":"backlog|todo|doing|done"}\n'
        '- {"tipo":"prioridade","ids":[..],"prioridade":"urgente|alta|media|baixa"}\n'
        '- {"tipo":"adicionar_tag","ids":[..],"tag":"nome"}   {"tipo":"remover_tag","ids":[..],"tag":"nome"}\n'
        '- {"tipo":"renomear_tag","de":"tag existente","para":"novo nome"}  (renomeia/unifica a tag em TODAS as tarefas, ex.: corrigir typo)\n'
        '- {"tipo":"atribuir","ids":[..],"usuario":"nome exato ou null para ninguém"}\n'
        '- {"tipo":"prazo","ids":[..],"prazo":"AAAA-MM-DD ou null"}\n'
        '- {"tipo":"criar","titulo":"..","status":"todo","prioridade":"media","prazo":null,"tags":[..]}\n'
        "Regras: use apenas ids da lista abaixo; para perguntas (\"o que está atrasado?\") responda em 'resposta' e, se ajudar, "
        "inclua uma ação 'filtrar'; se o pedido for ambíguo ou impossível, explique em 'resposta' e deixe 'acoes' vazio; "
        "no máximo 10 ações. Não invente ids, tags de projeto novas sem o usuário pedir, nem nomes de pessoas.\n"
        f"Hoje: {DIAS_PT[hoje.weekday()]}, {hoje.isoformat()}.\n"
        f"Pessoas: {', '.join(u['nome'] for u in usuarios) or '-'}.\n"
        f"Tags existentes: {', '.join(tags) or '-'}.\n"
        "Tarefas (id | status | prioridade | prazo | tags | responsável | título):\n" + linhas
    )


def _titulos_resumo(ids: list[int], por_id: dict) -> str:
    nomes = [f"«{por_id[i]['titulo'][:40]}»" for i in ids[:3]]
    return ", ".join(nomes) + (f" e mais {len(ids) - 3}" if len(ids) > 3 else "")


def validar_acoes(brutas, tarefas: list[dict], usuarios: list[dict], tags_existentes: list[str]) -> list[dict]:
    """Whitelist + ids existentes + enums + limites. Devolve ações prontas pra aplicar
    (cada uma com um `resumo` legível). Usada na proposta E de novo na aplicação:
    o servidor nunca confia no que o cliente devolve."""
    por_id = {t["id"]: t for t in tarefas}
    por_nome = {u["nome"].strip().lower(): u for u in usuarios}
    out = []
    for a in (brutas if isinstance(brutas, list) else [])[:10]:
        if not isinstance(a, dict) or a.get("tipo") not in TIPOS_ACAO:
            continue
        tipo = a["tipo"]

        if tipo == "filtrar":
            f = {}
            tag = _tags_limpas([a.get("tag")], tags_existentes) if a.get("tag") else []
            if tag:
                f["tag"] = tag[0]
            if a.get("atribuido"):
                u = por_nome.get(str(a["atribuido"]).strip().lower())
                if u:
                    f["atrib"] = str(u["id"])
            if a.get("prioridade") in dkdb.PRIORIDADES_VALIDAS:
                f["prio"] = a["prioridade"]
            if a.get("atrasadas") is True:
                f["atrasadas"] = True
            if a.get("busca"):
                f["busca"] = _limpa(a["busca"], 80)
            if f:
                out.append({"tipo": "filtrar", "tag": f.get("tag"), "atribuido": a.get("atribuido"), "prioridade": f.get("prio"),
                            "atrasadas": f.get("atrasadas", False), "busca": f.get("busca"), "filtro": f,
                            "resumo": "Filtrar o quadro", "mutavel": False})
            continue

        if tipo == "criar":
            r = sanitizar_rascunho(a, tags_existentes)
            if r:
                out.append({**r, "tipo": "criar", "resumo": f"Criar a tarefa «{r['titulo'][:60]}»", "mutavel": True})
            continue

        if tipo == "renomear_tag":
            de = next((t for t in tags_existentes if t.lower() == str(a.get("de") or "").strip().lower()), None)
            para = _limpa(a.get("para"), 40)
            if not de or not para or para == de:
                continue
            para = next((t for t in tags_existentes if t.lower() == para.lower() and t.lower() != de.lower()), para)   # reaproveita grafia existente
            n_afetadas = sum(1 for t in tarefas if any(x.lower() == de.lower() for x in (t.get("tags") or [])))
            if not n_afetadas:
                continue
            out.append({"tipo": "renomear_tag", "de": de, "para": para, "mutavel": True,
                        "resumo": f"Renomear a tag «{de}» para «{para}» em {n_afetadas} tarefa{'s' if n_afetadas != 1 else ''}"})
            continue

        ids = []
        for i in (a.get("ids") if isinstance(a.get("ids"), list) else []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i in por_id and i not in ids:
                ids.append(i)
        ids = ids[:50]
        if not ids:
            continue
        base = {"tipo": tipo, "ids": ids, "mutavel": True}
        quem = _titulos_resumo(ids, por_id)
        n = f"{len(ids)} tarefa{'s' if len(ids) != 1 else ''}"

        if tipo == "mover" and a.get("status") in dkdb.STATUS_VALIDOS:
            base.update(status=a["status"], resumo=f"Mover {n} para {NOMES_STATUS[a['status']]}: {quem}")
        elif tipo == "prioridade" and a.get("prioridade") in dkdb.PRIORIDADES_VALIDAS:
            base.update(prioridade=a["prioridade"], resumo=f"Prioridade «{a['prioridade']}» em {n}: {quem}")
        elif tipo in ("adicionar_tag", "remover_tag") and _limpa(a.get("tag"), 40):
            tag = _tags_limpas([a["tag"]], tags_existentes)[0]
            verbo = "Adicionar" if tipo == "adicionar_tag" else "Remover"
            base.update(tag=tag, resumo=f"{verbo} a tag «{tag}» em {n}: {quem}")
        elif tipo == "atribuir":
            u = por_nome.get(str(a.get("usuario") or "").strip().lower()) if a.get("usuario") else None
            if a.get("usuario") and not u:
                continue                                      # nome que não existe: descarta
            base.update(usuario=a.get("usuario"), usuario_id=u["id"] if u else None,
                        resumo=(f"Atribuir {n} a {u['nome']}: {quem}" if u else f"Tirar o responsável de {n}: {quem}"))
        elif tipo == "prazo":
            prazo = _data_iso(a.get("prazo"))
            if a.get("prazo") and not prazo:
                continue
            base.update(prazo=prazo, resumo=(f"Prazo {date.fromisoformat(prazo).strftime('%d/%m/%Y')} em {n}: {quem}" if prazo else f"Remover o prazo de {n}: {quem}"))
        else:
            continue
        out.append(base)
    return out


async def comando(texto: str, hoje: date, tarefas: list[dict], usuarios: list[dict]) -> dict:
    # Contexto enxuto: abertas + concluídas dos últimos 14 dias, no máx. 250 linhas.
    corte = (hoje - timedelta(days=14)).isoformat()
    ctx = [t for t in tarefas if t["status"] != "done" or (t.get("concluido_em") or "")[:10] >= corte][:250]
    tags = sorted({tg for t in tarefas for tg in (t.get("tags") or [])}, key=str.lower)
    bruto = await _llm([
        {"role": "system", "content": prompt_comando(hoje, ctx, usuarios, tags)},
        {"role": "user", "content": (texto or "").strip()[:500]},
    ], temperature=0.0)
    obj = extrair_json(bruto)
    resposta = obj.get("resposta")
    return {
        "resposta": str(resposta).strip()[:1200] if resposta else None,
        "acoes": validar_acoes(obj.get("acoes"), tarefas, usuarios, tags),
    }


def aplicar_acoes(cur, acoes: list[dict], sess: dict) -> dict:
    """Executa ações JÁ validadas (validar_acoes). Devolve contagem por tipo."""
    feito = {"movidas": 0, "alteradas": 0, "criadas": 0}
    for a in acoes:
        tipo, ids = a["tipo"], a.get("ids") or []
        if tipo == "mover":
            feito["movidas"] += sum(1 for i in ids if dkdb.mover_tarefa(cur, i, a["status"]))
        elif tipo == "prioridade":
            cur.execute("UPDATE dev_tarefas SET prioridade=%s, atualizado_em=NOW() WHERE id = ANY(%s)", (a["prioridade"], ids))
            feito["alteradas"] += cur.rowcount
        elif tipo == "adicionar_tag":
            cur.execute("""UPDATE dev_tarefas SET atualizado_em=NOW(), tags = CASE
                WHEN EXISTS (SELECT 1 FROM unnest(tags) x WHERE lower(x) = lower(%s)) THEN tags ELSE array_append(tags, %s) END
                WHERE id = ANY(%s)""", (a["tag"], a["tag"], ids))
            feito["alteradas"] += cur.rowcount
        elif tipo == "remover_tag":
            cur.execute("""UPDATE dev_tarefas SET atualizado_em=NOW(),
                tags = ARRAY(SELECT x FROM unnest(tags) x WHERE lower(x) <> lower(%s)) WHERE id = ANY(%s)""", (a["tag"], ids))
            feito["alteradas"] += cur.rowcount
        elif tipo == "renomear_tag":
            # Troca a tag e remove duplicata que sobrar na mesma tarefa (Dispacher + Dispatcher -> Dispatcher), mantendo a ordem.
            cur.execute("""UPDATE dev_tarefas SET atualizado_em=NOW(), tags = (
                SELECT COALESCE(array_agg(x ORDER BY ord), '{}') FROM (
                    SELECT DISTINCT ON (lower(x)) x, ord FROM (
                        SELECT CASE WHEN lower(t) = lower(%s) THEN %s ELSE t END AS x, ord
                        FROM unnest(tags) WITH ORDINALITY AS u(t, ord)) q
                    ORDER BY lower(x), ord) r)
                WHERE EXISTS (SELECT 1 FROM unnest(tags) y WHERE lower(y) = lower(%s))""", (a["de"], a["para"], a["de"]))
            feito["alteradas"] += cur.rowcount
        elif tipo == "atribuir":
            cur.execute("UPDATE dev_tarefas SET atribuido_a=%s, atualizado_em=NOW() WHERE id = ANY(%s)", (a.get("usuario_id"), ids))
            feito["alteradas"] += cur.rowcount
        elif tipo == "prazo":
            cur.execute("UPDATE dev_tarefas SET prazo=%s, atualizado_em=NOW() WHERE id = ANY(%s)", (a.get("prazo"), ids))
            feito["alteradas"] += cur.rowcount
        elif tipo == "criar":
            if dkdb.criar_tarefa(cur, a, sess["id"], origem="ia"):
                feito["criadas"] += 1
    return feito
