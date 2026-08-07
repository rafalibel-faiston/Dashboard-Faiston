"""
Servidor MCP do Faiston Ops — expõe o módulo de MC como conector do Claude.

Serve pra fechar o loop da MC sem passo manual: o Claude usa o conector do
Outlook pra achar o e-mail de KICK-OFF com a planilha nova, e este conector
pra gravar a MC dentro do Ops.

## Por que o protocolo é implementado à mão

O SDK oficial (`mcp`) exige `pydantic>=2.12`; o Ops roda `pydantic==2.7.1` com
`fastapi==0.111.0`. Subir o pydantic por causa de um módulo novo arrisca o app
inteiro. O subconjunto de MCP que um conector precisa é pequeno — JSON-RPC 2.0
sobre um POST, com `initialize`, `tools/list`, `tools/call` e `ping` — então
sai mais barato e mais seguro implementar aqui, sem dependência nova.

## Por que tem OAuth

O fluxo de conector personalizado do Claude assume OAuth 2.1: ele faz descoberta
(`/.well-known/...`), registro dinâmico de cliente (RFC 7591) e o code flow com
PKCE. Não existe opção "sem autenticação" na interface, e o suporte a header
fixo (Bearer) ainda é beta restrito. Então o Ops precisa se comportar como
authorization server pra o conector conectar.

O ganho é real e não é só burocracia: o `/authorize` autentica com o **login do
próprio Ops**, então o token fica amarrado a um usuário de verdade. As tools
respeitam o perfil desse usuário (`MC_PERFIS_ESCRITA`) e o `mc_ingestoes_log`
registra quem ingeriu — não é um token anônimo com acesso total.

Também aceita `Authorization: Bearer <MCP_API_TOKEN>` (variável de ambiente),
pra quem chamar de fora do Claude ou quando o header fixo estiver disponível.
Esse caminho age como o usuário configurado em `MCP_API_TOKEN_USUARIO`.

## Endpoints

    GET  /.well-known/oauth-protected-resource   descoberta do resource server
    GET  /.well-known/oauth-authorization-server descoberta do authorization server
    POST /mcp/oauth/register                     registro dinâmico de cliente
    GET  /mcp/oauth/authorize                    tela de login do Ops
    POST /mcp/oauth/authorize                    valida login e devolve o code
    POST /mcp/oauth/token                        troca code/refresh por token
    POST /mcp                                    o MCP em si (JSON-RPC 2.0)

Fora de `/api/*` de propósito: o CSRFMiddleware não se aplica (um cliente MCP
não tem cookie pra montar o header) e as rotas ficam visivelmente separadas.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger("faiston")

# Versões de protocolo que sabemos falar. Se o cliente pedir outra, ecoamos a
# dele quando estiver nesta lista; senão respondemos a nossa preferida e
# deixamos o cliente decidir se continua.
MCP_PROTOCOLO_PREFERIDO = "2025-06-18"
MCP_PROTOCOLOS_ACEITOS = ("2025-06-18", "2025-03-26", "2024-11-05")

CODE_TTL = 120           # segundos: code de autorização é de uso único e curto
TOKEN_TTL = 30 * 86400   # 30 dias
REFRESH_TTL = 180 * 86400

# ─────────────────────────────────────────────────────────────────────────────
#  Armazenamento
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_mcp_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
            client_id VARCHAR(64) PRIMARY KEY,
            client_secret VARCHAR(128) DEFAULT '',
            client_name VARCHAR(200) DEFAULT '',
            redirect_uris TEXT NOT NULL,
            criado_em TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
            code VARCHAR(128) PRIMARY KEY,
            client_id VARCHAR(64) NOT NULL,
            usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
            redirect_uri TEXT NOT NULL,
            code_challenge VARCHAR(128) NOT NULL,
            scope VARCHAR(200) DEFAULT '',
            expira_em TIMESTAMP NOT NULL,
            usado BOOLEAN DEFAULT FALSE,
            criado_em TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
            id SERIAL PRIMARY KEY,
            token_hash VARCHAR(64) UNIQUE NOT NULL,
            refresh_hash VARCHAR(64) UNIQUE,
            client_id VARCHAR(64) NOT NULL,
            usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
            scope VARCHAR(200) DEFAULT '',
            expira_em TIMESTAMP NOT NULL,
            refresh_expira_em TIMESTAMP,
            revogado BOOLEAN DEFAULT FALSE,
            ultimo_uso TIMESTAMP,
            criado_em TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_tokens_usuario ON mcp_oauth_tokens(usuario_id)")


def _hash(valor: str) -> str:
    """Tokens vão pro banco como SHA-256. Se o banco vazar, o vazamento não
    entrega credencial usável — mesma razão de senha não virar texto puro.
    SHA-256 puro basta aqui (ao contrário de senha): o segredo tem 256 bits de
    entropia, então não há dicionário nem rainbow table que ajude."""
    return hashlib.sha256(valor.encode()).hexdigest()


def _base_url(request: Request) -> str:
    """URL pública do serviço, respeitando o proxy do Railway.

    Sem olhar o X-Forwarded-Proto, o base_url sai como http:// atrás do proxy e
    o Claude rejeita metadata de OAuth que não seja https."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() \
        or request.headers.get("host", "")
    if not proto:
        proto = request.url.scheme
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}"


