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


class TestCamposDePlanejamento:
    def test_cria_com_prioridade_prazo_tags_e_link(self, dev_client):
        resp = dev_client.post("/api/dev-tarefas", json={
            "titulo": "Tarefa completa", "prioridade": "urgente", "prazo": "2026-08-15",
            "tags": ["bug", " backend ", ""], "link": "https://github.com/x/y/pull/1",
        })
        assert resp.status_code == 200, resp.text
        tid = resp.json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["prioridade"] == "urgente"
            assert item["prazo"] == "2026-08-15"
            assert item["tags"] == ["bug", "backend"]
            assert item["link"] == "https://github.com/x/y/pull/1"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_prioridade_invalida_cai_para_media(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Teste", "prioridade": "nao-existe"}).json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["prioridade"] == "media"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_status_backlog_e_aceito(self, dev_client):
        resp = dev_client.post("/api/dev-tarefas", json={"titulo": "No backlog", "status": "backlog"})
        assert resp.status_code == 200
        tid = resp.json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["status"] == "backlog"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_prazo_vazio_vira_null(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Sem prazo", "prazo": ""}).json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["prazo"] is None
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")


class TestComentarios:
    @pytest.fixture()
    def tarefa(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Tarefa com comentários"}).json()["id"]
        yield tid
        dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_cria_e_lista_comentario(self, dev_client, tarefa):
        resp = dev_client.post(f"/api/dev-tarefas/{tarefa}/comentarios", json={"texto": "Primeiro comentário"})
        assert resp.status_code == 200, resp.text
        coments = dev_client.get(f"/api/dev-tarefas/{tarefa}/comentarios").json()
        assert len(coments) == 1
        assert coments[0]["texto"] == "Primeiro comentário"
        assert coments[0]["usuario_nome"] == "Dev de Teste"

    def test_comentario_vazio_e_rejeitado(self, dev_client, tarefa):
        resp = dev_client.post(f"/api/dev-tarefas/{tarefa}/comentarios", json={"texto": "   "})
        assert resp.status_code == 400

    def test_comentario_em_tarefa_inexistente(self, dev_client):
        resp = dev_client.post("/api/dev-tarefas/999999/comentarios", json={"texto": "oi"})
        assert resp.status_code == 404

    def test_deleta_comentario(self, dev_client, tarefa):
        cid = dev_client.post(f"/api/dev-tarefas/{tarefa}/comentarios", json={"texto": "Apagar"}).json()["id"]
        resp = dev_client.delete(f"/api/dev-tarefas/comentarios/{cid}")
        assert resp.status_code == 200
        assert dev_client.get(f"/api/dev-tarefas/{tarefa}/comentarios").json() == []

    def test_contador_de_comentarios_aparece_na_listagem(self, dev_client, tarefa):
        dev_client.post(f"/api/dev-tarefas/{tarefa}/comentarios", json={"texto": "Um"})
        dev_client.post(f"/api/dev-tarefas/{tarefa}/comentarios", json={"texto": "Dois"})
        item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tarefa)
        assert item["comentarios_total"] == 2


class TestChecklist:
    @pytest.fixture()
    def tarefa(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Tarefa com checklist"}).json()["id"]
        yield tid
        dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_cria_e_lista_item(self, dev_client, tarefa):
        resp = dev_client.post(f"/api/dev-tarefas/{tarefa}/checklist", json={"texto": "Escrever testes"})
        assert resp.status_code == 200, resp.text
        itens = dev_client.get(f"/api/dev-tarefas/{tarefa}/checklist").json()
        assert len(itens) == 1
        assert itens[0]["texto"] == "Escrever testes"
        assert itens[0]["concluido"] is False

    def test_marca_item_como_concluido(self, dev_client, tarefa):
        iid = dev_client.post(f"/api/dev-tarefas/{tarefa}/checklist", json={"texto": "Item"}).json()["id"]
        resp = dev_client.put(f"/api/dev-tarefas/checklist/{iid}", json={"texto": "Item", "concluido": True})
        assert resp.status_code == 200
        itens = dev_client.get(f"/api/dev-tarefas/{tarefa}/checklist").json()
        assert itens[0]["concluido"] is True

    def test_deleta_item(self, dev_client, tarefa):
        iid = dev_client.post(f"/api/dev-tarefas/{tarefa}/checklist", json={"texto": "Apagar"}).json()["id"]
        resp = dev_client.delete(f"/api/dev-tarefas/checklist/{iid}")
        assert resp.status_code == 200
        assert dev_client.get(f"/api/dev-tarefas/{tarefa}/checklist").json() == []

    def test_contadores_de_checklist_aparecem_na_listagem(self, dev_client, tarefa):
        i1 = dev_client.post(f"/api/dev-tarefas/{tarefa}/checklist", json={"texto": "Um"}).json()["id"]
        dev_client.post(f"/api/dev-tarefas/{tarefa}/checklist", json={"texto": "Dois"})
        dev_client.put(f"/api/dev-tarefas/checklist/{i1}", json={"texto": "Um", "concluido": True})
        item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tarefa)
        assert item["checklist_total"] == 2
        assert item["checklist_concluidos"] == 1

    def test_item_vazio_e_rejeitado(self, dev_client, tarefa):
        resp = dev_client.post(f"/api/dev-tarefas/{tarefa}/checklist", json={"texto": "  "})
        assert resp.status_code == 400
