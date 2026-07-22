"""
Testes da segunda rodada de ajustes: "em_andamento" exige uma descrição do
que está acontecendo, e os status terminais (concluido/parcial/improdutiva_*)
exigem confirmar hora de saída + se houve material, em vez de trocar o
status silenciosamente sem capturar esses dados.
"""


def _payload(cliente_id, **overrides):
    base = {"cliente_id": cliente_id, "data": "2026-07-21"}
    base.update(overrides)
    return base


class TestEmAndamentoExigeDescricao:
    def test_sem_descricao_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={"status": "em_andamento"})
            assert resp.status_code == 400
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "agendado"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_descricao_em_branco_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "em_andamento", "andamento_descricao": "   ",
            })
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_com_descricao_e_salva(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "em_andamento", "andamento_descricao": "Aguardando liberação de acesso ao rack",
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "em_andamento"
            assert item["andamento_descricao"] == "Aguardando liberação de acesso ao rack"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")


class TestFinalizarExigeConfirmacao:
    def test_sem_hora_termino_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "material_utilizado": False,
            })
            assert resp.status_code == 400
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "agendado"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_sem_indicar_material_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "hora_termino": "17:30",
            })
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_finaliza_sem_material_limpa_campos_de_material(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "hora_termino": "17:45", "material_utilizado": False,
                "material_detalhe": "não deveria salvar isso", "material_quantidade": 9, "material_valor": 50.0,
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "concluido"
            assert item["hora_termino"] == "17:45"
            assert item["material_utilizado"] is False
            assert item["material_detalhe"] == ""
            assert item["material_quantidade"] is None
            assert item["material_valor"] is None
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_finaliza_com_material_salva_detalhe_quantidade_e_valor(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "parcial", "hora_termino": "18:00", "material_utilizado": True,
                "material_detalhe": "Patch cord 3m", "material_quantidade": 2, "material_valor": 40.0,
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "parcial"
            assert item["material_utilizado"] is True
            assert item["material_detalhe"] == "Patch cord 3m"
            assert item["material_quantidade"] == 2
            assert item["material_valor"] == 40.0
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_status_nao_terminal_nao_exige_confirmacao(self, admin_client, cliente_teste):
        # voltar pra "agendado" (ex.: reabrir) não deve exigir nenhum campo extra
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={"status": "agendado"})
            assert resp.status_code == 200, resp.text
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")