# ─────────────────────────────────────────────────────────────────────────────
#  Definição das tools
# ─────────────────────────────────────────────────────────────────────────────
#  As descrições são o que o Claude lê pra decidir quando usar cada uma, então
#  dizem também o que NÃO fazer (inventar contrato, inventar linha de custo).

_LINHA_EQUIPE = {
    "type": "object",
    "properties": {
        "funcao": {"type": "string", "description": "Cargo/função como está na planilha"},
        "quantidade": {"type": "number", "description": "Headcount desta função"},
        "salario": {"type": "number"},
        "encargos": {"type": "number"},
        "beneficios": {"type": "number"},
        "custo_mensal": {"type": "number"},
        "meses": {"type": "number"},
        "custo_total": {"type": "number", "description": "Custo total da linha como está na planilha. Não calcule: copie."},
    },
    "required": ["funcao"],
    "additionalProperties": True,
}

_LINHA_INVEST = {
    "type": "object",
    "properties": {
        "item": {"type": "string"},
        "categoria": {"type": "string"},
        "quantidade": {"type": "number"},
        "valor_unitario": {"type": "number"},
        "valor_total": {"type": "number", "description": "Total da linha como está na planilha. Não calcule: copie."},
    },
    "required": ["item"],
    "additionalProperties": True,
}

