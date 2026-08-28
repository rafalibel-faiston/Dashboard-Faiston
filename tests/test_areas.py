"""
Testes do cadastro de áreas (/api/areas) e da regra "área sem projetos".

A área deixou de ser lista fixa no código (TIMES_VALIDOS) e virou cadastro:
o admin cria, renomeia e escolhe se a área trabalha por projeto. Quando
`usa_projetos` é FALSE, esconder o campo na tela não basta -- a API tem que
recusar os dois caminhos (tarefa com projeto_id e criação de projeto), que é
o que estes testes cobrem.

Roda contra um Postgres real de teste (ver tests/conftest.py).
"""
import uuid

import pytest


@pytest.fixture()
def area_factory(admin_client):
    """Cria áreas temporárias e derruba tudo no teardown."""
    criadas = []

    def criar(usa_projetos=True, nome=None):
        nome = nome or f"TESTE-AREA-{uuid.uuid4().hex[:6]}"
        resp = admin_client.post("/api/areas", json={"nome": nome, "usa_projetos": usa_projetos})
        assert resp.status_code == 200, resp.text
        criadas.append(resp.json()["id"])
        return {"id": resp.json()["id"], "nome": nome}

    yield criar
    for aid in criadas:
        admin_client.delete(f"/api/areas/{aid}")


@pytest.fixture()
def usuario_factory(admin_client, app):
    """Cria usuário numa área e devolve um TestClient já logado como ele."""
    criados = []

    def criar(time_nome, perfil="funcionario", cargo="analista"):
        from tests.conftest import login_client
        usuario = f"teste_area_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": f"Usuário {time_nome}",
            "email": f"{usuario}@exemplo.test", "perfil": perfil,
            "cargo": cargo if perfil == "funcionario" else "", "time": time_nome,
        })
        assert resp.status_code == 200, resp.text
        uid = resp.json()["id"]
        criados.append(uid)
        # POST /api/usuarios não define senha desde 2026-08-10 (a pessoa define
        # pelo link enviado por e-mail), então o PUT é o único caminho de API
        # pra deixar o usuário logável dentro do teste.
        resp = admin_client.put(f"/api/usuarios/{uid}", json={
            "nome": f"Usuário {time_nome}", "perfil": perfil, "senha": senha,
            "ativo": True, "email": f"{usuario}@exemplo.test", "time": time_nome,
            "cargo": cargo if perfil == "funcionario" else "",
        })
        assert resp.status_code == 200, resp.text
        return {"id": uid, "client": login_client(app, usuario, senha)}

    yield criar
    for uid in criados:
        admin_client.delete(f"/api/usuarios/{uid}")


def _tipo_atividade_id(client, area):
    """Primeiro tipo de atividade da área -- POST /api/tarefas exige um."""
    resp = client.get("/api/tipos-atividade", params={"area": area})
    assert resp.status_code == 200, resp.text
    tipos = resp.json()
    return tipos[0]["id"] if tipos else None


