"""
Testes do módulo de MC (Margem de Contribuição) — /api/mc/*.

Cobre o que a ingestão precisa garantir pra ser confiável quando passar a ser
chamada por automação: conferência dos totais da planilha, idempotência por
(contrato, ano), tolerância ao formato dos números, log de tentativas e
isolamento de acesso por perfil.

O caso-base usa o contrato F260015 ano 01, cujos totais foram conferidos
contra a planilha original: equipe R$ 310.926,63 e investimentos R$ 28.400,00.
"""
import uuid

import pytest


EQUIPE_F260015 = [
    {"funcao": "Coordenador de Projeto", "quantidade": 1, "salario": 9500.00,
     "encargos": 7600.00, "beneficios": 1200.00, "custo_mensal": 18300.00,
     "meses": 12, "custo_total": 219600.00},
    {"funcao": "Analista de Suporte N2", "quantidade": 1, "salario": 4200.00,
     "encargos": 3360.00, "beneficios": 1050.00, "custo_mensal": 7610.55,
     "meses": 12, "custo_total": 91326.63},
]
INVEST_F260015 = [
    {"item": "Notebook Dell Latitude", "quantidade": 4, "valor_unitario": 5600.00,
     "valor_total": 22400.00},
    {"item": "Kit de ferramentas de campo", "quantidade": 4, "valor_unitario": 1500.00,
     "valor_total": 6000.00},
]
TOTAL_EQUIPE = 310926.63
TOTAL_INVEST = 28400.00


def payload_mc(contrato=None, ano="01", **over):
    """Payload no formato que o `mc_extractor.py` entrega."""
    p = {
        "contrato": contrato or f"TESTE-MC-{uuid.uuid4().hex[:8].upper()}",
        "ano": ano,
        "cliente": "Cliente Teste MC",
        "cliente_final": "Cliente Final Teste",
        "projeto": "Sustentação de campo",
        "tcv": 1250000.00,
        "arquivo": "MC F260015 - ANO 01.xlsb",
        "equipe": [dict(l) for l in EQUIPE_F260015],
        "investimentos": [dict(l) for l in INVEST_F260015],
        "totais": {"equipe": TOTAL_EQUIPE, "investimentos": TOTAL_INVEST},
    }
    p.update(over)
    return p


@pytest.fixture()
def mc_limpa(admin_client):
    """Remove no teardown toda MC criada pelo teste."""
    criadas = []
    yield criadas
    for mid in criadas:
        admin_client.delete(f"/api/mc/contratos/{mid}")


def importar(client, payload, criadas=None):
    resp = client.post("/api/mc/importar", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if criadas is not None:
        criadas.append(body["id"])
    return body


class TestAcesso:
    def test_sem_login_nao_importa(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/mc/importar", json=payload_mc())
        assert resp.status_code == 403

    def test_sem_login_nao_lista(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        assert client.get("/api/mc/contratos").status_code == 403

    def test_funcionario_nao_acessa(self, admin_client, app):
        from conftest import login_client
        usuario = f"teste_mc_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Funcionário Teste MC",
            "perfil": "funcionario"})
        assert resp.status_code == 200, resp.text
        uid = resp.json()["id"]
        try:
            client = login_client(app, usuario, senha)
            assert client.get("/api/mc/contratos").status_code == 403
            assert client.post("/api/mc/importar", json=payload_mc()).status_code == 403
        finally:
            admin_client.delete(f"/api/usuarios/{uid}")

    def test_demo_le_mas_nao_importa(self, admin_client, app):
        """Perfil 'demo' enxerga MC (igual ao Forecast) mas não ingere: MC
        alimenta número financeiro de contrato e este é o mesmo endpoint que a
        automação vai chamar."""
        from conftest import login_client
        usuario = f"teste_mcdemo_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Demo Teste MC", "perfil": "demo"})
        assert resp.status_code == 200, resp.text
        uid = resp.json()["id"]
        try:
            client = login_client(app, usuario, senha)
            assert client.get("/api/mc/contratos").status_code == 200
            assert client.post("/api/mc/importar", json=payload_mc()).status_code == 403
        finally:
            admin_client.delete(f"/api/usuarios/{uid}")