TOOLS = [
    {
        "name": "mc_importar",
        "title": "Importar MC no Faiston Ops",
        "description": (
            "Grava uma MC a partir de números que você já tem em mão. "
            "PREFIRA `mc_importar_planilha` quando a planilha estiver na nuvem: lá o Ops lê "
            "o arquivo e você não precisa transcrever nada. Use esta tool só quando não "
            "houver .xlsx acessível — por exemplo pra registrar o que veio no corpo do "
            "e-mail de kick-off enquanto a planilha não chega (pode mandar só contrato, "
            "cliente e projeto: a MC fica com status RECEBIDA aguardando as linhas).\n\n"
            "Idempotente por contrato+ano: reimportar o mesmo par substitui a MC "
            "anterior em vez de duplicar, então pode reenviar uma planilha corrigida.\n\n"
            "Copie os números da planilha como estão — não recalcule nem arredonde. Informe "
            "em `totais` os totais que a própria planilha declara: o Ops compara com a soma "
            "das linhas e, se não fechar, marca a MC para revisão manual em vez de aceitar "
            "número errado em silêncio. Se a planilha não declara total, omita `totais`.\n\n"
            "Nunca invente contrato, função ou item que não esteja na planilha. Se um campo "
            "não existir na planilha, omita."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contrato": {"type": "string", "description": "Código do contrato, ex.: F260015"},
                "ano": {"type": "string", "description": "Ano do contrato dentro da MC, ex.: '01'. Padrão '01'."},
                "cliente": {"type": "string"},
                "cliente_final": {"type": "string"},
                "projeto": {"type": "string"},
                "tcv": {"type": "number", "description": "Total Contract Value, se a planilha traz"},
                "arquivo": {"type": "string", "description": "Nome do arquivo de origem, para rastreio"},
                "equipe": {"type": "array", "items": _LINHA_EQUIPE},
                "investimentos": {"type": "array", "items": _LINHA_INVEST},
                "totais": {
                    "type": "object",
                    "description": "Totais declarados na planilha, para conferência",
                    "properties": {"equipe": {"type": "number"}, "investimentos": {"type": "number"}},
                    "additionalProperties": True,
                },
            },
            "required": ["contrato"],
            "additionalProperties": True,
        },
    },
    {
        "name": "mc_importar_planilha",
        "title": "Importar MC a partir da planilha (.xlsx)",
        "description": (
            "Forma preferida de registrar uma MC. Passe o link da planilha .xlsx no "
            "OneDrive/SharePoint e o Ops baixa, lê e extrai os valores por conta própria — "
            "você NÃO precisa abrir a planilha nem digitar número nenhum.\n\n"
            "Use esta tool em vez de mc_importar sempre que a planilha estiver na nuvem. "
            "Ela é melhor porque a leitura é determinística e os totais declarados saem da "
            "própria planilha, então a conferência do Ops sempre roda.\n\n"
            "A planilha precisa ser .xlsx. Se o arquivo for .xlsb, ele precisa ser salvo como "
            ".xlsx antes — nem o Ops nem você conseguem ler .xlsb.\n\n"
            "Contrato e ano são inferidos do nome do arquivo. Informe `contrato` "
            "explicitamente quando souber que o nome do arquivo está errado (já aconteceu: "
            "o assunto do e-mail dizia F260301 mas o código de faturamento era F260534). "
            "Os campos de cliente/projeto servem pra complementar com o que você leu no "
            "corpo do e-mail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Link da planilha .xlsx no OneDrive/SharePoint"},
                "contrato": {"type": "string", "description": "Sobrescreve o contrato inferido do nome do arquivo"},
                "ano": {"type": "string"},
                "cliente": {"type": "string"},
                "cliente_final": {"type": "string"},
                "projeto": {"type": "string"},
                "tcv": {"type": "number"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mc_listar",
        "title": "Listar MC's recebidas",
        "description": (
            "Lista as MC's já registradas no Ops, com status e totais. Use antes de importar "
            "para saber se aquele contrato/ano já entrou, e para achar o que está pendente "
            "de revisão."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["RECEBIDA", "PROCESSADA", "REVISAO_MANUAL", "ERRO"]},
                "contrato": {"type": "string", "description": "Filtro parcial pelo código do contrato"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mc_detalhe",
        "title": "Detalhe de uma MC",
        "description": (
            "Mostra uma MC com todas as linhas de equipe e de investimento, os totais "
            "declarados na planilha versus os somados, e o histórico de tentativas de "
            "ingestão. Use para entender por que uma MC caiu em revisão manual."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "id da MC (vem do mc_listar)"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "contratos_buscar",
        "title": "Buscar contrato no Ops",
        "description": (
            "Procura contratos cadastrados no Ops por parte do nome/código. Use quando uma "
            "MC cair em revisão manual por contrato não encontrado, para descobrir o nome "
            "exato que existe no sistema antes de sugerir o vínculo a uma pessoa."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"termo": {"type": "string", "description": "Parte do nome ou código"}},
            "required": ["termo"],
            "additionalProperties": False,
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
#  Montagem
# ─────────────────────────────────────────────────────────────────────────────

def montar_mcp(app, *, get_db, senha_confere, mc_processar_importacao,
               mc_consultar_lista, mc_consultar_detalhe,
               perfis_leitura, perfis_escrita,
               mc_importar_de_planilha=None, baixar_planilha=None):
    """Registra as rotas do MCP no app.

    As dependências entram por parâmetro (e não por `import main`) pra não criar
    import circular: o main.py importa este módulo."""

    # ── helpers de banco ────────────────────────────────────────────────────
    def _cursor():
        conn = get_db()
        if not conn:
            raise HTTPException(status_code=503, detail="Banco offline")
        cur = conn.cursor()
        _ensure_mcp_tables(cur)
        return conn, cur

    def _usuario(cur, uid):
        cur.execute("""SELECT id, nome, perfil, time, cargo FROM usuarios
                       WHERE id=%s AND ativo=TRUE""", (uid,))
        r = cur.fetchone()
        if not r:
            return None
        # 'dev' tem acesso equivalente a admin no resto do sistema (ver
        # get_session em main.py) -- mantém a mesma regra aqui.
        perfil = "admin" if r[2] == "dev" else r[2]
        return {"id": r[0], "nome": r[1], "perfil": perfil, "perfil_real": r[2],
                "time": r[3], "cargo": r[4] or ""}

    # ── descoberta (RFC 9728 / RFC 8414) ────────────────────────────────────
    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def mcp_meta_resource(request: Request):
        base = _base_url(request)
        return {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "scopes_supported": ["mc:ler", "mc:escrever"],
            "bearer_methods_supported": ["header"],
            "resource_name": "Faiston Ops — MC",
        }

    @app.get("/.well-known/oauth-authorization-server")
    def mcp_meta_as(request: Request):
        base = _base_url(request)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/mcp/oauth/authorize",
            "token_endpoint": f"{base}/mcp/oauth/token",
            "registration_endpoint": f"{base}/mcp/oauth/register",
            "scopes_supported": ["mc:ler", "mc:escrever"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            # PKCE obrigatório: sem S256 o code interceptado no redirect seria
            # trocável por token. 'plain' não entra de propósito.
            "code_challenge_methods_supported": ["S256"],
        }

    # ── registro dinâmico de cliente (RFC 7591) ─────────────────────────────
    @app.post("/mcp/oauth/register")
    async def mcp_oauth_register(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_client_metadata",
                                 "error_description": "corpo não é JSON"}, status_code=400)
        uris = body.get("redirect_uris") or []
        if not isinstance(uris, list) or not uris:
            return JSONResponse({"error": "invalid_redirect_uri",
                                 "error_description": "redirect_uris é obrigatório"}, status_code=400)
        for u in uris:
            p = urlparse(str(u))
            # Só https, exceto localhost no desenvolvimento: um redirect http
            # aberto na internet entrega o code pra quem estiver no caminho.
            if p.scheme != "https" and p.hostname not in ("localhost", "127.0.0.1"):
                return JSONResponse({"error": "invalid_redirect_uri",
                                     "error_description": f"redirect_uri precisa ser https: {u}"},
                                    status_code=400)
        client_id = "mcpc_" + secrets.token_hex(16)
        client_secret = secrets.token_urlsafe(32)
        conn, cur = _cursor()
        try:
            cur.execute("""INSERT INTO mcp_oauth_clients
                               (client_id, client_secret, client_name, redirect_uris)
                           VALUES (%s,%s,%s,%s)""",
                        (client_id, _hash(client_secret),
                         str(body.get("client_name") or "")[:200], json.dumps([str(u) for u in uris])))
            conn.commit()
        finally:
            cur.close(); conn.close()
        return JSONResponse({
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": body.get("client_name") or "",
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }, status_code=201)

    def _cliente(cur, client_id):
        cur.execute("SELECT client_id, client_secret, redirect_uris FROM mcp_oauth_clients WHERE client_id=%s",
                    (client_id,))
        r = cur.fetchone()
        if not r:
            return None
        return {"client_id": r[0], "secret_hash": r[1], "redirect_uris": json.loads(r[2])}

    # ── autorização: login do próprio Ops ───────────────────────────────────
    def _tela_login(base, params, erro=""):
        aviso = (f'<p class="erro">{erro}</p>' if erro else "")
        campos = "".join(
            f'<input type="hidden" name="{k}" value="{_html(v)}">' for k, v in params.items())
        return HTMLResponse(f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autorizar Claude — Faiston Ops</title>
<style>
 *{{box-sizing:border-box}}
 body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#07070F;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:20px}}
 .card{{background:#fff;border-radius:20px;padding:34px;width:100%;max-width:420px;
       box-shadow:0 20px 60px rgba(0,0,0,.45)}}
 .marca{{font-weight:900;font-size:22px;color:#07070F;letter-spacing:-.5px}}
 .marca span{{background:linear-gradient(135deg,#5B2EE0,#B826C9);-webkit-background-clip:text;
             -webkit-text-fill-color:transparent}}
 h1{{font-size:17px;margin:18px 0 6px}}
 p.sub{{color:#64748B;font-size:13px;margin:0 0 22px;line-height:1.5}}
 label{{display:block;font-size:11px;font-weight:700;color:#64748B;text-transform:uppercase;
       letter-spacing:.5px;margin-bottom:6px}}
 input[type=text],input[type=password]{{width:100%;padding:12px;border:1px solid #E2E8F0;
       border-radius:10px;font-size:14px;margin-bottom:16px}}
 input:focus{{outline:0;border-color:#5B2EE0}}
 button{{width:100%;padding:13px;border:0;border-radius:10px;color:#fff;font-weight:700;
        font-size:14px;cursor:pointer;background:linear-gradient(135deg,#5B2EE0,#B826C9)}}
 .erro{{background:rgba(236,72,153,.1);color:#BE185D;padding:10px 12px;border-radius:9px;
       font-size:13px;margin:0 0 16px}}
 .escopo{{background:#F8FAFC;border:1px solid #E2E8F0;border-radius:11px;padding:12px 14px;
         margin-bottom:20px;font-size:12.5px;color:#475569;line-height:1.6}}
</style></head><body>
<div class="card">
  <div class="marca">FAISTON <span>OPS</span></div>
  <h1>Autorizar acesso do Claude</h1>
  <p class="sub">Entre com o seu login do Faiston Ops para o Claude poder trabalhar com as MC's em seu nome.</p>
  {aviso}
  <div class="escopo">O Claude vai poder:<br>• ler as MC's registradas<br>• importar MC nova a partir da planilha de kick-off<br><br>Tudo fica registrado no histórico de ingestões com o seu nome.</div>
  <form method="post" action="{base}/mcp/oauth/authorize">
    {campos}
    <label>Usuário</label>
    <input type="text" name="usuario" autocomplete="username" autofocus required>
    <label>Senha</label>
    <input type="password" name="senha" autocomplete="current-password" required>
    <button type="submit">Autorizar</button>
  </form>
</div></body></html>""")

    def _html(v):
        return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;"))

    @app.get("/mcp/oauth/authorize")
    def mcp_oauth_authorize(request: Request):
        q = dict(request.query_params)
        erro = _validar_authorize(q)
        if erro:
            return JSONResponse({"error": "invalid_request", "error_description": erro}, status_code=400)
        return _tela_login(_base_url(request), {
            "client_id": q["client_id"], "redirect_uri": q["redirect_uri"],
            "code_challenge": q["code_challenge"], "state": q.get("state", ""),
            "scope": q.get("scope", ""), "resource": q.get("resource", ""),
        })

    def _validar_authorize(q):
        if q.get("response_type") not in (None, "code"):
            return "response_type precisa ser 'code'"
        if not q.get("client_id"):
            return "client_id ausente"
        if not q.get("redirect_uri"):
            return "redirect_uri ausente"
        if q.get("code_challenge_method", "S256") != "S256":
            return "code_challenge_method precisa ser S256"
        if not q.get("code_challenge"):
            return "code_challenge ausente (PKCE é obrigatório)"
        conn, cur = _cursor()
        try:
            cli = _cliente(cur, q["client_id"])
        finally:
            cur.close(); conn.close()
        if not cli:
            return "client_id desconhecido"
        if q["redirect_uri"] not in cli["redirect_uris"]:
            return "redirect_uri não registrada para este client_id"
        return None

    @app.post("/mcp/oauth/authorize")
    async def mcp_oauth_authorize_post(request: Request):
        form = await request.form()
        q = {k: str(form.get(k, "")) for k in
             ("client_id", "redirect_uri", "code_challenge", "state", "scope", "resource")}
        erro = _validar_authorize(q)
        if erro:
            return JSONResponse({"error": "invalid_request", "error_description": erro}, status_code=400)

        usuario = str(form.get("usuario", "")).strip()
        senha = str(form.get("senha", ""))
        base = _base_url(request)
        conn, cur = _cursor()
        try:
            cur.execute("""SELECT id, senha_hash, perfil FROM usuarios
                           WHERE LOWER(usuario)=LOWER(%s) AND ativo=TRUE""", (usuario,))
            row = cur.fetchone()
            if not row or not senha_confere(senha, row[1]):
                return _tela_login(base, q, "Usuário ou senha inválidos.")
            perfil = "admin" if row[2] == "dev" else row[2]
            if perfil not in perfis_leitura:
                return _tela_login(base, q, "Este perfil não tem acesso ao módulo de MC.")

            code = secrets.token_urlsafe(32)
            cur.execute("""INSERT INTO mcp_oauth_codes
                               (code, client_id, usuario_id, redirect_uri, code_challenge,
                                scope, expira_em)
                           VALUES (%s,%s,%s,%s,%s,%s, NOW() + INTERVAL '%s seconds')""",
                        (_hash(code), q["client_id"], row[0], q["redirect_uri"],
                         q["code_challenge"], q.get("scope") or "mc:ler mc:escrever", CODE_TTL))
            # Limpeza oportunista: code expirado não serve pra nada e a tabela
            # não deve crescer sem limite.
            cur.execute("DELETE FROM mcp_oauth_codes WHERE expira_em < NOW() - INTERVAL '1 day'")
            conn.commit()
        finally:
            cur.close(); conn.close()

        sep = "&" if "?" in q["redirect_uri"] else "?"
        params = {"code": code}
        if q.get("state"):
            params["state"] = q["state"]
        return RedirectResponse(q["redirect_uri"] + sep + urlencode(params), status_code=302)

    # ── token ───────────────────────────────────────────────────────────────
    def _emitir_token(cur, client_id, usuario_id, scope):
        access = secrets.token_urlsafe(40)
        refresh = secrets.token_urlsafe(40)
        cur.execute("""INSERT INTO mcp_oauth_tokens
                           (token_hash, refresh_hash, client_id, usuario_id, scope,
                            expira_em, refresh_expira_em)
                       VALUES (%s,%s,%s,%s,%s, NOW() + INTERVAL '%s seconds',
                                                NOW() + INTERVAL '%s seconds')""",
                    (_hash(access), _hash(refresh), client_id, usuario_id, scope,
                     TOKEN_TTL, REFRESH_TTL))
        return {"access_token": access, "token_type": "Bearer",
                "expires_in": TOKEN_TTL, "refresh_token": refresh, "scope": scope}

    @app.post("/mcp/oauth/token")
    async def mcp_oauth_token(request: Request):
        form = await request.form()
        grant = str(form.get("grant_type", ""))
        client_id = str(form.get("client_id", ""))
        conn, cur = _cursor()
        try:
            cli = _cliente(cur, client_id)
            if not cli:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            segredo = str(form.get("client_secret", ""))
            if segredo and _hash(segredo) != cli["secret_hash"]:
                return JSONResponse({"error": "invalid_client"}, status_code=401)

            if grant == "authorization_code":
                code = str(form.get("code", ""))
                verifier = str(form.get("code_verifier", ""))
                if not code or not verifier:
                    return JSONResponse({"error": "invalid_request",
                                         "error_description": "code e code_verifier são obrigatórios"},
                                        status_code=400)
                cur.execute("""SELECT usuario_id, redirect_uri, code_challenge, scope, usado
                               FROM mcp_oauth_codes
                               WHERE code=%s AND client_id=%s AND expira_em > NOW()""",
                            (_hash(code), client_id))
                r = cur.fetchone()
                if not r or r[4]:
                    return JSONResponse({"error": "invalid_grant",
                                         "error_description": "code inválido, expirado ou já usado"},
                                        status_code=400)
                if str(form.get("redirect_uri", r[1])) != r[1]:
                    return JSONResponse({"error": "invalid_grant",
                                         "error_description": "redirect_uri diferente da autorização"},
                                        status_code=400)
                # PKCE: S256(verifier) tem que dar o challenge guardado
                desafio = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if not secrets.compare_digest(desafio, r[2]):
                    return JSONResponse({"error": "invalid_grant",
                                         "error_description": "code_verifier não corresponde ao code_challenge"},
                                        status_code=400)
                # Uso único: marca antes de emitir, então um replay do mesmo
                # code não gera um segundo token.
                cur.execute("UPDATE mcp_oauth_codes SET usado=TRUE WHERE code=%s", (_hash(code),))
                out = _emitir_token(cur, client_id, r[0], r[3])
                conn.commit()
                return JSONResponse(out)

            if grant == "refresh_token":
                rt = str(form.get("refresh_token", ""))
                cur.execute("""SELECT id, usuario_id, scope FROM mcp_oauth_tokens
                               WHERE refresh_hash=%s AND client_id=%s
                                 AND revogado=FALSE AND refresh_expira_em > NOW()""",
                            (_hash(rt), client_id))
                r = cur.fetchone()
                if not r:
                    return JSONResponse({"error": "invalid_grant",
                                         "error_description": "refresh_token inválido ou expirado"},
                                        status_code=400)
                # Rotação: o refresh usado é revogado junto com seu access.
                cur.execute("UPDATE mcp_oauth_tokens SET revogado=TRUE WHERE id=%s", (r[0],))
                out = _emitir_token(cur, client_id, r[1], r[2])
                conn.commit()
                return JSONResponse(out)

            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        finally:
            cur.close(); conn.close()

    # ── autenticação das chamadas do MCP ────────────────────────────────────
    def _sessao_do_request(request: Request):
        """Resolve o usuário por trás do Bearer, ou None."""
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        token = auth[7:].strip()
        if not token:
            return None

        # Caminho alternativo: token fixo de ambiente, pra quem chama de fora
        # do fluxo OAuth do Claude.
        fixo = os.environ.get("MCP_API_TOKEN", "")
        if fixo and secrets.compare_digest(token, fixo):
            login = os.environ.get("MCP_API_TOKEN_USUARIO", "").strip()
            conn, cur = _cursor()
            try:
                if login:
                    cur.execute("""SELECT id FROM usuarios
                                   WHERE LOWER(usuario)=LOWER(%s) AND ativo=TRUE""", (login,))
                else:
                    cur.execute("""SELECT id FROM usuarios
                                   WHERE perfil IN ('admin','dev') AND ativo=TRUE
                                   ORDER BY id LIMIT 1""")
                r = cur.fetchone()
                if not r:
                    logger.error("MCP_API_TOKEN configurado mas nenhum usuário correspondente "
                                 "(MCP_API_TOKEN_USUARIO=%r)", login)
                    return None
                return _usuario(cur, r[0])
            finally:
                cur.close(); conn.close()

        conn, cur = _cursor()
        try:
            cur.execute("""SELECT id, usuario_id FROM mcp_oauth_tokens
                           WHERE token_hash=%s AND revogado=FALSE AND expira_em > NOW()""",
                        (_hash(token),))
            r = cur.fetchone()
            if not r:
                return None
            cur.execute("UPDATE mcp_oauth_tokens SET ultimo_uso=NOW() WHERE id=%s", (r[0],))
            sess = _usuario(cur, r[1])
            conn.commit()
            return sess
        finally:
            cur.close(); conn.close()

    def _nao_autorizado(request: Request):
        """401 com WWW-Authenticate apontando pro resource metadata.

        A spec de autorização do MCP exige esse header: é assim que o cliente
        descobre onde iniciar o OAuth em vez de simplesmente falhar."""
        base = _base_url(request)
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Não autenticado"}, "id": None},
            status_code=401,
            headers={"WWW-Authenticate":
                     f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})

    # ── execução das tools ──────────────────────────────────────────────────
    def _executar_tool(nome, args, sess):
        """Roda a tool e devolve (payload_json, is_error)."""
        if nome == "mc_importar":
            if sess["perfil"] not in perfis_escrita:
                return ({"erro": f"O perfil '{sess['perfil']}' não pode importar MC. "
                                 f"Perfis permitidos: {', '.join(perfis_escrita)}."}, True)
            r = mc_processar_importacao(dict(args or {}), sess)
            # Status não-PROCESSADA não é falha da chamada: a MC foi gravada.
            # Vai como sucesso, com o motivo explícito pro Claude poder avisar
            # a pessoa em vez de tentar de novo.
            if r.get("status") != "PROCESSADA":
                r = dict(r)
                r["atencao"] = ("A MC foi gravada, mas precisa de conferência humana: "
                                + (r.get("motivo_revisao") or r.get("status", "")))
            return (r, False)

        if nome == "mc_importar_planilha":
            if sess["perfil"] not in perfis_escrita:
                return ({"erro": f"O perfil '{sess['perfil']}' não pode importar MC."}, True)
            if not (mc_importar_de_planilha and baixar_planilha):
                return ({"erro": "Importação por planilha não está disponível nesta instância."}, True)
            url = str((args or {}).get("url") or "").strip()
            if not url:
                return ({"erro": "Informe a url da planilha .xlsx."}, True)
            if not url.lower().split("?")[0].endswith(".xlsx"):
                # Aviso, não bloqueio: link de compartilhamento do OneDrive
                # costuma não terminar em .xlsx. Quem valida de verdade é o
                # parser, olhando o conteúdo.
                logger.info("mc_importar_planilha: url sem extensão .xlsx (%s)", url[:120])
            try:
                conteudo = baixar_planilha(url)
            except Exception as e:
                return ({"erro": f"Não consegui baixar a planilha: {e}. Confira se o link "
                                 f"permite acesso sem login."}, True)
            extra = {k: v for k, v in (args or {}).items()
                     if k != "url" and v not in (None, "")}
            from mc_planilha import nome_do_arquivo_da_url
            nome_arq = nome_do_arquivo_da_url(url)
            r = mc_importar_de_planilha(conteudo, nome_arq, sess, extra)
            if r.get("status") != "PROCESSADA":
                r = dict(r)
                r["atencao"] = ("A MC foi gravada, mas precisa de conferência humana: "
                                + (r.get("motivo_revisao") or r.get("status", "")))
            return (r, False)

        if nome == "mc_listar":
            if sess["perfil"] not in perfis_leitura:
                return ({"erro": "Sem permissão de leitura de MC."}, True)
            return (mc_consultar_lista(str((args or {}).get("status") or ""),
                                       str((args or {}).get("contrato") or "")), False)

        if nome == "mc_detalhe":
            if sess["perfil"] not in perfis_leitura:
                return ({"erro": "Sem permissão de leitura de MC."}, True)
            mid = (args or {}).get("id")
            if not isinstance(mid, int):
                try:
                    mid = int(str(mid))
                except (TypeError, ValueError):
                    return ({"erro": "Informe o id numérico da MC."}, True)
            return (mc_consultar_detalhe(mid), False)

        if nome == "contratos_buscar":
            if sess["perfil"] not in perfis_leitura:
                return ({"erro": "Sem permissão."}, True)
            termo = str((args or {}).get("termo") or "").strip()
            if not termo:
                return ({"erro": "Informe o termo de busca."}, True)
            conn, cur = _cursor()
            try:
                cur.execute("""SELECT cg.id, cg.nome, COALESCE(c.nome,''), cg.status
                               FROM contratos_gestao cg
                               LEFT JOIN clientes c ON c.id = cg.cliente_id
                               WHERE cg.nome ILIKE %s ORDER BY cg.nome LIMIT 25""",
                            (f"%{termo}%",))
                contratos = [{"contrato_id": r[0], "nome": r[1], "cliente": r[2], "status": r[3]}
                             for r in cur.fetchall()]
                # O código do contrato (F260015) vive no forecast, não em
                # contratos_gestao -- procura nos dois pra não dar "não achei"
                # quando o código existe do outro lado.
                cur.execute("SELECT to_regclass('public.forecast_projetos') IS NOT NULL")
                projetos = []
                if cur.fetchone()[0]:
                    cur.execute("""SELECT codigo, cliente, cliente_final, projeto, status
                                   FROM forecast_projetos
                                   WHERE codigo ILIKE %s OR projeto ILIKE %s
                                   ORDER BY codigo LIMIT 25""", (f"%{termo}%", f"%{termo}%"))
                    projetos = [{"codigo": r[0], "cliente": r[1], "cliente_final": r[2],
                                 "projeto": r[3], "status": r[4]} for r in cur.fetchall()]
                return ({"contratos_gestao": contratos, "forecast_projetos": projetos}, False)
            finally:
                cur.close(); conn.close()

        return ({"erro": f"Tool desconhecida: {nome}"}, True)

    # ── o endpoint MCP (JSON-RPC 2.0) ───────────────────────────────────────
    def _rpc_erro(rid, code, msg):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

    @app.get("/mcp")
    def mcp_get():
        # O canal SSE de servidor→cliente é opcional na spec e este servidor
        # não emite nada espontâneo, então recusa explicitamente em vez de
        # deixar o cliente esperando uma stream que nunca vem.
        return JSONResponse({"detail": "Use POST para falar com este endpoint MCP."},
                            status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp")
    async def mcp_post(request: Request):
        sess = _sessao_do_request(request)
        if not sess:
            return _nao_autorizado(request)
        try:
            corpo = await request.json()
        except Exception:
            return JSONResponse(_rpc_erro(None, -32700, "JSON inválido"), status_code=400)

        lote = isinstance(corpo, list)
        mensagens = corpo if lote else [corpo]
        respostas = []
        for msg in mensagens:
            r = _tratar_mensagem(msg, sess)
            if r is not None:
                respostas.append(r)
        if not respostas:
            # Só notificações no lote: a spec pede 202 sem corpo.
            return JSONResponse(None, status_code=202)
        return JSONResponse(respostas if lote else respostas[0])

    def _tratar_mensagem(msg, sess):
        if not isinstance(msg, dict):
            return _rpc_erro(None, -32600, "Mensagem inválida")
        metodo = msg.get("method")
        rid = msg.get("id")
        eh_notificacao = "id" not in msg

        if metodo == "initialize":
            pedida = ((msg.get("params") or {}).get("protocolVersion") or "").strip()
            versao = pedida if pedida in MCP_PROTOCOLOS_ACEITOS else MCP_PROTOCOLO_PREFERIDO
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": versao,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "faiston-ops-mc", "version": "1.0.0",
                               "title": "Faiston Ops — MC"},
                "instructions": (
                    "Conector do Faiston Ops para MC (Margem de Contribuição).\n\n"
                    "Fluxo esperado: quando chegar um e-mail de KICK-OFF, registre o que dá "
                    "com mc_importar (contrato, cliente, projeto do corpo do e-mail) — a MC "
                    "fica RECEBIDA aguardando a planilha. Quando a planilha .xlsx estiver no "
                    "OneDrive, chame mc_importar_planilha com o link: o Ops baixa e extrai os "
                    "valores sozinho, e isso substitui o registro anterior sem duplicar.\n\n"
                    "Não transcreva números de planilha à mão se houver .xlsx acessível — "
                    "deixe o Ops ler. Se só houver .xlsb, ele precisa ser salvo como .xlsx "
                    "primeiro; nem você nem o Ops leem .xlsb.\n\n"
                    "Nunca invente contrato ou linha de custo. Se a planilha não traz, omita. "
                    f"Você está autenticado como {sess['nome']} (perfil {sess['perfil']})."),
            }}

        if eh_notificacao:
            return None   # notifications/initialized, cancelled, etc.

        if metodo == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}

        if metodo == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

        if metodo == "tools/call":
            params = msg.get("params") or {}
            nome = params.get("name")
            args = params.get("arguments") or {}
            if not nome:
                return _rpc_erro(rid, -32602, "params.name é obrigatório")
            try:
                payload, erro = _executar_tool(nome, args, sess)
            except HTTPException as e:
                payload, erro = ({"erro": str(e.detail), "http_status": e.status_code}, True)
            except Exception as e:
                logger.error("Erro na tool MCP %s: %s", nome, e, exc_info=True)
                payload, erro = ({"erro": f"Falha interna ao executar {nome}: {e}"}, True)
            texto = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": texto}],
                "structuredContent": payload if isinstance(payload, dict) else {"resultado": payload},
                "isError": bool(erro),
            }}

        if metodo in ("resources/list", "prompts/list"):
            # Declaramos só tools em capabilities, mas alguns clientes chamam
            # de todo jeito -- lista vazia é mais amigável que erro.
            chave = "resources" if metodo.startswith("resources") else "prompts"
            return {"jsonrpc": "2.0", "id": rid, "result": {chave: []}}

        return _rpc_erro(rid, -32601, f"Método não suportado: {metodo}")

    logger.info("MCP montado em /mcp (%d tools)", len(TOOLS))