class TestCadastro:
    def test_listar_sem_login_retorna_401(self, app):
        from fastapi.testclient import TestClient
        assert TestClient(app).get("/api/areas").status_code == 401

    def test_areas_padrao_vem_do_seed(self, admin_client):
        nomes = [a["nome"] for a in admin_client.get("/api/areas").json()]
        for esperada in ("Projetos", "Logística", "Rede Credenciada", "Desenvolvimento"):
            assert esperada in nomes

    def test_criar_exige_admin(self, usuario_factory):
        client = usuario_factory("Projetos")["client"]
        resp = client.post("/api/areas", json={"nome": "TESTE-AREA-NEGADA"})
        assert resp.status_code == 403

    def test_criar_e_listar(self, admin_client, area_factory):
        area = area_factory(usa_projetos=False)
        listada = next(a for a in admin_client.get("/api/areas").json() if a["id"] == area["id"])
        assert listada["nome"] == area["nome"]
        assert listada["usa_projetos"] is False

    def test_nome_duplicado_recusado(self, admin_client, area_factory):
        area = area_factory()
        resp = admin_client.post("/api/areas", json={"nome": area["nome"]})
        assert resp.status_code == 400

    def test_nome_vazio_recusado(self, admin_client):
        assert admin_client.post("/api/areas", json={"nome": "   "}).status_code == 400

    def test_area_nova_aceita_usuario_e_frente(self, admin_client, area_factory, usuario_factory):
        # Estes dois eram exatamente o que a lista fixa travava: sem entrar em
        # TIMES_VALIDOS, o usuário caía pra "Projetos" e a frente dava 400.
        area = area_factory()
        uid = usuario_factory(area["nome"])["id"]
        atual = next(u for u in admin_client.get("/api/usuarios").json() if u["id"] == uid)
        assert atual["time"] == area["nome"]

        resp = admin_client.post("/api/frentes", json={"area": area["nome"], "nome": "Backoffice"})
        assert resp.status_code == 200, resp.text

    def test_frente_em_area_inexistente_recusada(self, admin_client):
        resp = admin_client.post("/api/frentes", json={"area": "Área Que Não Existe", "nome": "X"})
        assert resp.status_code == 400


class TestRenomear:
    def test_rename_propaga_para_o_usuario(self, admin_client, area_factory, usuario_factory):
        area = area_factory()
        uid = usuario_factory(area["nome"])["id"]
        novo = f"{area['nome']}-RENOMEADA"
        resp = admin_client.put(f"/api/areas/{area['id']}",
                                json={"nome": novo, "usa_projetos": True, "ativo": True})
        assert resp.status_code == 200, resp.text
        atual = next(u for u in admin_client.get("/api/usuarios").json() if u["id"] == uid)
        assert atual["time"] == novo

    def test_rename_para_nome_existente_recusado(self, admin_client, area_factory):
        area = area_factory()
        resp = admin_client.put(f"/api/areas/{area['id']}",
                                json={"nome": "Projetos", "usa_projetos": True, "ativo": True})
        assert resp.status_code == 400

    def test_area_inexistente_retorna_404(self, admin_client):
        resp = admin_client.put("/api/areas/99999999",
                                json={"nome": "Qualquer", "usa_projetos": True, "ativo": True})
        assert resp.status_code == 404


class TestDesativar:
    def test_area_com_usuario_ativo_nao_desativa(self, admin_client, area_factory, usuario_factory):
        area = area_factory()
        usuario_factory(area["nome"])
        resp = admin_client.delete(f"/api/areas/{area['id']}")
        assert resp.status_code == 400
        assert "usuário" in resp.json()["detail"]

    def test_area_vazia_desativa_e_some_da_listagem(self, admin_client, area_factory):
        area = area_factory()
        assert admin_client.delete(f"/api/areas/{area['id']}").status_code == 200
        assert area["id"] not in [a["id"] for a in admin_client.get("/api/areas").json()]
        # admin ainda enxerga a inativa com ?todas=1 (é o que a tela de cadastro usa)
        todas = admin_client.get("/api/areas", params={"todas": True}).json()
        assert area["id"] in [a["id"] for a in todas]


