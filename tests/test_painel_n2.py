"""
Testes do Painel de Controle do N2: escala do dia (/api/escala-n2) e
resumo da equipe (/api/painel-n2/resumo).
"""
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
            assert item["n2_nome"] == "N2 Fixture Teste"
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


class TestAtribuirLote:
    """
    Cobre o bug de raiz do item 4 do feedback: gerar a escala criava só um
    registro de plantão (escala_n2) sem de fato vincular o N2 às atividades
    -- por isso elas nunca apareciam na aba do N2. Esse endpoint é o que o
    Gerador de Escala chama pra fazer o vínculo real em status_atividades.
    """

    def test_sem_permissao_retorna_403(self, app, n2_user, cliente_teste):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/login", json={"usuario": n2_user["usuario"], "senha": n2_user["senha"]})
        assert resp.status_code == 200
        resp = client.post("/api/status-campo/atribuir-lote", json={
            "cliente_id": cliente_teste, "data": "2026-07-21", "quantidade": 1, "n2_usuario_id": n2_user["id"],
        })
        assert resp.status_code == 403

    def test_atribui_apenas_as_sem_n2_ate_a_quantidade_pedida(self, admin_client, n2_user, cliente_teste):
        data = "2026-07-21"
        # 3 atividades sem N2 + 1 já com N2 definido (não deve ser tocada)
        sem_n2 = [
            admin_client.post("/api/status-campo", json={"cliente_id": cliente_teste, "data": data}).json()["id"]
            for _ in range(3)
        ]
        ja_atribuida = admin_client.post("/api/status-campo", json={
            "cliente_id": cliente_teste, "data": data, "n2_usuario_id": n2_user["id"],
        }).json()["id"]
        todas = sem_n2 + [ja_atribuida]
        try:
            resp = admin_client.post("/api/status-campo/atribuir-lote", json={
                "cliente_id": cliente_teste, "data": data, "quantidade": 2, "n2_usuario_id": n2_user["id"],
            })
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["sucesso"] is True
            assert body["atribuidas"] == 2
            assert len(body["ids"]) == 2
            # as duas atribuídas têm que vir das que estavam sem N2
            assert set(body["ids"]).issubset(set(sem_n2))

            atualizadas = {aid: admin_client.get(f"/api/status-campo/{aid}").json() for aid in todas}
            com_n2_agora = [aid for aid, item in atualizadas.items() if item["n2_usuario_id"] == n2_user["id"]]
            # as 2 recém-atribuídas + a que já estava atribuída antes
            assert len(com_n2_agora) == 3
            assert ja_atribuida in com_n2_agora
        finally:
            for aid in todas:
                admin_client.delete(f"/api/status-campo/{aid}")

    def test_atividade_atribuida_aparece_para_o_n2(self, admin_client, app, n2_user, cliente_teste):
        """Prova fim-a-fim do fix: depois do atribuir-lote, a atividade
        aparece na listagem do próprio N2 (GET /api/status-campo?n2_usuario_id=)."""
        data = "2026-07-21"
        aid = admin_client.post("/api/status-campo", json={"cliente_id": cliente_teste, "data": data}).json()["id"]
        try:
            resp = admin_client.post("/api/status-campo/atribuir-lote", json={
                "cliente_id": cliente_teste, "data": data, "quantidade": 1, "n2_usuario_id": n2_user["id"],
            })
            assert resp.status_code == 200, resp.text
            assert resp.json()["atribuidas"] == 1

            from fastapi.testclient import TestClient
            n2_client = TestClient(app)
            resp = n2_client.post("/api/login", json={"usuario": n2_user["usuario"], "senha": n2_user["senha"]})
            assert resp.status_code == 200
            resp = n2_client.get("/api/status-campo", params={"n2_usuario_id": n2_user["id"]})
            assert resp.status_code == 200
            assert aid in [r["id"] for r in resp.json()]
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")

    def test_quantidade_maior_que_disponivel_atribui_so_as_existentes(self, admin_client, n2_user, cliente_teste):
        data = "2026-07-21"
        aid = admin_client.post("/api/status-campo", json={"cliente_id": cliente_teste, "data": data}).json()["id"]
        try:
            resp = admin_client.post("/api/status-campo/atribuir-lote", json={
                "cliente_id": cliente_teste, "data": data, "quantidade": 5, "n2_usuario_id": n2_user["id"],
            })
            assert resp.status_code == 200, resp.text
            assert resp.json()["atribuidas"] == 1
        finally:
            admin_client.delete(f"/api/status-campo/{aid}")
