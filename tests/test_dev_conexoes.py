"""Conexões do Kanban da Área de Dev: webhooks de saída/entrada, GitHub, calendário e Outlook.

Nada aqui fala com a internet: o HTTP de saída (eventos._http_post), a API do GitHub (github._get) e o
Microsoft Graph (graph._req) são trocados por falsos; a thread de eventos roda síncrona."""
import json
import time
import uuid
from urllib.parse import quote

import pytest

from app.devkanban import eventos, github, graph


def _mk(c, titulo, **kw):
    r = c.post("/api/dev-tarefas", json={"titulo": titulo, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _get(c, tid):
    return next(t for t in c.get("/api/dev-tarefas").json() if t["id"] == tid)


@pytest.fixture()
def rede(monkeypatch):
    """Eventos síncronos, sem rede real; guarda o que seria enviado. Libera http/IP privado só pro cadastro de teste."""
    class R:
        posts, status_resposta = [], 200
        def para(self, url):
            """Só os envios pra essa URL (o banco compartilhado pode ter outros webhooks de saída ativos)."""
            return [p for p in self.posts if p["url"] == url]
    r = R(); r.posts = []
    monkeypatch.setattr(eventos, "_executar", lambda fn: fn())
    monkeypatch.setattr(eventos, "PERMITIR_HTTP", True)
    monkeypatch.setattr(eventos, "PERMITIR_REDE_PRIVADA", True)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def _post(url, corpo, headers):
        r.posts.append({"url": url, "corpo": json.loads(corpo), "bruto": corpo, "headers": headers})
        return r.status_resposta
    monkeypatch.setattr(eventos, "_http_post", _post)
    return r


@pytest.fixture()
def anonimo(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


class TestAcesso:
    @pytest.mark.parametrize("metodo,caminho", [
        ("get", "/api/dev-integracoes/status"), ("get", "/api/dev-integracoes/hooks-entrada"),
        ("get", "/api/dev-integracoes/hooks-saida"), ("get", "/api/dev-integracoes/outlook/emails"),
    ])
    def test_so_dev(self, admin_client, anonimo, metodo, caminho):
        for c in (admin_client, anonimo):
            assert getattr(c, metodo)(caminho).status_code == 403

    def test_status(self, dev_client):
        d = dev_client.get("/api/dev-integracoes/status").json()
        assert set(d) == {"ia", "github", "graph"} and "token" in d["github"]


class TestUrlSegura:
    @pytest.mark.parametrize("url", [
        "http://exemplo.com/x", "javascript:alert(1)", "file:///etc/passwd", "ftp://a.com/x", "https://user:senha@exemplo.com/x",
        "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.5/x", "https://192.168.1.1/x", "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x", "", "https:///sem-host",
    ])
    def test_bloqueia(self, monkeypatch, url):
        monkeypatch.setattr(eventos, "PERMITIR_HTTP", False)
        monkeypatch.setattr(eventos, "PERMITIR_REDE_PRIVADA", False)
        assert eventos.url_segura(url)[0] is False

    def test_aceita_https_publico(self, monkeypatch):
        monkeypatch.setattr(eventos, "PERMITIR_HTTP", False)
        monkeypatch.setattr(eventos, "PERMITIR_REDE_PRIVADA", False)
        assert eventos.url_segura("https://8.8.8.8/hook")[0] is True

    def test_cadastro_rejeita_destino_interno(self, dev_client, monkeypatch):
        monkeypatch.setattr(eventos, "PERMITIR_HTTP", False)
        monkeypatch.setattr(eventos, "PERMITIR_REDE_PRIVADA", False)
        r = dev_client.post("/api/dev-integracoes/hooks-saida", json={"nome": "x", "url": "https://169.254.169.254/x"})
        assert r.status_code == 400


class TestHooksSaida:
    @pytest.fixture()
    def hook(self, dev_client, rede):
        h = dev_client.post("/api/dev-integracoes/hooks-saida", json={
            "nome": "n8n teste", "url": f"http://n8n.local/webhook/{uuid.uuid4().hex}", "eventos": ["tarefa.concluida", "evento.inventado"]}).json()
        yield h
        dev_client.delete(f"/api/dev-integracoes/hooks-saida/{h['id']}")

    def test_cadastro_filtra_eventos_invalidos_e_gera_segredo(self, hook):
        assert hook["eventos"] == ["tarefa.concluida"] and len(hook["segredo"]) >= 20

    def test_so_dispara_para_os_eventos_assinados_e_assina_com_hmac(self, dev_client, hook, rede):
        tid = _mk(dev_client, "Vai concluir")                            # tarefa.criada -> hook não assina
        try:
            assert rede.para(hook["url"]) == []
            dev_client.put(f"/api/dev-tarefas/{tid}", json={"titulo": "Vai concluir", "status": "done"})
            assert len(rede.para(hook["url"])) == 1
            p = rede.para(hook["url"])[0]
            assert p["corpo"]["evento"] == "tarefa.concluida" and p["corpo"]["tarefa"]["titulo"] == "Vai concluir"
            assert p["headers"]["X-Faiston-Event"] == "tarefa.concluida"
            assert p["headers"]["X-Faiston-Signature"] == eventos.assinar(hook["segredo"], p["bruto"])
            ultimo = next(h for h in dev_client.get("/api/dev-integracoes/hooks-saida").json()["hooks"] if h["id"] == hook["id"])
            assert ultimo["ultimo_status"] == 200 and ultimo["ultimo_erro"] is None
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_arrastar_para_concluido_tambem_dispara(self, dev_client, hook, rede):
        tid = _mk(dev_client, "Drag concluir")
        try:
            dev_client.post("/api/dev-tarefas/reordenar", json={"status": "done", "ids": [tid]})
            assert [p["corpo"]["evento"] for p in rede.para(hook["url"])] == ["tarefa.concluida"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_falha_do_destino_tenta_de_novo_e_registra_o_erro(self, dev_client, hook, rede):
        rede.status_resposta = 503
        tid = _mk(dev_client, "Destino fora", status="done")             # criada já como concluída
        try:
            assert len(rede.para(hook["url"])) == 2                       # 1 tentativa + 1 retry
            ultimo = next(h for h in dev_client.get("/api/dev-integracoes/hooks-saida").json()["hooks"] if h["id"] == hook["id"])
            assert ultimo["ultimo_status"] is None and "503" in ultimo["ultimo_erro"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_desativado_nao_dispara(self, dev_client, hook, rede):
        dev_client.put(f"/api/dev-integracoes/hooks-saida/{hook['id']}", json={"ativo": False})
        tid = _mk(dev_client, "Sem hook", status="done")
        try:
            assert rede.para(hook["url"]) == []
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_botao_testar(self, dev_client, hook, rede):
        r = dev_client.post(f"/api/dev-integracoes/hooks-saida/{hook['id']}/testar").json()
        assert r == {"status": 200, "erro": None} and rede.para(hook["url"])[0]["corpo"]["evento"] == "ping"

    def test_excluir_tarefa_manda_evento_com_snapshot(self, dev_client, rede):
        h = dev_client.post("/api/dev-integracoes/hooks-saida", json={"nome": "del", "url": f"http://x.local/{uuid.uuid4().hex}", "eventos": ["tarefa.excluida"]}).json()
        tid = _mk(dev_client, "Vai sumir")
        try:
            dev_client.delete(f"/api/dev-tarefas/{tid}")
            env = rede.para(h["url"])
            assert len(env) == 1 and env[0]["corpo"]["evento"] == "tarefa.excluida" and env[0]["corpo"]["tarefa"]["titulo"] == "Vai sumir"
        finally:
            dev_client.delete(f"/api/dev-integracoes/hooks-saida/{h['id']}")


class TestEntradaGenerica:
    @pytest.fixture()
    def hook(self, dev_client):
        h = dev_client.post("/api/dev-integracoes/hooks-entrada", json={
            "nome": "TiFlux via n8n", "origem": "TiFlux", "tag_padrao": "Suporte", "status_padrao": "backlog",
            "atribuido_padrao": dev_client.dev_uid}).json()
        yield h
        dev_client.delete(f"/api/dev-integracoes/hooks-entrada/{h['id']}")

    def _post(self, anonimo, hook, corpo, **kw):
        return anonimo.post(hook["caminho"], json=corpo, **kw)

    def _limpar(self, dev_client, origem="tiflux"):
        for t in dev_client.get("/api/dev-tarefas").json():
            if t["origem"] == origem:
                dev_client.delete(f"/api/dev-tarefas/{t['id']}")

    def test_cria_com_padroes_do_hook_sem_precisar_de_cookie_nem_csrf(self, dev_client, anonimo, hook):
        try:
            r = self._post(anonimo, hook, {"titulo": "Chamado #123: VPN caindo", "origem_ref": "TKT-123", "prioridade": "alta", "link": "https://tiflux.exemplo/123"})
            assert r.status_code == 200, r.text
            assert r.json()["resultado"] == "criada"
            t = _get(dev_client, r.json()["id"])
            assert t["origem"] == "tiflux" and t["origem_ref"] == "TKT-123" and t["status"] == "backlog"
            assert t["tags"] == ["Suporte"] and t["atribuido_a"] == dev_client.dev_uid and t["prioridade"] == "alta"
        finally:
            self._limpar(dev_client)

    def test_mesmo_origem_ref_atualiza_em_vez_de_duplicar(self, dev_client, anonimo, hook):
        try:
            a = self._post(anonimo, hook, {"titulo": "Original", "origem_ref": "TKT-1"}).json()
            b = self._post(anonimo, hook, {"titulo": "Título novo", "origem_ref": "TKT-1", "prioridade": "urgente", "status": "doing"}).json()
            assert b["resultado"] == "atualizada" and b["id"] == a["id"]
            t = _get(dev_client, a["id"])
            assert t["titulo"] == "Título novo" and t["prioridade"] == "urgente" and t["status"] == "doing"
            assert len([x for x in dev_client.get("/api/dev-tarefas").json() if x["origem_ref"] == "TKT-1"]) == 1
        finally:
            self._limpar(dev_client)

    def test_mover_concluir_e_comentar(self, dev_client, anonimo, hook):
        try:
            tid = self._post(anonimo, hook, {"titulo": "Fluxo", "origem_ref": "TKT-9"}).json()["id"]
            assert self._post(anonimo, hook, {"acao": "mover", "origem_ref": "TKT-9", "status": "doing"}).json()["resultado"] == "movida"
            assert _get(dev_client, tid)["status"] == "doing"
            self._post(anonimo, hook, {"acao": "comentar", "origem_ref": "TKT-9", "comentario": "Cliente respondeu"})
            c = dev_client.get(f"/api/dev-tarefas/{tid}/comentarios").json()
            assert c[0]["usuario_nome"] == "TiFlux via n8n" and c[0]["texto"] == "Cliente respondeu"
            self._post(anonimo, hook, {"acao": "concluir", "origem_ref": "TKT-9"})
            t = _get(dev_client, tid)
            assert t["status"] == "done" and t["concluido_em"]
        finally:
            self._limpar(dev_client)

    def test_erros(self, dev_client, anonimo, hook):
        assert self._post(anonimo, hook, {"acao": "mover", "origem_ref": "NAO-EXISTE", "status": "done"}).status_code == 404
        assert self._post(anonimo, hook, {"acao": "mover", "status": "done"}).status_code == 400            # sem origem_ref
        assert self._post(anonimo, hook, {"origem_ref": "X"}).status_code == 400                             # sem título pra criar
        assert self._post(anonimo, hook, {"acao": "apagar", "origem_ref": "X"}).status_code == 400
        assert anonimo.post(hook["caminho"], content=b"nao e json").status_code == 400
        assert anonimo.post(hook["caminho"], json=["lista"]).status_code == 400
        assert anonimo.post(hook["caminho"], content=b"{" + b" " * (300 * 1024) + b"}").status_code == 413
        ultimo = next(h for h in dev_client.get("/api/dev-integracoes/hooks-entrada").json() if h["id"] == hook["id"])
        assert ultimo["ultimo_uso"] is not None

    def test_token_errado_ou_hook_desativado_dao_404(self, dev_client, anonimo, hook):
        assert anonimo.post("/api/dev-hooks/in/token-que-nao-existe", json={"titulo": "x"}).status_code == 404
        dev_client.put(f"/api/dev-integracoes/hooks-entrada/{hook['id']}", json={"ativo": False})
        assert self._post(anonimo, hook, {"titulo": "x"}).status_code == 404

    def test_limite_por_minuto(self, dev_client, anonimo, hook, monkeypatch):
        from app.devkanban import conexoes
        monkeypatch.setattr(conexoes, "LIMITE_ENTRADA_MIN", 2)
        conexoes._uso_hook.clear()
        codigos = [self._post(anonimo, hook, {"acao": "mover", "origem_ref": "N", "status": "done"}).status_code for _ in range(3)]
        assert codigos == [404, 404, 429]

    def test_entrada_generica_nao_dispara_saida(self, dev_client, anonimo, hook, rede):
        h = dev_client.post("/api/dev-integracoes/hooks-saida", json={"nome": "laço", "url": f"http://x.local/{uuid.uuid4().hex}"}).json()
        try:
            self._post(anonimo, hook, {"titulo": "Sem laço", "origem_ref": "LOOP-1"})
            assert rede.para(h["url"]) == []                                    # evita saída -> n8n -> entrada -> saída
        finally:
            dev_client.delete(f"/api/dev-integracoes/hooks-saida/{h['id']}")
            self._limpar(dev_client)

    def test_so_dev_cadastra_hook(self, admin_client):
        assert admin_client.post("/api/dev-integracoes/hooks-entrada", json={"nome": "x"}).status_code == 403


class TestGithubEntrada:
    PR = "https://github.com/Faiston/Dashboard/pull/42"

    @pytest.fixture()
    def hook(self, dev_client):
        h = dev_client.post("/api/dev-integracoes/hooks-entrada", json={"nome": "GitHub", "tipo": "github"}).json()
        yield h
        dev_client.delete(f"/api/dev-integracoes/hooks-entrada/{h['id']}")

    def _enviar(self, anonimo, hook, evento, payload, assinar=True):
        corpo = json.dumps(payload).encode()
        h = {"X-GitHub-Event": evento, "Content-Type": "application/json"}
        if assinar:
            h["X-Hub-Signature-256"] = eventos.assinar(hook["segredo"], corpo)
        return anonimo.post(hook["caminho"], content=corpo, headers=h)

    def _pr(self, acao, merged=False, url=None):
        return {"action": acao, "sender": {"login": "bob"}, "pull_request": {"html_url": url or self.PR, "number": 42, "merged": merged}}

    def test_exige_assinatura_valida(self, anonimo, hook):
        assert self._enviar(anonimo, hook, "ping", {"zen": "x"}, assinar=False).status_code == 401
        corpo = b'{"zen":"x"}'
        r = anonimo.post(hook["caminho"], content=corpo, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=" + "0" * 64})
        assert r.status_code == 401
        assert self._enviar(anonimo, hook, "ping", {"zen": "x"}).json()["acao"] == "ping"

    def test_pr_mergeado_conclui_a_tarefa_comenta_e_avisa_a_saida(self, dev_client, anonimo, hook, rede):
        tid = _mk(dev_client, "Feature do PR", status="doing", link=self.PR + "?diff=split")        # link com query: normaliza
        h = dev_client.post("/api/dev-integracoes/hooks-saida", json={"nome": "s", "url": f"http://x.local/{uuid.uuid4().hex}", "eventos": ["tarefa.concluida"]}).json()
        try:
            r = self._enviar(anonimo, hook, "pull_request", self._pr("closed", merged=True, url=self.PR.lower()))
            assert r.status_code == 200 and r.json()["movidas"] == 1, r.text
            assert _get(dev_client, tid)["status"] == "done"
            assert "mergeado por @bob" in dev_client.get(f"/api/dev-tarefas/{tid}/comentarios").json()[0]["texto"]
            assert [p["corpo"]["evento"] for p in rede.para(h["url"])] == ["tarefa.concluida"]
        finally:
            dev_client.delete(f"/api/dev-integracoes/hooks-saida/{h['id']}")
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_pr_aberto_move_a_fazer_para_em_progresso_mas_nao_mexe_no_que_ja_avancou(self, dev_client, anonimo, hook):
        a, b = _mk(dev_client, "Em A fazer", link=self.PR), _mk(dev_client, "Já concluída", status="done", link=self.PR)
        try:
            self._enviar(anonimo, hook, "pull_request", self._pr("opened"))
            assert _get(dev_client, a)["status"] == "doing" and _get(dev_client, b)["status"] == "done"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{a}"); dev_client.delete(f"/api/dev-tarefas/{b}")

    def test_pr_fechado_sem_merge_so_comenta(self, dev_client, anonimo, hook):
        tid = _mk(dev_client, "PR descartado", status="doing", link=self.PR)
        try:
            self._enviar(anonimo, hook, "pull_request", self._pr("closed", merged=False))
            assert _get(dev_client, tid)["status"] == "doing"
            assert "fechado sem merge" in dev_client.get(f"/api/dev-tarefas/{tid}/comentarios").json()[0]["texto"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_issue_fechada_conclui_e_reaberta_volta(self, dev_client, anonimo, hook):
        url = "https://github.com/Faiston/Dashboard/issues/7"
        tid = _mk(dev_client, "Bug da issue", link=url)
        try:
            payload = lambda a: {"action": a, "sender": {"login": "ana"}, "issue": {"html_url": url, "number": 7}}
            self._enviar(anonimo, hook, "issues", payload("closed"))
            assert _get(dev_client, tid)["status"] == "done"
            self._enviar(anonimo, hook, "issues", payload("reopened"))
            assert _get(dev_client, tid)["status"] == "todo"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_sem_tarefa_ligada_ou_evento_desconhecido_e_ignorado(self, anonimo, hook):
        assert self._enviar(anonimo, hook, "pull_request", self._pr("closed", True, "https://github.com/x/y/pull/1")).json()["acao"] == "ignorado"
        assert self._enviar(anonimo, hook, "push", {"ref": "refs/heads/main"}).json()["acao"] == "ignorado"


class TestGithubStatus:
    @pytest.fixture()
    def api(self, monkeypatch):
        class A:
            chamadas, resp = [], {}
        a = A(); a.chamadas = []; a.resp = {}
        github._cache.clear()
        def _get_falso(caminho):
            a.chamadas.append(caminho)
            for k, v in a.resp.items():
                if caminho.startswith(k):
                    return v
            return 404, {}
        monkeypatch.setattr(github, "_get", _get_falso)
        return a

    def test_interpretar_e_normalizar(self):
        assert github.interpretar("https://github.com/Org/Repo/pull/12/files") == ("Org", "Repo", "pull", 12)
        assert github.normalizar("https://github.com/Org/Repo/issues/3#issuecomment-1") == "https://github.com/org/repo/issues/3"
        assert github.interpretar("https://gitlab.com/a/b/pull/1") is None and github.interpretar("https://github.com/a/b") is None

    def test_pr_mergeado(self, api):
        api.resp["/repos/o/r/pulls/1"] = (200, {"merged": True, "state": "closed", "title": "Feat", "user": {"login": "bob"}, "updated_at": "2026-11-10T10:00:00Z", "head": {"sha": "abc"}, "html_url": "u"})
        s = github.status("https://github.com/o/r/pull/1")
        assert s["estado"] == "mergeado" and s["titulo"] == "Feat" and s["checks"] is None
        assert not any("check-runs" in c for c in api.chamadas)            # não gasta chamada de checks em PR já fechado

    def test_pr_aberto_com_checks_falhando_e_cache(self, api):
        api.resp["/repos/o/r/pulls/2"] = (200, {"merged": False, "state": "open", "draft": False, "title": "T", "user": {}, "head": {"sha": "s"}})
        api.resp["/repos/o/r/commits/s/check-runs"] = (200, {"check_runs": [{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "failure"}]})
        assert github.status("https://github.com/o/r/pull/2")["checks"] == "falhando"
        n = len(api.chamadas)
        github.status("https://github.com/o/r/pull/2")
        assert len(api.chamadas) == n                                        # segunda leitura veio do cache

    def test_checks_pendentes_e_ok(self, api):
        api.resp["/repos/o/r/pulls/3"] = (200, {"state": "open", "draft": True, "title": "T", "user": {}, "head": {"sha": "p"}})
        api.resp["/repos/o/r/commits/p/check-runs"] = (200, {"check_runs": [{"status": "in_progress"}]})
        s = github.status("https://github.com/o/r/pull/3")
        assert s["estado"] == "rascunho" and s["checks"] == "pendente"

    def test_repo_privado_sem_token(self, api, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        s = github.status("https://github.com/o/r/pull/9")
        assert s["erro"] == "sem_acesso" and "GITHUB_TOKEN" in s["dica"]

    def test_endpoint_em_lote_ignora_links_que_nao_sao_do_github(self, dev_client, api):
        api.resp["/repos/o/r/issues/5"] = (200, {"state": "open", "title": "Issue", "user": {"login": "z"}})
        d = dev_client.post("/api/dev-integracoes/github/status", json={"urls": ["https://github.com/o/r/issues/5", "https://exemplo.com/x", "lixo"]}).json()
        assert list(d) == ["https://github.com/o/r/issues/5"] and d["https://github.com/o/r/issues/5"]["tipo"] == "issue"


class TestCalendario:
    @pytest.fixture()
    def graph_falso(self, monkeypatch, rede):
        class G:
            chamadas = []
        g = G(); g.chamadas = []
        monkeypatch.setattr(graph, "configurado", lambda: True)
        def _req(metodo, caminho, **kw):
            g.chamadas.append((metodo, caminho, kw))
            return {"id": "EVT-1"} if metodo == "POST" else {}
        monkeypatch.setattr(graph, "_req", _req)
        return g

    def test_nao_configurado(self, dev_client, monkeypatch):
        monkeypatch.setattr(graph, "configurado", lambda: False)
        tid = _mk(dev_client, "Sem graph", prazo="2026-12-01")
        try:
            assert dev_client.post(f"/api/dev-integracoes/calendario/{tid}").status_code == 503
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_exige_prazo(self, dev_client, graph_falso):
        tid = _mk(dev_client, "Sem prazo")
        try:
            assert dev_client.post(f"/api/dev-integracoes/calendario/{tid}").status_code == 400
            assert graph_falso.chamadas == []
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_cria_evento_dia_inteiro_na_caixa_do_responsavel(self, dev_client, graph_falso):
        tid = _mk(dev_client, "Entregar relatório", prazo="2026-12-01", atribuido_a=dev_client.dev_uid)
        try:
            r = dev_client.post(f"/api/dev-integracoes/calendario/{tid}")
            assert r.status_code == 200 and r.json()["mailbox"] == dev_client.dev_email
            metodo, caminho, kw = graph_falso.chamadas[0]
            assert metodo == "POST" and caminho.endswith("/events") and quote(dev_client.dev_email) in caminho
            ev = kw["json"]
            assert ev["isAllDay"] and ev["subject"] == "[Prazo] Entregar relatório"
            assert ev["start"]["dateTime"] == "2026-12-01T00:00:00" and ev["end"]["dateTime"] == "2026-12-02T00:00:00"
            assert _get(dev_client, tid)["evento_id"] == "EVT-1"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_alterar_prazo_atualiza_e_concluir_remove_o_evento(self, dev_client, graph_falso):
        tid = _mk(dev_client, "Muda prazo", prazo="2026-12-01", atribuido_a=dev_client.dev_uid)
        try:
            dev_client.post(f"/api/dev-integracoes/calendario/{tid}")
            graph_falso.chamadas.clear()
            dev_client.put(f"/api/dev-tarefas/{tid}", json={"titulo": "Muda prazo", "prazo": "2026-12-10", "atribuido_a": dev_client.dev_uid})
            m, c, kw = graph_falso.chamadas[-1]
            assert m == "PATCH" and kw["json"]["start"]["dateTime"] == "2026-12-10T00:00:00"
            graph_falso.chamadas.clear()
            dev_client.put(f"/api/dev-tarefas/{tid}", json={"titulo": "Muda prazo", "prazo": "2026-12-10", "status": "done"})
            assert graph_falso.chamadas[-1][0] == "DELETE" and _get(dev_client, tid)["evento_id"] is None
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_excluir_a_tarefa_remove_o_evento(self, dev_client, graph_falso):
        tid = _mk(dev_client, "Vai excluir", prazo="2026-12-01", atribuido_a=dev_client.dev_uid)
        dev_client.post(f"/api/dev-integracoes/calendario/{tid}")
        graph_falso.chamadas.clear()
        dev_client.delete(f"/api/dev-tarefas/{tid}")
        assert graph_falso.chamadas and graph_falso.chamadas[-1][0] == "DELETE"

    def test_remover_manual(self, dev_client, graph_falso):
        tid = _mk(dev_client, "Remover manual", prazo="2026-12-01", atribuido_a=dev_client.dev_uid)
        try:
            dev_client.post(f"/api/dev-integracoes/calendario/{tid}")
            assert dev_client.delete(f"/api/dev-integracoes/calendario/{tid}").status_code == 200
            assert graph_falso.chamadas[-1][0] == "DELETE" and _get(dev_client, tid)["evento_id"] is None
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_erro_do_graph_vira_mensagem_clara(self, dev_client, monkeypatch, rede):
        monkeypatch.setattr(graph, "configurado", lambda: True)
        def _req(*a, **k):
            raise graph.GraphErro(403, "Access is denied")
        monkeypatch.setattr(graph, "_req", _req)
        tid = _mk(dev_client, "Graph nega", prazo="2026-12-01", atribuido_a=dev_client.dev_uid)
        try:
            r = dev_client.post(f"/api/dev-integracoes/calendario/{tid}")
            assert r.status_code == 503 and "denied" in r.json()["detail"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")


class TestOutlook:
    def test_lista_sinalizados_e_converte_html_do_email(self, dev_client, monkeypatch):
        monkeypatch.setattr(graph, "configurado", lambda: True)
        def _req(metodo, caminho, **kw):
            if caminho.endswith("/messages"):
                assert "flagged" in kw["params"]["$filter"] and quote(dev_client.dev_email) in caminho
                return {"value": [{"id": "AAA=", "subject": "Revisar contrato", "from": {"emailAddress": {"name": "Ana"}}, "receivedDateTime": "2026-11-10T09:00:00Z", "bodyPreview": "oi", "webLink": "https://outlook/x"}]}
            return {"subject": "Revisar contrato", "from": {"emailAddress": {"name": "Ana"}}, "webLink": "https://outlook/x",
                    "body": {"contentType": "html", "content": "<style>p{}</style><p>Olá,</p><p>revisar até <b>sexta</b>.</p><script>x()</script>"}}
        monkeypatch.setattr(graph, "_req", _req)
        lista = dev_client.get("/api/dev-integracoes/outlook/emails").json()
        assert lista == [{"id": "AAA=", "assunto": "Revisar contrato", "de": "Ana", "recebido": "2026-11-10", "previa": "oi", "link": "https://outlook/x"}]
        e = dev_client.get("/api/dev-integracoes/outlook/email", params={"id": "AAA="}).json()
        assert "Assunto: Revisar contrato" in e["texto"] and "revisar até sexta." in e["texto"] and "x()" not in e["texto"] and "<p>" not in e["texto"]
        assert e["link"] == "https://outlook/x"

    def test_conversao_de_html_nao_deixa_espaco_solto(self):
        h = "<html><head><title>x</title></head><body><div>Oi&nbsp;Rafa,</div><p>Precisamos fechar o <b>contrato</b>, <a href='x'>veja aqui</a>.</p><ul><li>item 1</li><li>item 2</li></ul><table><tr><td>A</td><td>B</td></tr></table></body></html>"
        assert graph._html_para_texto(h) == "Oi Rafa,\nPrecisamos fechar o contrato, veja aqui.\nitem 1\nitem 2\nA B"

    def test_sem_graph(self, dev_client, monkeypatch):
        monkeypatch.setattr(graph, "configurado", lambda: False)
        assert dev_client.get("/api/dev-integracoes/outlook/emails").status_code == 503