class TestImportacao:
    def test_totais_batem_com_a_planilha(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        assert body["total_equipe"] == TOTAL_EQUIPE
        assert body["total_investimentos"] == TOTAL_INVEST
        assert body["linhas_equipe"] == 2
        assert body["linhas_investimentos"] == 2

    def test_sem_contrato_no_ops_vai_pra_revisao(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        # O código de teste não existe em forecast_projetos nem contratos_gestao
        assert body["match_origem"] == "nenhum"
        assert body["status"] == "REVISAO_MANUAL"
        assert "não encontrado" in body["motivo_revisao"]

    def test_contrato_existente_e_vinculado(self, admin_client, mc_limpa, cliente_teste):
        nome = f"CONTRATO-MC-{uuid.uuid4().hex[:8].upper()}"
        resp = admin_client.post("/api/gestao/contratos", json={"cliente_id": cliente_teste, "nome": nome})
        assert resp.status_code == 200, resp.text
        cid = resp.json()["id"]
        try:
            body = importar(admin_client, payload_mc(contrato=nome), mc_limpa)
            assert body["match_origem"] == "contrato_nome"
            assert body["contrato_id"] == cid
            assert body["status"] == "PROCESSADA"
            assert body["motivo_revisao"] == ""
        finally:
            admin_client.delete(f"/api/gestao/contratos/{cid}")

    def test_divergencia_de_total_vai_pra_revisao(self, admin_client, mc_limpa):
        p = payload_mc()
        p["totais"]["equipe"] = TOTAL_EQUIPE + 100  # planilha não fecha com as linhas
        body = importar(admin_client, p, mc_limpa)
        assert body["status"] == "REVISAO_MANUAL"
        assert "equipe divergente" in body["motivo_revisao"]

    def test_tolerancia_de_um_centavo(self, admin_client, mc_limpa):
        p = payload_mc()
        p["totais"]["equipe"] = TOTAL_EQUIPE + 0.004  # ruído de float do .xlsb
        body = importar(admin_client, p, mc_limpa)
        assert "divergente" not in body["motivo_revisao"]

    def test_numeros_em_texto_ptbr(self, admin_client, mc_limpa):
        p = payload_mc()
        p["equipe"][0]["custo_total"] = "R$ 219.600,00"
        p["equipe"][1]["custo_total"] = "91.326,63"
        p["totais"]["equipe"] = "R$ 310.926,63"
        body = importar(admin_client, p, mc_limpa)
        assert body["total_equipe"] == TOTAL_EQUIPE
        assert "divergente" not in body["motivo_revisao"]

    def test_deriva_total_quando_planilha_nao_traz(self, admin_client, mc_limpa):
        p = payload_mc()
        for l in p["equipe"]:
            l.pop("custo_total")
        p["equipe"][0]["quantidade"] = 1
        p["equipe"][1]["quantidade"] = 1
        p["totais"].pop("equipe")
        body = importar(admin_client, p, mc_limpa)
        # 18300*12 + 7610.55*12
        assert body["total_equipe"] == pytest.approx(310926.60, abs=0.01)

    def test_sem_contrato_rejeita(self, admin_client):
        resp = admin_client.post("/api/mc/importar", json={"ano": "01", "equipe": []})
        assert resp.status_code == 400

    def test_payload_vazio_marca_erro(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(equipe=[], investimentos=[], totais={}), mc_limpa)
        assert body["status"] == "ERRO"
        assert "sem linhas" in body["motivo_revisao"]

    def test_coluna_nova_da_planilha_nao_quebra(self, admin_client, mc_limpa):
        p = payload_mc()
        p["equipe"][0]["adicional_noturno"] = 320.50
        p["campo_que_o_ops_nao_conhece"] = {"qualquer": "coisa"}
        body = importar(admin_client, p, mc_limpa)
        assert body["total_equipe"] == TOTAL_EQUIPE

    def test_reimportar_substitui_e_nao_duplica(self, admin_client, mc_limpa):
        p = payload_mc()
        primeiro = importar(admin_client, p, mc_limpa)
        p2 = payload_mc(contrato=p["contrato"])
        p2["equipe"] = [dict(EQUIPE_F260015[0])]
        p2["totais"]["equipe"] = 219600.00
        segundo = importar(admin_client, p2)
        assert segundo["id"] == primeiro["id"]
        assert segundo["linhas_equipe"] == 1
        det = admin_client.get(f"/api/mc/contratos/{primeiro['id']}").json()
        assert len(det["equipe"]) == 1
        assert det["total_equipe"] == 219600.00

    def test_mesmo_contrato_anos_diferentes_coexistem(self, admin_client, mc_limpa):
        p = payload_mc()
        a1 = importar(admin_client, p, mc_limpa)
        a2 = importar(admin_client, payload_mc(contrato=p["contrato"], ano="02"), mc_limpa)
        assert a1["id"] != a2["id"]


class TestConsulta:
    def test_detalhe_traz_linhas_e_historico(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        det = admin_client.get(f"/api/mc/contratos/{body['id']}").json()
        assert det["contrato"] == body["contrato"]
        assert [l["funcao"] for l in det["equipe"]] == [l["funcao"] for l in EQUIPE_F260015]
        assert det["investimentos"][0]["valor_total"] == 22400.00
        assert det["total_declarado_equipe"] == TOTAL_EQUIPE
        assert len(det["historico"]) >= 1

    def test_detalhe_inexistente_404(self, admin_client):
        assert admin_client.get("/api/mc/contratos/99999999").status_code == 404

    def test_lista_filtra_por_status_e_contrato(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        lista = admin_client.get(f"/api/mc/contratos?contrato={body['contrato']}").json()
        assert lista["total"] == 1
        assert lista["itens"][0]["total_geral"] == pytest.approx(TOTAL_EQUIPE + TOTAL_INVEST)
        assert "por_status" in lista
        vazia = admin_client.get(
            f"/api/mc/contratos?contrato={body['contrato']}&status=PROCESSADA").json()
        assert vazia["total"] == 0

    def test_log_de_ingestoes(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        log = admin_client.get("/api/mc/ingestoes?limite=50").json()
        meus = [l for l in log["itens"] if l["contrato"] == body["contrato"]]
        assert meus and meus[0]["resultado"] == body["status"]
        assert meus[0]["linhas_equipe"] == 2


class TestStatusManual:
    def test_resolver_revisao_manual(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        assert body["status"] == "REVISAO_MANUAL"
        resp = admin_client.patch(f"/api/mc/contratos/{body['id']}/status",
                                  json={"status": "PROCESSADA", "observacao": "conferido na mão"})
        assert resp.status_code == 200, resp.text
        det = admin_client.get(f"/api/mc/contratos/{body['id']}").json()
        assert det["status"] == "PROCESSADA"
        assert det["processado_em"]
        assert any("conferido na mão" in h["detalhe"] for h in det["historico"])

    def test_vincular_contrato_na_mao(self, admin_client, mc_limpa, cliente_teste):
        body = importar(admin_client, payload_mc(), mc_limpa)
        nome = f"CONTRATO-MC-{uuid.uuid4().hex[:8].upper()}"
        cid = admin_client.post("/api/gestao/contratos",
                                json={"cliente_id": cliente_teste, "nome": nome}).json()["id"]
        try:
            resp = admin_client.patch(f"/api/mc/contratos/{body['id']}/status",
                                      json={"status": "PROCESSADA", "contrato_id": cid})
            assert resp.status_code == 200, resp.text
            det = admin_client.get(f"/api/mc/contratos/{body['id']}").json()
            assert det["contrato_id"] == cid
            assert det["match_origem"] == "manual"
        finally:
            admin_client.delete(f"/api/gestao/contratos/{cid}")

    def test_status_invalido_rejeita(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        resp = admin_client.patch(f"/api/mc/contratos/{body['id']}/status",
                                  json={"status": "QUALQUER_COISA"})
        assert resp.status_code == 400

    def test_contrato_inexistente_rejeita(self, admin_client, mc_limpa):
        body = importar(admin_client, payload_mc(), mc_limpa)
        resp = admin_client.patch(f"/api/mc/contratos/{body['id']}/status",
                                  json={"status": "PROCESSADA", "contrato_id": 99999999})
        assert resp.status_code == 400


class TestExclusao:
    def test_deletar_remove_linhas_e_mantem_log(self, admin_client):
        body = importar(admin_client, payload_mc())
        assert admin_client.delete(f"/api/mc/contratos/{body['id']}").status_code == 200
        assert admin_client.get(f"/api/mc/contratos/{body['id']}").status_code == 404
        log = admin_client.get("/api/mc/ingestoes?limite=200").json()
        assert any(l["contrato"] == body["contrato"] for l in log["itens"])
