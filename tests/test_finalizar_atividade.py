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
                "materiais": [{"descricao": "não deveria salvar isso", "quantidade": 9, "valor": 50.0}],
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "concluido"
            assert item["hora_termino"] == "17:45"
            assert item["material_utilizado"] is False
            assert item["materiais"] == []
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_finaliza_com_material_salva_lista_de_materiais(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "parcial", "hora_termino": "18:00", "material_utilizado": True,
                "materiais": [{"descricao": "Patch cord", "quantidade": 2, "unidade": "unidade", "valor": 40.0}],
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["status"] == "parcial"
            assert item["material_utilizado"] is True
            assert len(item["materiais"]) == 1
            assert item["materiais"][0]["descricao"] == "Patch cord"
            assert item["materiais"][0]["quantidade"] == 2
            assert item["materiais"][0]["unidade"] == "unidade"
            assert item["materiais"][0]["valor"] == 40.0
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_finaliza_com_varios_materiais_e_unidade_de_medida(self, admin_client, cliente_teste):
        # Ponto de feedback: quantidade nem sempre é contagem de item --
        # ex. "28" com unidade "metro" pra cabo de rede -- e uma visita pode
        # usar mais de um material ao mesmo tempo.
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "hora_termino": "18:00", "material_utilizado": True,
                "materiais": [
                    {"descricao": "Cabo de rede cat6", "quantidade": 28, "unidade": "metro", "valor": 84.0},
                    {"descricao": "Conector RJ45", "quantidade": 4, "unidade": "unidade", "valor": 8.0},
                ],
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert len(item["materiais"]) == 2
            cabo = next(m for m in item["materiais"] if m["descricao"] == "Cabo de rede cat6")
            assert cabo["quantidade"] == 28
            assert cabo["unidade"] == "metro"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_unidade_invalida_vira_unidade_padrao(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "hora_termino": "18:00", "material_utilizado": True,
                "materiais": [{"descricao": "Algo", "unidade": "valor-invalido"}],
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert item["materiais"][0]["unidade"] == "unidade"
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_material_utilizado_sem_nenhum_item_retorna_400(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "hora_termino": "18:00", "material_utilizado": True,
            })
            assert resp.status_code == 400
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_reenviar_finalizacao_substitui_lista_de_materiais(self, admin_client, cliente_teste):
        aid = admin_client.post("/api/status-campo", json=_payload(cliente_teste)).json()["id"]
        try:
            admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "parcial", "hora_termino": "17:00", "material_utilizado": True,
                "materiais": [{"descricao": "Item antigo", "quantidade": 1}],
            })
            resp = admin_client.patch(f"/api/status-campo/{aid}/status", json={
                "status": "concluido", "hora_termino": "18:00", "material_utilizado": True,
                "materiais": [{"descricao": "Item novo", "quantidade": 3}],
            })
            assert resp.status_code == 200, resp.text
            item = admin_client.get(f"/api/status-campo/{aid}").json()
            assert len(item["materiais"]) == 1
            assert item["materiais"][0]["descricao"] == "Item novo"
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
