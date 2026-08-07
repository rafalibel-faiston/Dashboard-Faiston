"""
Testes da extração de MC a partir do .xlsx (mc_planilha.py).

Estes rodam sem banco: são sobre ler planilha, não sobre persistir.

As planilhas de teste imitam o template real de MC — cabeçalho que não começa
na linha 1, abas de instrução no meio, linhas de separação em branco, linha de
TOTAL no fim do bloco e números como texto pt-BR. O layout foi montado a
partir do que a extração real do F260015-7 mostrou (funções com salário/meses,
investimentos com categoria/unitário) e do que o template declara nas próprias
abas.
"""
import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

from mc_planilha import _contrato_ano_do_nome, extrair_mc_de_xlsx  # noqa: E402


def montar(abas):
    """abas = {nome: [[celula, ...], ...]}"""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for nome, linhas in abas.items():
        ws = wb.create_sheet(nome[:31])
        for linha in linhas:
            ws.append(linha)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ABA_EQUIPE_REAL = [
    ["MC — Custo de Equipe", None, None, None, None, None],
    [None, None, None, None, None, None],
    ["Função", "Quantidade", "Salário", "Custo Mensal", "Meses", "Custo Total"],
    ["Assistente De Service Desk", 1, 4991.95, 4991.95, 9, 44927.52],
    ["Assistente De Service Desk", 2, 5390.91, 5390.91, 9, 97036.44],
    [None, None, None, None, None, None],
    ["Ticket Manager Jr", 30, 27.94, 27.94, 12, 10058.28],
    ["TOTAL EQUIPE", None, None, None, None, 152022.24],
]

ABA_INVEST_REAL = [
    ["Investimentos do projeto", None, None, None],
    ["Item", "Categoria", "Quantidade", "Valor Unitário", "Valor Total"],
    ["Notebook", "Equipamentos", 1, 2000, 2000],
    ["Celular", "Outros", 1, 800, 800],
    ["Despesa Operação", "Outros", 1, 0, 0],
    ["TOTAL INVESTIMENTOS", None, None, None, 2800],
]


class TestExtracaoBasica:
    def test_le_equipe_e_investimentos(self):
        d = extrair_mc_de_xlsx(montar({"2.1 Equipe": ABA_EQUIPE_REAL,
                                       "2.2 Investimentos": ABA_INVEST_REAL}))
        assert [l["funcao"] for l in d["equipe"]] == [
            "Assistente De Service Desk", "Assistente De Service Desk", "Ticket Manager Jr"]
        assert [l["item"] for l in d["investimentos"]] == [
            "Notebook", "Celular", "Despesa Operação"]

    def test_le_os_totais_declarados(self):
        """O ponto todo da mudança: o total sai da planilha, não do modelo."""
        d = extrair_mc_de_xlsx(montar({"Equipe": ABA_EQUIPE_REAL,
                                       "Investimentos": ABA_INVEST_REAL}))
        assert d["totais"]["equipe"] == 152022.24
        assert d["totais"]["investimentos"] == 2800

    def test_linha_de_total_nao_entra_como_linha(self):
        """Somar a linha TOTAL junto dobraria o valor do bloco."""
        d = extrair_mc_de_xlsx(montar({"Equipe": ABA_EQUIPE_REAL}))
        assert all("TOTAL" not in (l.get("funcao") or "").upper() for l in d["equipe"])
        assert len(d["equipe"]) == 3

    def test_cabecalho_fora_da_primeira_linha(self):
        d = extrair_mc_de_xlsx(montar({"Equipe": ABA_EQUIPE_REAL}))
        assert len(d["equipe"]) == 3

    def test_linha_em_branco_no_meio_nao_corta_o_bloco(self):
        """A planilha real tem linhas de separação entre grupos."""
        d = extrair_mc_de_xlsx(montar({"Equipe": ABA_EQUIPE_REAL}))
        assert any(l["funcao"] == "Ticket Manager Jr" for l in d["equipe"])

    def test_abas_de_instrucao_sao_ignoradas(self):
        abas = {
            "Instruções": [["REVISÃO", "DATA", "HISTÓRICO"],
                           [1.0, "02/01/2021", "Liberação V1 MC"]],
            "Equipe": ABA_EQUIPE_REAL,
        }
        d = extrair_mc_de_xlsx(montar(abas))
        assert len(d["equipe"]) == 3
        assert not d["investimentos"]

    def test_numeros_em_texto_ptbr(self):
        abas = {"Equipe": [
            ["Função", "Custo Total"],
            ["Analista", "R$ 44.927,52"],
            ["TOTAL EQUIPE", "R$ 44.927,52"],
        ]}
        d = extrair_mc_de_xlsx(montar(abas))
        assert d["totais"]["equipe"] == 44927.52


