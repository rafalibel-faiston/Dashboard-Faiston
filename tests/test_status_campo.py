"""
Testes do módulo Status de Campo (/api/status-campo).

Roda contra um Postgres real de teste (ver tests/conftest.py) — sem
mocks, porque o app inteiro usa psycopg2 direto sem camada trocável.
"""
import uuid

import pytest


def _payload(cliente_id, **overrides):
    base = {
        "cliente_id": cliente_id,
        "data": "2026-07-20",
        "horario_agendado": "06:00",
        "tecnico": "Técnico de Teste",
        "n2_responsavel": "N2 de Teste",
        "site_sigla": "TST",
        "site_nome": "Site de Teste",
        "endereco": "Rua de Teste, 123",
        "cidade": "Sao Paulo",
        "uf": "sp",
        "status": "agendado",
        "observacoes": "criado pela suíte de testes",
    }
    base.update(overrides)
    return base


class TestAutenticacao:
    def test_listar_sem_login_retorna_401(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/status-campo")
        assert resp.status_code == 401

    def test_criar_sem_login_retorna_403(self, app, cliente_teste):
        # Rotas de escrita usam `if not sess or perfil not in (...)` num só
        # check (mesmo padrão de main.py:3439 em Projetos) — não-autenticado
        # cai no mesmo 403 de não-autorizado, não 401. Consistente de propósito.
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/status-campo", json=_payload(cliente_teste))
        assert resp.status_code == 403


class TestCRUD:
    def test_criar_listar_editar_e_excluir(self, admin_client, cliente_teste):
        # criar
        resp = admin_client.post("/api/status-campo", json=_payload(cliente_teste))
        assert resp.status_code == 200, resp.text
        aid = resp.json()["id"]
        assert isinstance(aid, int)

        try:
            # listar (filtrando pela data usada) deve conter o registro criado
            resp = admin_client.get("/api/status-campo", params={"data": "2026-07-20"})
            assert resp.status_code == 200
            ids = [r["id"] for r in resp.json()]
            assert aid in ids

            # buscar por id
            resp = admin_client.get(f"/api/status-campo/{aid}")
            assert resp.status_code == 200
            item = resp.json()
            assert item["tecnico"] == "Técnico de Teste"
            assert item["status"] == "agendado"
            assert item["cliente_id"] == cliente_teste

            # editar (PUT completo)
            resp = admin_client.put(f"/api/status-campo/{aid}", json=_payload(cliente_teste, status="concluido", tecnico="Outro Técnico"))
            assert resp.status_code == 200
            resp = admin_client.get(f"/api/status-campo/{aid}")
            assert resp.json()["status"] == "concluido"
            assert resp.json()["tecnico"] == "Outro Técnico"

            # patch de status isolado
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={"status": "parcial"})
            assert resp.status_code == 200
            resp = admin_client.get(f"/api/status-campo/{aid}")
            assert resp.json()["status"] == "parcial"
        finally:
            resp = admin_client.delete(f"/api/status-campo/{aid}")
            assert resp.status_code == 200

        # confirma que sumiu
        resp = admin_client.get(f"/api/status-campo/{aid}")
        assert resp.status_code == 404

    def test_status_invalido_cai_para_agendado(self, admin_client, cliente_teste):
        resp = admin_client.post("/api/status-campo", json=_payload(cliente_teste, status="status-que-nao-existe"))
        assert resp.status_code == 200
        aid = resp.json()["id"]
        try:
            resp = admin_client.get(f"/api/status-campo/{aid}")
            assert resp.json()["status"] == "agendado"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_patch_status_invalido_retorna_400(self, admin_client, cliente_teste):
        resp = admin_client.post("/api/status-campo", json=_payload(cliente_teste))
        aid = resp.json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={"status": "nao-existe"})
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_buscar_id_inexistente_retorna_404(self, admin_client):
        resp = admin_client.get("/api/status-campo/999999999")
        assert resp.status_code == 404


