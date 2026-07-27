"""
Testes do kanban da equipe dev (/api/dev-tarefas): CRUD básico e o
isolamento de acesso — só usuários com perfil 'dev' podem ver/mexer,
mesmo que perfil 'dev' tenha acesso equivalente a admin no resto do
sistema (ver get_session() em main.py).
"""
import uuid

import pytest


@pytest.fixture()
def dev_client(admin_client, app):
    from fastapi.testclient import TestClient
    usuario = f"teste_dev_{uuid.uuid4().hex[:8]}"
    senha = "senhaTeste123"
    resp = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "senha": senha, "nome": "Dev de Teste", "perfil": "dev",
    })
    assert resp.status_code == 200, resp.text
    uid = resp.json()["id"]
    client = TestClient(app)
    resp = client.post("/api/login", json={"usuario": usuario, "senha": senha})
    assert resp.status_code == 200
    yield client
    admin_client.delete(f"/api/usuarios/{uid}")


class TestAcesso:
    def test_admin_comum_nao_acessa(self, admin_client):
        resp = admin_client.get("/api/dev-tarefas")
        assert resp.status_code == 403

    def test_sem_login_nao_acessa(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/dev-tarefas")
        assert resp.status_code == 403

    def test_dev_acessa(self, dev_client):
        resp = dev_client.get("/api/dev-tarefas")
        assert resp.status_code == 200


class TestCRUD:
    def test_cria_com_status_padrao_todo(self, dev_client):
        resp = dev_client.post("/api/dev-tarefas", json={"titulo": "Corrigir bug X"})
        assert resp.status_code == 200, resp.text
        tid = resp.json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["status"] == "todo"
            assert item["titulo"] == "Corrigir bug X"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_titulo_vazio_e_rejeitado(self, dev_client):
        resp = dev_client.post("/api/dev-tarefas", json={"titulo": "   "})
        assert resp.status_code == 400

    def test_status_invalido_cai_para_todo(self, dev_client):
        resp = dev_client.post("/api/dev-tarefas", json={"titulo": "Teste", "status": "algo-invalido"})
        tid = resp.json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["status"] == "todo"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_atualiza_status_e_titulo(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Original"}).json()["id"]
        try:
            resp = dev_client.put(f"/api/dev-tarefas/{tid}", json={"titulo": "Editado", "status": "doing"})
            assert resp.status_code == 200
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["titulo"] == "Editado"
            assert item["status"] == "doing"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_deleta_tarefa(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Vai ser deletada"}).json()["id"]
        resp = dev_client.delete(f"/api/dev-tarefas/{tid}")
        assert resp.status_code == 200
        ids = [t["id"] for t in dev_client.get("/api/dev-tarefas").json()]
        assert tid not in ids

    def test_atribuir_a_outro_dev(self, dev_client, admin_client):
        outro_usuario = f"teste_dev2_{uuid.uuid4().hex[:8]}"
        r = admin_client.post("/api/usuarios", json={
            "usuario": outro_usuario, "senha": "senhaTeste123", "nome": "Segundo Dev", "perfil": "dev",
        })
        outro_id = r.json()["id"]
        tid = None
        try:
            resp = dev_client.post("/api/dev-tarefas", json={"titulo": "Pra outro dev", "atribuido_a": outro_id})
            assert resp.status_code == 200, resp.text
            tid = resp.json()["id"]
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["atribuido_a"] == outro_id
            assert item["atribuido_a_nome"] == "Segundo Dev"
        finally:
            if tid:
                dev_client.delete(f"/api/dev-tarefas/{tid}")
            admin_client.delete(f"/api/usuarios/{outro_id}")

    def test_lista_usuarios_dev_para_atribuicao(self, dev_client):
        resp = dev_client.get("/api/dev-tarefas/usuarios")
        assert resp.status_code == 200
        nomes = [u["nome"] for u in resp.json()]
        assert "Dev de Teste" in nomes
