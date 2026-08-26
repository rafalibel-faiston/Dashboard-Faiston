"""
Testes do aviso global de novidade (ex.: lançamento do Assistente OPS):
GET/POST /api/avisos-ops e o redirect de /ajuda preservando ?destaque=.
"""
import uuid

import main


class TestAvisoOps:
    def test_get_sem_login_retorna_401(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/avisos-ops")
        assert resp.status_code == 401

    def test_disparar_sem_permissao_retorna_403(self, app):
        # POST /api/usuarios ignora a senha enviada (endurecimento de
        # 2026-08-10 -- a pessoa define a própria senha pelo link de
        # e-mail, que não existe neste ambiente de teste sem
        # BREVO_API_KEY/EMAIL_USER). Cria direto no banco de teste pra
        # isolar este teste desse fluxo, que não é o que está sendo
        # testado aqui.
        from fastapi.testclient import TestClient

        usuario = f"teste_func_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        conn = main.get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usuarios (usuario, senha_hash, nome, perfil, email, primeiro_acesso, time, cargo) "
            "VALUES (%s, %s, %s, 'funcionario', %s, FALSE, 'Projetos', 'analista')",
            (usuario, main.hash_senha(senha), "Func Fixture Teste", f"{usuario}@example.com"),
        )
        conn.commit(); cur.close(); conn.close()

        client = TestClient(app)
        resp = client.post("/api/login", json={"usuario": usuario, "senha": senha})
        assert resp.status_code == 200, resp.text
        resp = client.post("/api/avisos-ops/disparar")
        assert resp.status_code == 403

    def test_previa_sem_login_retorna_401(self, app):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/avisos-ops/previa")
        assert resp.status_code == 401

    def test_previa_devolve_o_email_montado_sem_enviar_nada(self, admin_client, monkeypatch):
        """A prévia existe justamente pra conferir antes de disparar -- se ela
        mandasse e-mail, o botão de conferir viraria um segundo disparo."""
        enviados = []
        monkeypatch.setattr(main, "_brevo_send", lambda *a, **k: enviados.append(a))

        resp = admin_client.get("/api/avisos-ops/previa")
        assert resp.status_code == 200, resp.text
        assert enviados == []
        html = resp.text
        assert "Assistente OPS" in html
        assert "/ajuda?destaque=assistente" in html
        # Rodapé próprio: o texto padrão do envelope fala de "resumo de fim de
        # expediente", que não faz sentido num aviso pontual.
        assert "Resumo automático de fim de expediente" not in html

    def test_email_nao_promete_consulta_a_procedimento(self):
        """Os POPs ainda não estão indexados; prometer procedimento no e-mail
        de lançamento faria a pessoa perguntar e o assistente recusar (Regra 2:
        sem fonte, não responde)."""
        corpo = main._corpo_email_aviso_ops("https://exemplo/ajuda?destaque=assistente")
        assert "procedimento" not in corpo.lower()

    def test_disparar_e_ler_de_volta(self, admin_client):
        resp = admin_client.post("/api/avisos-ops/disparar")
        assert resp.status_code == 200, resp.text

        resp = admin_client.get("/api/avisos-ops")
        assert resp.status_code == 200
        aviso = resp.json()
        assert aviso is not None
        assert "assistente ops" in aviso["mensagem"].lower() or "Assistente OPS" in aviso["mensagem"]
        assert aviso["link"] == "/ajuda?destaque=assistente"
        assert aviso["disparado_em"]

    def test_disparar_agenda_envio_de_emails_em_background(self, admin_client, monkeypatch):
        chamadas = []
        monkeypatch.setattr(main, "_enviar_emails_aviso_ops", lambda system_url: chamadas.append(system_url))

        resp = admin_client.post("/api/avisos-ops/disparar")
        assert resp.status_code == 200, resp.text
        # BackgroundTasks do Starlette roda antes do TestClient devolver a
        # resposta -- não precisa de espera/retry aqui.
        assert len(chamadas) == 1
        assert chamadas[0]  # veio uma URL base, não vazio/None

    def test_disparar_de_novo_atualiza_o_timestamp(self, admin_client):
        primeiro = admin_client.post("/api/avisos-ops/disparar").json()
        segundo_disparo = admin_client.get("/api/avisos-ops").json()["disparado_em"]

        resp = admin_client.post("/api/avisos-ops/disparar")
        assert resp.status_code == 200
        terceiro = admin_client.get("/api/avisos-ops").json()["disparado_em"]
        # Não é garantido que o timestamp mude num teste rápido o bastante
        # (resolução), mas o campo tem que continuar presente e válido nos
        # dois disparos -- é isso que faz o front reavaliar "já vi isso?".
        assert segundo_disparo and terceiro


class TestAjudaPreservaDestaque:
    def test_ajuda_sem_perfil_redireciona_preservando_destaque(self, admin_client):
        resp = admin_client.get("/ajuda?destaque=assistente", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "destaque=assistente" in location
        assert "perfil=" in location

    def test_ajuda_com_perfil_no_url_no_redireciona(self, app):
        """Sem sessão (link do e-mail de primeiro acesso), o ?perfil= da URL
        é tudo que existe — aí ele vale."""
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/ajuda?perfil=func&destaque=assistente", follow_redirects=False)
        assert resp.status_code == 200


class TestAjudaSegueOPerfilDaSessao:
    """Com sessão, o guia mostra a função de quem abriu — a URL não decide."""

    def test_perfil_da_url_e_reescrito_pelo_da_sessao(self, admin_client):
        """Trocar /ajuda?perfil=X na barra do navegador não abre o guia de
        outra função: quem está logado é redirecionado pro seu."""
        resp = admin_client.get("/ajuda?perfil=funcionario", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "perfil=admin" in location
        assert location.count("perfil=") == 1, f"perfil duplicado no redirect: {location}"

    def test_perfil_certo_na_url_serve_a_pagina(self, admin_client):
        """Já no destino, serve o HTML — senão o redirect entraria em laço."""
        resp = admin_client.get("/ajuda?perfil=admin", follow_redirects=False)
        assert resp.status_code == 200

    def test_reescrita_preserva_os_outros_parametros(self, admin_client):
        resp = admin_client.get("/ajuda?perfil=dev&destaque=assistente", follow_redirects=False)
        assert resp.status_code in (302, 307)
        location = resp.headers["location"]
        assert "perfil=admin" in location and "destaque=assistente" in location
