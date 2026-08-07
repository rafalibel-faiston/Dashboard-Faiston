"""
Testes do servidor MCP (/mcp) — o conector do Claude para o módulo de MC.

Simula o que o Claude faz ao adicionar um conector personalizado: descoberta
(.well-known), registro dinâmico de cliente, code flow com PKCE, troca por
token e as chamadas JSON-RPC (initialize / tools/list / tools/call).

Cobre também o que não pode acontecer: token forjado, code reutilizado,
code_verifier errado, redirect_uri não registrada, e perfil sem permissão
conseguindo importar.
"""
import base64
import hashlib
import secrets
import urllib.parse
import uuid

import pytest


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture()
def cliente_oauth(admin_client):
    """Registra um cliente OAuth como o Claude faria (RFC 7591)."""
    r = admin_client.post("/mcp/oauth/register", json={
        "client_name": "Claude (teste)", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201, r.text
    return r.json()


def autorizar(client, cliente_oauth, usuario="admin", senha="admin123"):
    """Faz o code flow inteiro e devolve o access_token."""
    verifier, challenge = pkce()
    r = client.post("/mcp/oauth/authorize", data={
        "client_id": cliente_oauth["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "state": "xyz", "usuario": usuario, "senha": senha,
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    assert qs["state"] == ["xyz"]
    code = qs["code"][0]
    r = client.post("/mcp/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": cliente_oauth["client_id"], "client_secret": cliente_oauth["client_secret"],
        "redirect_uri": REDIRECT,
    })
    assert r.status_code == 200, r.text
    return r.json()


def rpc(client, token, metodo, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": metodo}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def chamar(client, token, nome, args=None):
    resp = rpc(client, token, "tools/call", {"name": nome, "arguments": args or {}})
    assert "result" in resp, resp
    return resp["result"]


@pytest.fixture()
def token(admin_client, cliente_oauth):
    return autorizar(admin_client, cliente_oauth)["access_token"]


class TestDescoberta:
    def test_metadata_do_resource(self, admin_client):
        d = admin_client.get("/.well-known/oauth-protected-resource").json()
        assert d["resource"].endswith("/mcp")
        assert d["authorization_servers"]

    def test_metadata_com_sufixo_mcp(self, admin_client):
        # O Claude pode pedir /.well-known/oauth-protected-resource/mcp
        assert admin_client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200

    def test_metadata_do_authorization_server(self, admin_client):
        d = admin_client.get("/.well-known/oauth-authorization-server").json()
        for campo in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
            assert d[campo], campo
        # PKCE S256 obrigatório: 'plain' não pode ser oferecido
        assert d["code_challenge_methods_supported"] == ["S256"]

    def test_https_atras_do_proxy(self, admin_client):
        """No Railway o app recebe http; o metadata tem que anunciar https."""
        d = admin_client.get("/.well-known/oauth-authorization-server",
                             headers={"x-forwarded-proto": "https",
                                      "x-forwarded-host": "ops.example.com"}).json()
        assert d["issuer"] == "https://ops.example.com"
        assert d["token_endpoint"].startswith("https://ops.example.com/")


class TestRegistro:
    def test_registra_cliente(self, cliente_oauth):
        assert cliente_oauth["client_id"].startswith("mcpc_")
        assert cliente_oauth["client_secret"]

    def test_sem_redirect_uri_rejeita(self, admin_client):
        r = admin_client.post("/mcp/oauth/register", json={"client_name": "x"})
        assert r.status_code == 400

    def test_redirect_http_rejeitado(self, admin_client):
        r = admin_client.post("/mcp/oauth/register", json={
            "redirect_uris": ["http://evil.example.com/cb"]})
        assert r.status_code == 400
        assert "https" in r.json()["error_description"]

    def test_localhost_http_permitido(self, admin_client):
        r = admin_client.post("/mcp/oauth/register", json={
            "redirect_uris": ["http://localhost:5173/cb"]})
        assert r.status_code == 201


class TestAutorizacao:
    def test_tela_de_login_aparece(self, admin_client, cliente_oauth):
        _, challenge = pkce()
        r = admin_client.get("/mcp/oauth/authorize", params={
            "response_type": "code", "client_id": cliente_oauth["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256"})
        assert r.status_code == 200
        assert "FAISTON" in r.text and "Autorizar" in r.text

    def test_sem_pkce_rejeita(self, admin_client, cliente_oauth):
        r = admin_client.get("/mcp/oauth/authorize", params={
            "response_type": "code", "client_id": cliente_oauth["client_id"],
            "redirect_uri": REDIRECT})
        assert r.status_code == 400
        assert "code_challenge" in r.json()["error_description"]

    def test_pkce_plain_rejeita(self, admin_client, cliente_oauth):
        _, challenge = pkce()
        r = admin_client.get("/mcp/oauth/authorize", params={
            "response_type": "code", "client_id": cliente_oauth["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "plain"})
        assert r.status_code == 400

    def test_redirect_nao_registrada_rejeita(self, admin_client, cliente_oauth):
        _, challenge = pkce()
        r = admin_client.get("/mcp/oauth/authorize", params={
            "response_type": "code", "client_id": cliente_oauth["client_id"],
            "redirect_uri": "https://outro.example.com/cb", "code_challenge": challenge})
        assert r.status_code == 400

    def test_client_desconhecido_rejeita(self, admin_client):
        _, challenge = pkce()
        r = admin_client.get("/mcp/oauth/authorize", params={
            "response_type": "code", "client_id": "mcpc_naoexiste",
            "redirect_uri": REDIRECT, "code_challenge": challenge})
        assert r.status_code == 400

    def test_senha_errada_nao_gera_code(self, admin_client, cliente_oauth):
        _, challenge = pkce()
        r = admin_client.post("/mcp/oauth/authorize", data={
            "client_id": cliente_oauth["client_id"], "redirect_uri": REDIRECT,
            "code_challenge": challenge, "usuario": "admin", "senha": "errada",
        }, follow_redirects=False)
        assert r.status_code == 200          # volta pra tela, não redireciona
        assert "inválidos" in r.text

    def test_perfil_sem_acesso_a_mc_nao_autoriza(self, admin_client, cliente_oauth):
        usuario = f"teste_mcp_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        r = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Func MCP", "perfil": "funcionario"})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        try:
            _, challenge = pkce()
            r = admin_client.post("/mcp/oauth/authorize", data={
                "client_id": cliente_oauth["client_id"], "redirect_uri": REDIRECT,
                "code_challenge": challenge, "usuario": usuario, "senha": senha,
            }, follow_redirects=False)
            assert r.status_code == 200
            assert "não tem acesso" in r.text
        finally:
            admin_client.delete(f"/api/usuarios/{uid}")


class TestToken:
    def test_fluxo_completo(self, admin_client, cliente_oauth):
        t = autorizar(admin_client, cliente_oauth)
        assert t["token_type"] == "Bearer"
        assert t["access_token"] and t["refresh_token"] and t["expires_in"] > 0

    def test_verifier_errado_rejeita(self, admin_client, cliente_oauth):
        _, challenge = pkce()
        r = admin_client.post("/mcp/oauth/authorize", data={
            "client_id": cliente_oauth["client_id"], "redirect_uri": REDIRECT,
            "code_challenge": challenge, "usuario": "admin", "senha": "admin123",
        }, follow_redirects=False)
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(r.headers["location"]).query)["code"][0]
        r = admin_client.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "code_verifier": secrets.token_urlsafe(48),   # não corresponde
            "client_id": cliente_oauth["client_id"],
            "client_secret": cliente_oauth["client_secret"]})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_code_nao_pode_ser_reusado(self, admin_client, cliente_oauth):
        verifier, challenge = pkce()
        r = admin_client.post("/mcp/oauth/authorize", data={
            "client_id": cliente_oauth["client_id"], "redirect_uri": REDIRECT,
            "code_challenge": challenge, "usuario": "admin", "senha": "admin123",
        }, follow_redirects=False)
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(r.headers["location"]).query)["code"][0]
        dados = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                 "client_id": cliente_oauth["client_id"],
                 "client_secret": cliente_oauth["client_secret"]}
        assert admin_client.post("/mcp/oauth/token", data=dados).status_code == 200
        r2 = admin_client.post("/mcp/oauth/token", data=dados)
        assert r2.status_code == 400
        assert r2.json()["error"] == "invalid_grant"

    def test_client_secret_errado_rejeita(self, admin_client, cliente_oauth):
        verifier, challenge = pkce()
        r = admin_client.post("/mcp/oauth/authorize", data={
            "client_id": cliente_oauth["client_id"], "redirect_uri": REDIRECT,
            "code_challenge": challenge, "usuario": "admin", "senha": "admin123",
        }, follow_redirects=False)
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(r.headers["location"]).query)["code"][0]
        r = admin_client.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": cliente_oauth["client_id"], "client_secret": "errado"})
        assert r.status_code == 401

    def test_refresh_token_funciona_e_rotaciona(self, admin_client, cliente_oauth):
        t = autorizar(admin_client, cliente_oauth)
        r = admin_client.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
            "client_id": cliente_oauth["client_id"],
            "client_secret": cliente_oauth["client_secret"]})
        assert r.status_code == 200, r.text
        novo = r.json()
        assert novo["access_token"] != t["access_token"]
        # o refresh antigo não serve mais (rotação)
        r2 = admin_client.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
            "client_id": cliente_oauth["client_id"],
            "client_secret": cliente_oauth["client_secret"]})
        assert r2.status_code == 400
        # e o token novo funciona no MCP
        assert rpc(admin_client, novo["access_token"], "ping")["result"] == {}

    def test_grant_desconhecido_rejeita(self, admin_client, cliente_oauth):
        r = admin_client.post("/mcp/oauth/token", data={
            "grant_type": "password", "client_id": cliente_oauth["client_id"],
            "client_secret": cliente_oauth["client_secret"]})
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"


