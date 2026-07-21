"""
Testes do Painel de Controle do N2: escala do dia (/api/escala-n2) e
resumo da equipe (/api/painel-n2/resumo).
"""
import uuid

import pytest


@pytest.fixture()
def n2_user(admin_client):
    usuario = f"teste_pn2_{uuid.uuid4().hex[:8]}"
    senha = "senhaTeste123"
    resp = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "senha": senha, "nome": "N2 Painel Teste", "perfil": "n2",
    })
    assert resp.status_code == 200, resp.text
    uid = resp.json()["id"]
    yield {"id": uid, "usuario": usuario, "senha": senha}
    admin_client.delete(f"/api/usuarios/{uid}")


class TestEscalaN2:
    def test_listar_sem_login_retorna_401(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/escala-n2")
        assert resp.status_code == 401

    def test_criar_sem_permissao_retorna_403(self, app, n2_user):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/login", json={"usuario": n2_user["usuario"], "senha": n2_user["senha"]})
        assert resp.status_code == 200
        resp = client.post("/api/escala-n2", json={"data": "2026-07-21", "n2_usuario_id": n2_user["id"]})
        assert resp.status_code == 403

    def test_criar_listar_editar_e_excluir(self, admin_client, n2_user):
        data = "2026-07-21"
        resp = admin_client.post("/api/escala-n2", json={
            "data": data, "n2_usuario_id": n2_user["id"], "horario_entrada": "07:00",
            "modalidade": "home", "atribuicao": "ARCOS - vistoria",
        })
        assert resp.status_code == 200, resp.text
        eid = resp.json()["id"]

        try:
            resp = admin_client.get("/api/escala-n2", params={"data": data})
            assert resp.status_code == 200
            item = next(x for x in resp.json() if x["id"] == eid)
            assert item["n2_nome"] == "N2 Painel Teste"
            assert item["modalidade"] == "home"
            assert item["atribuicao"] == "ARCOS - vistoria"

            resp = admin_client.put(f"/api/escala-n2/{eid}", json={
                "data": data, "n2_usuario_id": n2_user["id"], "horario_entrada": "08:00",
                "modalidade": "presencial", "atribuicao": "ZAMP",
            })
            assert resp.status_code == 200

            resp = admin_client.get("/api/escala-n2", params={"data": data})
            item = next(x for x in resp.json() if x["id"] == eid)
            assert item["modalidade"] == "presencial"
            assert item["atribuicao"] == "ZAMP"
        finally:
            resp = admin_client.delete(f"/api/escala-n2/{eid}")
            assert resp.status_code == 200

        resp = admin_client.get("/api/escala-n2", params={"data": data})
        assert eid not in [x["id"] for x in resp.json()]

    def test_modalidade_invalida_cai_para_presencial(self, admin_client, n2_user):
        resp = admin_client.post("/api/escala-n2", json={
            "data": "2026-07-21", "n2_usuario_id": n2_user["id"], "modalidade": "modalidade-invalida",
        })
        assert resp.status_code == 200
        eid = resp.json()["id"]
        try:
            item = next(x for x in admin_client.get("/api/escala-n2", params={"data": "2026-07-21"}).json() if x["id"] == eid)
            assert item["modalidade"] == "presencial"
        finally:
            admin_client.delete(f"/api/escala-n2/{eid}")

    def test_listar_sem_data_usa_hoje(self, admin_client):
        # não deve dar erro mesmo sem ninguém escalado hoje
        resp = admin_client.get("/api/escala-n2")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestPainelN2Resumo:
    def test_resumo_sem_login_retorna_403(self, app):
        # Mesmo padrão combinado `if not sess or perfil not in (...)` das
        # outras rotas restritas (ver test_status_campo.py) -- não-autenticado
        # cai no mesmo 403 de não-autorizado.
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/painel-n2/resumo")
        assert resp.status_code == 403

    def test_resumo_sem_permissao_retorna_403(self, app, n2_user):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/login", json={"usuario": n2_user["usuario"], "senha": n2_user["senha"]})
        assert resp.status_code == 200
        resp = client.get("/api/painel-n2/resumo")
        assert resp.status_code == 403

    def test_resumo_lista_n2_com_contagens(self, admin_client, n2_user, cliente_teste):
        # cria 2 atividades pro N2: uma concluída, uma pendente (status
        # inicial é sempre "agendado" -- a concluída muda depois via PATCH)
        base = {"cliente_id": cliente_teste, "data": "2026-07-21"}
        a1 = admin_client.post("/api/status-campo", json={**base, "n2_usuario_id": n2_user["id"]}).json()["id"]
        a2 = admin_client.post("/api/status-campo", json={**base, "n2_usuario_id": n2_user["id"]}).json()["id"]
        admin_client.patch(f"/api/status-campo/{a1}/status", json={"status": "concluido"})
        try:
            resp = admin_client.get("/api/painel-n2/resumo")
            assert resp.status_code == 200
            item = next(x for x in resp.json() if x["id"] == n2_user["id"])
            assert item["total"] == 2
            assert item["concluidas"] == 1
            assert item["pendentes"] == 1
        finally:
            admin_client.delete(f"/api/status-campo/{a1}")
            admin_client.delete(f"/api/status-campo/{a2}")
