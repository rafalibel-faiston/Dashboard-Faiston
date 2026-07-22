"""
Testes da terceira rodada de ajustes: histórico de andamento (localização
do técnico + status de acesso + descrição, com quantas atualizações forem
necessárias durante a atividade), subprojeto com autocomplete por cliente,
e a particularidade "equipamento removido".
"""
from fastapi.testclient import TestClient


def _payload(cliente_id, **overrides):
    base = {"cliente_id": cliente_id, "data": "2026-07-22"}
    base.update(overrides)
    return base


def _n2_client(app, n2_user):
    client = TestClient(app)
    resp = client.post("/api/login", json={"usuario": n2_user["usuario"], "senha": n2_user["senha"]})
    assert resp.status_code == 200, resp.text
    return client


class TestTransicaoParaEmAndamentoRegistraHistorico:
    def test_registra_entrada_no_historico(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "em_andamento", "andamento_descricao": "Chegou no local, iniciando",
                "localizacao": "no_local", "acesso": "com_acesso",
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["localizacao"] == "no_local"
            assert item["acesso"] == "com_acesso"

            hist = admin_client.get(f"/api/status-campo/{aid}/andamento").json()
            assert len(hist) == 1
            assert hist[0]["localizacao"] == "no_local"
            assert hist[0]["acesso"] == "com_acesso"
            assert hist[0]["descricao"] == "Chegou no local, iniciando"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_localizacao_invalida_retorna_400(self, admin_client, cliente_teste):
        # Localização virou o campo obrigatório do fluxo escalonado do N2
        # (deslocamento/no local -> chegada+acesso -> início+tipo) -- um
        # valor inválido/ausente barra a transição pra "em_andamento" em vez
        # de silenciosamente virar null.
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "em_andamento", "andamento_descricao": "teste",
                "localizacao": "valor-invalido", "acesso": "outro-invalido",
            })
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_acesso_invalido_vira_null(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "em_andamento", "andamento_descricao": "teste",
                "localizacao": "no_local", "acesso": "outro-invalido",
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["localizacao"] == "no_local"
            assert item["acesso"] is None
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestAtualizacoesConstantesDeAndamento:
    """POST /andamento ("Atualizar andamento") é só o que está sendo feito
    tecnicamente (andamento_tipo) + descrição -- localização/acesso saíram
    daqui, viraram PATCH /situacao (ver TestAtualizarSituacao), porque são
    informações diferentes (onde o técnico está x o que ele está fazendo)."""
    def test_pode_adicionar_varias_atualizacoes_sem_trocar_status(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "em_andamento", "andamento_descricao": "Fixando no rack",
                "localizacao": "no_local", "acesso": "com_acesso",
            })
            resp = admin_client.post(f"/api/status-campo/{aid}/andamento",
                                      json={"descricao": "Configurando o switch", "andamento_tipo": "instalando"})
            assert resp.status_code == 200, resp.text
            resp = admin_client.post(f"/api/status-campo/{aid}/andamento",
                                      json={"descricao": "Validando conectividade", "andamento_tipo": "instalando"})
            assert resp.status_code == 200, resp.text

            hist = admin_client.get(f"/api/status-campo/{aid}/andamento").json()
            assert len(hist) == 3
            assert [h["descricao"] for h in hist] == ["Fixando no rack", "Configurando o switch", "Validando conectividade"]

            # o snapshot na atividade reflete a última atualização
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["andamento_descricao"] == "Validando conectividade"
            # localização/acesso definidos na transição inicial não são
            # apagados por uma atualização de andamento que não os envia
            assert item["localizacao"] == "no_local"
            assert item["acesso"] == "com_acesso"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_andamento_tipo_ausente_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.post(f"/api/status-campo/{aid}/andamento", json={"descricao": "algo"})
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_descricao_vazia_e_permitida_com_andamento_tipo(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.post(f"/api/status-campo/{aid}/andamento",
                                      json={"descricao": "", "andamento_tipo": "trocando"})
            assert resp.status_code == 200, resp.text
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_n2_de_outro_nao_pode_atualizar_andamento(self, admin_client, app, n2_user, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            n2_client = _n2_client(app, n2_user)
            resp = n2_client.post(f"/api/status-campo/{aid}/andamento",
                                   json={"descricao": "não deveria funcionar", "andamento_tipo": "instalando"})
            assert resp.status_code == 403
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestAtualizarSituacao:
    """PATCH /situacao ("Atualizar situação") é localização/acesso/hora de
    chegada -- estado atual do técnico. Não gera entrada na linha do tempo
    de andamento (só atualiza o snapshot), diferente de POST /andamento."""
    def test_atualiza_sem_criar_entrada_no_historico(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/situacao", json={
                "localizacao": "no_local", "acesso": "com_acesso", "hora_chegada": "14:30",
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["localizacao"] == "no_local"
            assert item["acesso"] == "com_acesso"
            assert item["hora_chegada"] == "14:30"

            hist = admin_client.get(f"/api/status-campo/{aid}/andamento").json()
            assert hist == []
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_localizacao_ausente_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/situacao", json={"acesso": "com_acesso"})
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_n2_de_outro_nao_pode_atualizar_situacao(self, admin_client, app, n2_user, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            n2_client = _n2_client(app, n2_user)
            resp = n2_client.patch(f"/api/status-campo/{aid}/situacao", json={"localizacao": "no_local"})
            assert resp.status_code == 403
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestSubprojeto:
    def test_subprojeto_e_salvo(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste, subprojeto="Migração de core")).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["subprojeto"] == "Migração de core"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_autocomplete_lista_subprojetos_distintos_do_cliente(self, admin_client, cliente_teste):
        a1 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, subprojeto="Migração de core")).json()["id"]
        a2 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, subprojeto="Migração de core")).json()["id"]
        a3 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, subprojeto="Expansão de wifi")).json()["id"]
        try:
            resp = admin_client.get("/api/status-campo/subprojetos", params={"cliente_id": cliente_teste})
            assert resp.status_code == 200
            assert sorted(resp.json()) == ["Expansão de wifi", "Migração de core"]
        finally:
            for aid in (a1, a2, a3):
                admin_client.delete(f"/api/status-campo/{aid}")

    def test_autocomplete_vazio_pra_cliente_sem_subprojetos(self, admin_client, cliente_teste):
        resp = admin_client.get("/api/status-campo/subprojetos", params={"cliente_id": cliente_teste})
        assert resp.status_code == 200
        assert resp.json() == []


class TestEquipamentoRemovido:
    def test_particularidade_e_detalhe_sao_salvos(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, particularidades=["equipamento_removido"],
            equipamento_removido_detalhe="Switch antigo, S/N ABC123",
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert "equipamento_removido" in item["particularidades"]
            assert item["equipamento_removido_detalhe"] == "Switch antigo, S/N ABC123"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_sem_particularidade_ignora_detalhe(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, equipamento_removido_detalhe="não deveria salvar isso",
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["equipamento_removido_detalhe"] == ""
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")