class TestAcessoAoMcp:
    def test_sem_token_401_com_www_authenticate(self, admin_client):
        r = admin_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
        # é assim que o cliente descobre onde começar o OAuth
        assert "resource_metadata=" in r.headers.get("www-authenticate", "")

    def test_token_forjado_401(self, admin_client):
        r = admin_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Authorization": "Bearer " + secrets.token_urlsafe(40)})
        assert r.status_code == 401

    def test_get_recusado(self, admin_client):
        r = admin_client.get("/mcp")
        assert r.status_code == 405
        assert r.headers.get("allow") == "POST"


class TestProtocolo:
    def test_initialize_ecoa_versao_conhecida(self, admin_client, token):
        r = rpc(admin_client, token, "initialize",
                {"protocolVersion": "2025-03-26", "capabilities": {},
                 "clientInfo": {"name": "claude", "version": "1"}})
        assert r["result"]["protocolVersion"] == "2025-03-26"
        assert r["result"]["serverInfo"]["name"] == "faiston-ops-mc"
        assert "tools" in r["result"]["capabilities"]
        # as instruções dizem quem está autenticado
        assert "Administrador" in r["result"]["instructions"]

    def test_initialize_versao_desconhecida_cai_no_preferido(self, admin_client, token):
        r = rpc(admin_client, token, "initialize", {"protocolVersion": "1999-01-01"})
        assert r["result"]["protocolVersion"] == "2025-06-18"

    def test_notificacao_nao_gera_resposta(self, admin_client, token):
        r = admin_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                              headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 202

    def test_json_invalido(self, admin_client, token):
        r = admin_client.post("/mcp", content=b"{nao eh json",
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32700

    def test_metodo_desconhecido(self, admin_client, token):
        r = rpc(admin_client, token, "coisa/inexistente")
        assert r["error"]["code"] == -32601

    def test_lote(self, admin_client, token):
        r = admin_client.post("/mcp", json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ], headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        corpo = r.json()
        assert isinstance(corpo, list) and len(corpo) == 2

    def test_tools_list(self, admin_client, token):
        tools = rpc(admin_client, token, "tools/list")["result"]["tools"]
        nomes = {t["name"] for t in tools}
        assert {"mc_importar", "mc_listar", "mc_detalhe", "contratos_buscar"} <= nomes
        for t in tools:
            assert t["description"] and t["inputSchema"]["type"] == "object"

    def test_resources_e_prompts_vazios(self, admin_client, token):
        assert rpc(admin_client, token, "resources/list")["result"]["resources"] == []
        assert rpc(admin_client, token, "prompts/list")["result"]["prompts"] == []


class TestTools:
    def test_importar_grava_e_confere_totais(self, admin_client, token):
        contrato = f"TESTE-MCP-{uuid.uuid4().hex[:8].upper()}"
        res = chamar(admin_client, token, "mc_importar", {
            "contrato": contrato, "ano": "01", "cliente": "Via MCP",
            "equipe": [{"funcao": "Coordenador", "custo_mensal": 18300, "meses": 12,
                        "custo_total": 219600.00},
                       {"funcao": "Analista N2", "custo_mensal": 7610.5525, "meses": 12,
                        "custo_total": 91326.63}],
            "investimentos": [{"item": "Notebook", "quantidade": 4, "valor_unitario": 5600,
                               "valor_total": 22400.00},
                              {"item": "Ferramentas", "quantidade": 4, "valor_unitario": 1500,
                               "valor_total": 6000.00}],
            "totais": {"equipe": 310926.63, "investimentos": 28400.00},
        })
        assert res["isError"] is False
        d = res["structuredContent"]
        assert d["total_equipe"] == 310926.63
        assert d["total_investimentos"] == 28400.00
        # texto legível também vai, pra quem não usa structuredContent
        assert contrato in res["content"][0]["text"]
        admin_client.delete(f"/api/mc/contratos/{d['id']}")

    def test_divergencia_vira_atencao_nao_erro(self, admin_client, token):
        """MC gravada com problema não é falha da chamada: o Claude precisa
        saber que entrou e precisa de revisão, não tentar de novo."""
        contrato = f"TESTE-MCP-{uuid.uuid4().hex[:8].upper()}"
        res = chamar(admin_client, token, "mc_importar", {
            "contrato": contrato,
            "equipe": [{"funcao": "X", "custo_total": 100.00}],
            "totais": {"equipe": 999.00},
        })
        assert res["isError"] is False
        d = res["structuredContent"]
        assert d["status"] == "REVISAO_MANUAL"
        assert "atencao" in d and "conferência humana" in d["atencao"]
        admin_client.delete(f"/api/mc/contratos/{d['id']}")

    def test_importar_sem_contrato_da_erro(self, admin_client, token):
        res = chamar(admin_client, token, "mc_importar", {"ano": "01"})
        assert res["isError"] is True
        assert "contrato" in res["structuredContent"]["erro"].lower()

    def test_reimportar_substitui(self, admin_client, token):
        contrato = f"TESTE-MCP-{uuid.uuid4().hex[:8].upper()}"
        args = {"contrato": contrato, "equipe": [{"funcao": "A", "custo_total": 10},
                                                {"funcao": "B", "custo_total": 20}]}
        p1 = chamar(admin_client, token, "mc_importar", args)["structuredContent"]
        args["equipe"] = [{"funcao": "A", "custo_total": 10}]
        p2 = chamar(admin_client, token, "mc_importar", args)["structuredContent"]
        assert p2["id"] == p1["id"] and p2["linhas_equipe"] == 1
        admin_client.delete(f"/api/mc/contratos/{p1['id']}")

    def test_listar_e_detalhe(self, admin_client, token):
        contrato = f"TESTE-MCP-{uuid.uuid4().hex[:8].upper()}"
        mid = chamar(admin_client, token, "mc_importar", {
            "contrato": contrato, "equipe": [{"funcao": "Analista", "custo_total": 500}],
        })["structuredContent"]["id"]
        try:
            lista = chamar(admin_client, token, "mc_listar",
                           {"contrato": contrato})["structuredContent"]
            assert lista["total"] == 1
            det = chamar(admin_client, token, "mc_detalhe", {"id": mid})["structuredContent"]
            assert det["contrato"] == contrato
            assert det["equipe"][0]["funcao"] == "Analista"
        finally:
            admin_client.delete(f"/api/mc/contratos/{mid}")

    def test_detalhe_id_como_texto(self, admin_client, token):
        """Modelo às vezes manda "12" em vez de 12."""
        contrato = f"TESTE-MCP-{uuid.uuid4().hex[:8].upper()}"
        mid = chamar(admin_client, token, "mc_importar", {
            "contrato": contrato, "equipe": [{"funcao": "X", "custo_total": 1}],
        })["structuredContent"]["id"]
        try:
            det = chamar(admin_client, token, "mc_detalhe", {"id": str(mid)})
            assert det["isError"] is False
            assert det["structuredContent"]["id"] == mid
        finally:
            admin_client.delete(f"/api/mc/contratos/{mid}")

    def test_detalhe_inexistente_vira_erro_de_tool(self, admin_client, token):
        res = chamar(admin_client, token, "mc_detalhe", {"id": 99999999})
        assert res["isError"] is True
        assert res["structuredContent"]["http_status"] == 404

    def test_contratos_buscar(self, admin_client, token, cliente_teste):
        nome = f"CONTRATO-MCP-{uuid.uuid4().hex[:8].upper()}"
        cid = admin_client.post("/api/gestao/contratos",
                                json={"cliente_id": cliente_teste, "nome": nome}).json()["id"]
        try:
            d = chamar(admin_client, token, "contratos_buscar",
                       {"termo": nome[:18]})["structuredContent"]
            assert any(c["nome"] == nome for c in d["contratos_gestao"])
            assert "forecast_projetos" in d
        finally:
            admin_client.delete(f"/api/gestao/contratos/{cid}")

    def test_tool_desconhecida(self, admin_client, token):
        res = chamar(admin_client, token, "nao_existe")
        assert res["isError"] is True

    def test_demo_le_mas_nao_importa(self, admin_client, app, cliente_oauth):
        """Mesma regra do HTTP: demo tem leitura, não escrita."""
        usuario = f"teste_mcpdemo_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        r = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Demo MCP", "perfil": "demo"})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        try:
            t = autorizar(admin_client, cliente_oauth, usuario, senha)["access_token"]
            assert chamar(admin_client, t, "mc_listar")["isError"] is False
            res = chamar(admin_client, t, "mc_importar", {"contrato": "F999999"})
            assert res["isError"] is True
            assert "não pode importar" in res["structuredContent"]["erro"]
        finally:
            admin_client.delete(f"/api/usuarios/{uid}")