class TestAreaSemProjetos:
    def test_tarefa_com_projeto_recusada(self, admin_client, area_factory, usuario_factory, cliente_teste):
        area = area_factory(usa_projetos=False)
        admin_client.post("/api/frentes", json={"area": area["nome"], "nome": "Backoffice"})
        resp = admin_client.post("/api/tipos-atividade", json={
            "frente_id": [f["id"] for f in admin_client.get("/api/frentes").json()
                          if f["area"] == area["nome"]][0],
            "nome": "Atividade de teste", "peso": 2, "descricao": "",
        })
        assert resp.status_code == 200, resp.text
        tipo_id = resp.json()["id"]

        # projeto de uma área que trabalha por projeto (Projetos), pra ter um id válido
        proj = admin_client.post("/api/projetos", json={"nome": f"TESTE-PROJ-{uuid.uuid4().hex[:6]}",
                                                        "time": "Projetos"})
        assert proj.status_code == 200, proj.text
        pid = proj.json()["id"]

        try:
            usuario = usuario_factory(area["nome"])
            base = {"descricao": "Tarefa de teste", "cliente": "Demandas Gerais",
                    "prioridade": "Media", "status": "aberto", "segundos": 0,
                    "tipo_atividade_id": tipo_id, "data_prazo": "2026-12-31"}

            recusada = usuario["client"].post("/api/tarefas", json={**base, "projeto_id": pid})
            assert recusada.status_code == 400, recusada.text
            assert "não trabalha por projeto" in recusada.json()["detail"]

            # sem projeto (demanda avulsa) continua funcionando normalmente
            aceita = usuario["client"].post("/api/tarefas", json=base)
            assert aceita.status_code == 200, aceita.text
            usuario["client"].delete(f"/api/tarefas/{aceita.json()['id']}")
        finally:
            admin_client.delete(f"/api/projetos/{pid}")

    def test_opcoes_tarefa_nao_oferece_projeto(self, area_factory, usuario_factory):
        area = area_factory(usa_projetos=False)
        client = usuario_factory(area["nome"])["client"]
        resp = client.get("/api/opcoes-tarefa")
        assert resp.status_code == 200, resp.text
        assert resp.json()["projetos"] == []

    def test_gestor_da_area_nao_cria_projeto(self, area_factory, usuario_factory):
        area = area_factory(usa_projetos=False)
        client = usuario_factory(area["nome"], perfil="gestor")["client"]
        resp = client.post("/api/projetos", json={"nome": f"TESTE-PROJ-{uuid.uuid4().hex[:6]}"})
        assert resp.status_code == 400
        assert "não trabalha por projeto" in resp.json()["detail"]

    def test_admin_tambem_nao_cria_projeto_para_a_area(self, admin_client, area_factory):
        area = area_factory(usa_projetos=False)
        resp = admin_client.post("/api/projetos", json={
            "nome": f"TESTE-PROJ-{uuid.uuid4().hex[:6]}", "time": area["nome"]})
        assert resp.status_code == 400

    def test_area_com_projeto_ativo_nao_vira_area_sem_projetos(self, admin_client, area_factory):
        area = area_factory(usa_projetos=True)
        proj = admin_client.post("/api/projetos", json={
            "nome": f"TESTE-PROJ-{uuid.uuid4().hex[:6]}", "time": area["nome"]})
        assert proj.status_code == 200, proj.text
        pid = proj.json()["id"]
        try:
            resp = admin_client.put(f"/api/areas/{area['id']}",
                                    json={"nome": area["nome"], "usa_projetos": False, "ativo": True})
            assert resp.status_code == 400
            assert "projeto(s) ativo(s)" in resp.json()["detail"]
        finally:
            admin_client.delete(f"/api/projetos/{pid}")
        # arquivado o projeto, a virada passa
        resp = admin_client.put(f"/api/areas/{area['id']}",
                                json={"nome": area["nome"], "usa_projetos": False, "ativo": True})
        assert resp.status_code == 200, resp.text

    def test_area_com_projetos_segue_aceitando_vinculo(self, admin_client, area_factory, usuario_factory):
        """Contraprova: a regra só morde quem tem usa_projetos=False."""
        area = area_factory(usa_projetos=True)
        client = usuario_factory(area["nome"], perfil="gestor")["client"]
        resp = client.post("/api/projetos", json={"nome": f"TESTE-PROJ-{uuid.uuid4().hex[:6]}"})
        assert resp.status_code == 200, resp.text
        admin_client.delete(f"/api/projetos/{resp.json()['id']}")