class TestVariacoesDeCabecalho:
    def test_sinonimos_de_coluna(self):
        abas = {"Mão de Obra": [
            ["Cargo", "Qtde", "Remuneração", "Vigência (meses)", "Custo Total (R$)"],
            ["Coordenador", 1, 9500, 12, 219600],
        ]}
        d = extrair_mc_de_xlsx(montar(abas))
        assert len(d["equipe"]) == 1
        assert d["equipe"][0]["funcao"] == "Coordenador"
        assert d["equipe"][0]["meses"] == 12

    def test_investimento_sem_dica_no_nome_da_aba(self):
        abas = {"Planilha1": [
            ["Item", "Valor Unitário", "Valor Total"],
            ["Notebook", 2000, 2000],
        ]}
        d = extrair_mc_de_xlsx(montar(abas))
        assert len(d["investimentos"]) == 1

    def test_salario_desempata_equipe_de_investimento(self):
        """'Descrição + Total' serve pros dois; salário só existe em equipe."""
        abas = {"Planilha1": [
            ["Descrição", "Salário", "Meses", "Total"],
            ["Analista", 5000, 12, 60000],
        ]}
        d = extrair_mc_de_xlsx(montar(abas))
        assert len(d["equipe"]) == 1
        assert not d["investimentos"]


class TestDiagnostico:
    def test_diagnostico_lista_abas_e_o_que_reconheceu(self):
        d = extrair_mc_de_xlsx(montar({"Instruções": [["REVISÃO"]],
                                       "Equipe": ABA_EQUIPE_REAL}))
        diag = d["_diagnostico"]
        assert "Instruções" in diag["abas"] and "Equipe" in diag["abas"]
        assert diag["linhas_equipe"] == 3
        rec = [r for r in diag["reconhecido"] if r["aba"] == "Equipe"][0]
        assert rec["cabecalhos"][0]["tipo"] == "equipe"
        assert "custo_total" in rec["cabecalhos"][0]["colunas"]

    def test_planilha_sem_nada_reconhecivel_nao_explode(self):
        """Devolve vazio com diagnóstico, pra dar pra calibrar o vocabulário."""
        d = extrair_mc_de_xlsx(montar({"Aba X": [["foo", "bar"], [1, 2]]}))
        assert d["equipe"] == [] and d["investimentos"] == []
        assert d["_diagnostico"]["abas"] == ["Aba X"]


class TestArquivoInvalido:
    def test_html_de_login_do_onedrive(self):
        """Engano comum: o link pede login e volta uma página HTML."""
        with pytest.raises(ValueError, match="pede login"):
            extrair_mc_de_xlsx(b"<!DOCTYPE html><html><body>Sign in</body></html>" * 3)

    def test_arquivo_vazio(self):
        with pytest.raises(ValueError, match="vazio"):
            extrair_mc_de_xlsx(b"")

    def test_xlsb_da_mensagem_util(self):
        """.xlsb também começa com PK, então cai no load_workbook e falha lá."""
        with pytest.raises(ValueError, match="xlsx"):
            extrair_mc_de_xlsx(b"PK\x03\x04" + b"\x00" * 200, "MC F260015 - ANO 01.xlsb")


class TestContratoEAnoDoNome:
    @pytest.mark.parametrize("nome,contrato,ano", [
        ("MC F260015-7 Serviços de Suporte SGB - ANO 01.xlsx", "F260015-7", "01"),
        ("MC F260015 - ANO 02.xlsx", "F260015", "02"),
        ("F260534-4 - PTC - SBUX_Obsolecencia.xlsx", "F260534-4", None),
        ("MC f240523-5 vivo - ano 3.xlsx", "F240523-5", "03"),
        ("planilha qualquer.xlsx", None, None),
    ])
    def test_extrai(self, nome, contrato, ano):
        assert _contrato_ano_do_nome(nome) == (contrato, ano)

    def test_payload_traz_contrato_e_ano(self):
        d = extrair_mc_de_xlsx(montar({"Equipe": ABA_EQUIPE_REAL}),
                               "MC F260015-7 SGB - ANO 01.xlsx")
        assert d["contrato"] == "F260015-7"
        assert d["ano"] == "01"
        assert d["arquivo"] == "MC F260015-7 SGB - ANO 01.xlsx"


class TestNomeDaUrl:
    """Link do OneDrive traz o nome escapado; sem desescapar o contrato não é
    reconhecido (o '0' do '%20' cola no código e mata o limite de palavra)."""

    @pytest.mark.parametrize("url,esperado", [
        ("https://x.com/a/MC%20F260015-7%20SGB%20-%20ANO%2001.xlsx",
         "MC F260015-7 SGB - ANO 01.xlsx"),
        ("https://x.com/a/MC F260015 - ANO 02.xlsx?download=1",
         "MC F260015 - ANO 02.xlsx"),
        ("https://x.sharepoint.com/p/Documents/MC%20F240523-5.xlsx#frag",
         "MC F240523-5.xlsx"),
    ])
    def test_desescapa(self, url, esperado):
        from mc_planilha import nome_do_arquivo_da_url
        assert nome_do_arquivo_da_url(url) == esperado

    def test_contrato_reconhecido_apos_desescapar(self):
        from mc_planilha import nome_do_arquivo_da_url
        nome = nome_do_arquivo_da_url("https://x.com/MC%20F260015-7%20-%20ANO%2001.xlsx")
        assert _contrato_ano_do_nome(nome) == ("F260015-7", "01")