class TestTokenFixo:
    def test_bearer_de_ambiente(self, admin_client, monkeypatch):
        """Caminho alternativo pra chamar de fora do OAuth do Claude."""
        monkeypatch.setenv("MCP_API_TOKEN", "token-fixo-de-teste-123")
        monkeypatch.setenv("MCP_API_TOKEN_USUARIO", "admin")
        assert rpc(admin_client, "token-fixo-de-teste-123", "ping")["result"] == {}

    def test_token_fixo_errado_401(self, admin_client, monkeypatch):
        monkeypatch.setenv("MCP_API_TOKEN", "token-fixo-de-teste-123")
        r = admin_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Authorization": "Bearer token-errado"})
        assert r.status_code == 401

    def test_sem_env_nao_aceita_nada(self, admin_client, monkeypatch):
        monkeypatch.delenv("MCP_API_TOKEN", raising=False)
        r = admin_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Authorization": "Bearer qualquer-coisa"})
        assert r.status_code == 401


class TestToolPlanilha:
    """A tool preferida: o Claude passa o link e o Ops lê a planilha.

    O download é exercitado de verdade (servidor HTTP local), não mockado —
    é justamente o caminho que quebra na vida real."""

    @pytest.fixture()
    def servidor_xlsx(self):
        import http.server, io, socket, threading
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet("2.1 Equipe")
        for l in [["MC — Custo de Equipe"], [],
                  ["Função", "Quantidade", "Salário", "Custo Mensal", "Meses", "Custo Total"],
                  ["Coordenador de Projeto", 1, 9500, 18300, 12, 219600.00],
                  ["Analista de Suporte N2", 1, 4200, 7610.5525, 12, 91326.63],
                  ["TOTAL EQUIPE", None, None, None, None, 310926.63]]:
            ws.append(l)
        ws = wb.create_sheet("2.2 Investimentos")
        for l in [["Investimentos"],
                  ["Item", "Categoria", "Quantidade", "Valor Unitário", "Valor Total"],
                  ["Notebook Dell Latitude", "Equipamentos", 4, 5600, 22400.00],
                  ["Kit de ferramentas", "Equipamentos", 4, 1500, 6000.00],
                  ["TOTAL INVESTIMENTOS", None, None, None, 28400.00]]:
            ws.append(l)
        buf = io.BytesIO(); wb.save(buf)
        conteudo = buf.getvalue()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith(".xlsx"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-"
                                                     "officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Length", str(len(conteudo)))
                    self.end_headers()
                    self.wfile.write(conteudo)
                elif self.path.endswith("login.xlsx.html") or "login" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<!DOCTYPE html><html>Sign in to OneDrive</html>" * 3)
                else:
                    self.send_response(404); self.end_headers()

            def log_message(self, *a):
                pass

        s = socket.socket(); s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]; s.close()
        srv = http.server.HTTPServer(("127.0.0.1", porta), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        yield f"http://127.0.0.1:{porta}"
        srv.shutdown()

    def test_ops_baixa_le_e_confere_sozinho(self, admin_client, token, servidor_xlsx):
        """Ninguém transcreveu número: o Ops leu a planilha e conferiu contra o
        total que ela declara."""
        res = chamar(admin_client, token, "mc_importar_planilha", {
            "url": f"{servidor_xlsx}/MC%20F260015-8%20SGB%20-%20ANO%2001.xlsx"})
        assert res["isError"] is False, res
        d = res["structuredContent"]
        try:
            assert d["contrato"] == "F260015-8"       # veio do nome do arquivo
            assert d["ano"] == "01"
            assert d["total_equipe"] == 310926.63
            assert d["total_investimentos"] == 28400.00
            assert "divergente" not in d["motivo_revisao"]
            assert d["planilha"]["linhas_equipe"] == 2
        finally:
            admin_client.delete(f"/api/mc/contratos/{d['id']}")

    def test_contrato_informado_vence_o_nome_do_arquivo(self, admin_client, token, servidor_xlsx):
        res = chamar(admin_client, token, "mc_importar_planilha", {
            "url": f"{servidor_xlsx}/MC%20F260301%20-%20ANO%2001.xlsx",
            "contrato": "F260534", "cliente": "ZAMP"})
        d = res["structuredContent"]
        try:
            assert d["contrato"] == "F260534"
            assert d["cliente"] == "ZAMP"
        finally:
            admin_client.delete(f"/api/mc/contratos/{d['id']}")

    def test_substitui_o_placeholder_do_kickoff(self, admin_client, token, servidor_xlsx):
        """O fluxo inteiro: e-mail cria o registro, planilha completa depois."""
        ph = chamar(admin_client, token, "mc_importar", {
            "contrato": "F260015-8", "ano": "01", "cliente": "T-SYSTEMS",
        })["structuredContent"]
        assert ph["status"] == "RECEBIDA"
        d = chamar(admin_client, token, "mc_importar_planilha", {
            "url": f"{servidor_xlsx}/MC%20F260015-8%20SGB%20-%20ANO%2001.xlsx",
        })["structuredContent"]
        try:
            assert d["id"] == ph["id"]                 # substituiu
            assert d["total_equipe"] == 310926.63
            assert d["linhas_equipe"] == 2
        finally:
            admin_client.delete(f"/api/mc/contratos/{d['id']}")

    def test_link_que_pede_login_da_erro_claro(self, admin_client, token, servidor_xlsx):
        """Engano mais comum do OneDrive: o link volta HTML de login."""
        res = chamar(admin_client, token, "mc_importar_planilha", {
            "url": f"{servidor_xlsx}/login.xlsx.html", "contrato": "F260777"})
        assert res["isError"] is True
        assert "login" in res["structuredContent"]["erro"].lower()

    def test_url_inacessivel_da_erro_claro(self, admin_client, token, servidor_xlsx):
        res = chamar(admin_client, token, "mc_importar_planilha", {
            "url": f"{servidor_xlsx}/nao-existe.zip"})
        assert res["isError"] is True
        assert "baixar" in res["structuredContent"]["erro"].lower()

    def test_sem_url_da_erro(self, admin_client, token):
        res = chamar(admin_client, token, "mc_importar_planilha", {})
        assert res["isError"] is True

    def test_tool_aparece_na_lista(self, admin_client, token):
        tools = {t["name"]: t for t in rpc(admin_client, token, "tools/list")["result"]["tools"]}
        assert "mc_importar_planilha" in tools
        # a descrição precisa dizer pro modelo preferir esta tool
        assert "PREFIRA" in tools["mc_importar"]["description"]

    def test_demo_nao_importa_planilha(self, admin_client, app, cliente_oauth, servidor_xlsx):
        usuario = f"teste_mcpldemo_{uuid.uuid4().hex[:8]}"
        senha = "senhaTeste123"
        uid = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": senha, "nome": "Demo", "perfil": "demo"}).json()["id"]
        try:
            t = autorizar(admin_client, cliente_oauth, usuario, senha)["access_token"]
            res = chamar(admin_client, t, "mc_importar_planilha", {
                "url": f"{servidor_xlsx}/MC%20F260015-8%20-%20ANO%2001.xlsx"})
            assert res["isError"] is True
            assert "não pode importar" in res["structuredContent"]["erro"]
        finally:
            admin_client.delete(f"/api/usuarios/{uid}")
