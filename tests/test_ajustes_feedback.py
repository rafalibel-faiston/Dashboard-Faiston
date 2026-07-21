"""
Testes dos ajustes pedidos após a primeira rodada de testes manuais:
status trava em "agendado" na criação, particularidades, material,
status improdutiva_cliente/improdutiva_faiston, e acesso de diretor.
"""
import uuid

import pytest


def _payload(cliente_id, **overrides):
    base = {
        "cliente_id": cliente_id,
        "data": "2026-07-21",
        "status": "agendado",
    }
    base.update(overrides)
    return base


class TestStatusTravaNaCriacao:
    def test_status_enviado_e_ignorado_na_criacao(self, admin_client, cliente_teste):
        resp = admin_client.post("/api/status-campo", json=_payload(cliente_teste, status="concluido"))
        assert resp.status_code == 200, resp.text
        aid = resp.json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "agendado"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_status_pode_ser_alterado_depois_via_edicao(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.put(f"/api/status-campo/{aid}", json=_payload(cliente_teste, status="improdutiva_cliente"))
            assert resp.status_code == 200
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "improdutiva_cliente"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestStatusImprodutivaSplit:
    @pytest.mark.parametrize("status", ["improdutiva_cliente", "improdutiva_faiston"])
    def test_ambos_valores_de_improdutiva_sao_aceitos(self, admin_client, cliente_teste, status):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={"status": status})
            assert resp.status_code == 200
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == status
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_valor_antigo_improdutiva_sem_sufixo_e_invalido(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={"status": "improdutiva"})
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestParticularidadesEMaterial:
    def test_particularidades_validas_sao_salvas(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, particularidades=["reversa", "equipamento_em_posse_do_cliente"]
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert set(item["particularidades"]) == {"reversa", "equipamento_em_posse_do_cliente"}
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_particularidade_invalida_e_filtrada_silenciosamente(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, particularidades=["reversa", "valor-que-nao-existe"]
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["particularidades"] == ["reversa"]
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_sem_particularidades_retorna_lista_vazia(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["particularidades"] == []
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_material_utilizado_com_detalhe(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, material_utilizado=True, material_detalhe="Patch cord 2m"
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["material_utilizado"] is True
            assert item["material_detalhe"] == "Patch cord 2m"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_material_nao_utilizado_ignora_detalhe(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, material_utilizado=False, material_detalhe="não deveria salvar isso"
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["material_utilizado"] is False
            assert item["material_detalhe"] == ""
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_material_utilizado_com_quantidade_e_valor(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, material_utilizado=True, material_detalhe="Cabo cat6",
            material_quantidade=5, material_valor=123.45,
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["material_quantidade"] == 5
            assert item["material_valor"] == 123.45
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_material_nao_utilizado_ignora_quantidade_e_valor(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(
            cliente_teste, material_utilizado=False, material_quantidade=5, material_valor=123.45,
        )).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["material_quantidade"] is None
            assert item["material_valor"] is None
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestTicket:
    def test_ticket_e_salvo_e_pode_ser_editado(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste, ticket="TCK-1001")).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["ticket"] == "TCK-1001"

            resp = admin_client.put(f"/api/status-campo/{aid}", json=_payload(cliente_teste, ticket="TCK-2002"))
            assert resp.status_code == 200
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["ticket"] == "TCK-2002"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_ticket_default_vazio(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["ticket"] == ""
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestAcessoDiretor:
    @pytest.fixture()
    def diretor_client(self, admin_client, app):
        from fastapi.testclient import TestClient
        usuario = f"teste_diretor_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Diretor de Teste", "perfil": "diretor",
        })
        assert resp.status_code == 200, resp.text
        uid = resp.json()["id"]
        client = TestClient(app)
        resp = client.post("/api/login", json={"usuario": usuario, "senha": senha})
        assert resp.status_code == 200
        yield client
        admin_client.delete(f"/api/usuarios/{uid}")

    def test_diretor_pode_criar_atividade(self, diretor_client, cliente_teste):
        resp = diretor_client.post("/api/status-campo", json=_payload(cliente_teste))
        assert resp.status_code == 200, resp.text
        diretor_client.delete(f"/api/status-campo/{resp.json()['id']}")

    def test_diretor_pode_ver_painel_n2_resumo(self, diretor_client):
        resp = diretor_client.get("/api/painel-n2/resumo")
        assert resp.status_code == 200

    def test_diretor_pode_criar_escala(self, diretor_client, admin_client):
        usuario = f"teste_n2_dir_{uuid.uuid4().hex[:8]}"
        r = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": "senhaTeste123", "nome": "N2 pra Diretor", "perfil": "n2",
        })
        uid = r.json()["id"]
        try:
            resp = diretor_client.post("/api/escala-n2", json={"data": "2026-07-21", "n2_usuario_id": uid})
            assert resp.status_code == 200, resp.text
            diretor_client.delete(f"/api/escala-n2/{resp.json()['id']}")
        finally:
            admin_client.delete(f"/api/usuarios/{uid}")