class TestFiltrosEBusca:
    def test_filtro_por_status(self, admin_client, cliente_teste):
        r1 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, status="concluido")).json()["id"]
        r2 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, status="cancelado")).json()["id"]
        try:
            resp = admin_client.get("/api/status-campo", params={"data": "2026-07-20", "cliente_id": cliente_teste, "status": "concluido"})
            ids = [r["id"] for r in resp.json()]
            assert r1 in ids
            assert r2 not in ids
        finally:
            admin_client.delete(f"/api/status-campo/{r1}")
            admin_client.delete(f"/api/status-campo/{r2}")

    def test_filtro_texto_busca_tecnico_e_site(self, admin_client, cliente_teste):
        marcador = uuid.uuid4().hex[:8]
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste, tecnico=f"Fulano-{marcador}")).json()["id"]
        try:
            resp = admin_client.get("/api/status-campo", params={"texto": marcador})
            ids = [r["id"] for r in resp.json()]
            assert aid in ids
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_intervalo_de_datas(self, admin_client, cliente_teste):
        dentro = admin_client.post("/api/status-campo", json=_payload(cliente_teste, data="2026-07-15")).json()["id"]
        fora = admin_client.post("/api/status-campo", json=_payload(cliente_teste, data="2026-01-01")).json()["id"]
        try:
            resp = admin_client.get("/api/status-campo", params={"data_de": "2026-07-01", "data_ate": "2026-07-31", "cliente_id": cliente_teste})
            ids = [r["id"] for r in resp.json()]
            assert dentro in ids
            assert fora not in ids
        finally:
            admin_client.delete(f"/api/status-campo/{dentro}")
            admin_client.delete(f"/api/status-campo/{fora}")


class TestReport:
    def test_report_agrupa_por_cliente_e_conta_status(self, admin_client, cliente_teste):
        data = "2026-07-19"
        a1 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, data=data, status="concluido")).json()["id"]
        a2 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, data=data, status="concluido")).json()["id"]
        a3 = admin_client.post("/api/status-campo", json=_payload(cliente_teste, data=data, status="parcial")).json()["id"]
        try:
            resp = admin_client.get("/api/status-campo/report", params={"data": data})
            assert resp.status_code == 200
            body = resp.json()
            assert body["contagem"]["concluido"] == 2
            assert body["contagem"]["parcial"] == 1
            nomes_clientes = list(body["por_cliente"].keys())
            assert len(nomes_clientes) == 1
            assert len(body["por_cliente"][nomes_clientes[0]]) == 3
        finally:
            for aid in (a1, a2, a3):
                admin_client.delete(f"/api/status-campo/{aid}")

    def test_report_rota_nao_e_capturada_pela_rota_de_id(self, admin_client):
        """Regressão: /api/status-campo/report não pode ser interpretada
        como /api/status-campo/{aid} (aid teria que ser int, 'report' não é).
        Se a ordem das rotas em main.py for trocada de novo, isso quebra com 422."""
        resp = admin_client.get("/api/status-campo/report", params={"data": "2026-07-20"})
        assert resp.status_code == 200


class TestPermissoes:
    @pytest.fixture()
    def funcionario_client(self, admin_client, app):
        """Cria um usuário perfil=funcionario temporário e retorna um client logado como ele."""
        from fastapi.testclient import TestClient
        usuario = f"teste_func_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Funcionário de Teste", "perfil": "funcionario",
        })
        assert resp.status_code == 200, resp.text
        uid = resp.json()["id"]

        client = TestClient(app)
        resp = client.post("/api/login", json={"usuario": usuario, "senha": senha})
        assert resp.status_code == 200, resp.text

        yield client
        admin_client.delete(f"/api/usuarios/{uid}")

    def test_funcionario_nao_pode_criar_despacho(self, funcionario_client, cliente_teste):
        resp = funcionario_client.post("/api/status-campo", json=_payload(cliente_teste))
        assert resp.status_code == 403

    def test_funcionario_pode_listar_despachos(self, funcionario_client):
        # leitura é permitida pra qualquer perfil autenticado
        resp = funcionario_client.get("/api/status-campo")
        assert resp.status_code == 200
