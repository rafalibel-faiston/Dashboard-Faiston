"""Login, logout, troca e redefinição de senha, /api/me e tutoriais.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
import hashlib
import secrets

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.auth import _senha_fraca, get_session, hash_senha, senha_confere
from app.core.db import get_db
from app.core.email import _brevo_send, _resolver_system_url, _shell_email


router = APIRouter()


# --- MODELOS ---
class LoginRequest(BaseModel):
    usuario: str
    senha: str


class TrocarSenhaModel(BaseModel):
    nova_senha: str


# --- AUTH ---
# ── Rate limiting de login (in-memory; processo único no Railway) ─────────────
import time as _time
from collections import deque as _deque
_LOGIN_FAILS: dict = {}          # chave (ip:.. / user:..) -> deque[timestamps de falha]
_LOGIN_WINDOW_S = 300            # janela deslizante de 5 minutos
_LOGIN_MAX_FAILS = 8             # falhas por janela antes de bloquear temporariamente

def _login_ip(request: Request) -> str:
    """IP real do cliente — respeita X-Forwarded-For atrás do proxy do Railway."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"

def _login_bloqueado(chaves) -> bool:
    agora = _time.time()
    for k in chaves:
        dq = _LOGIN_FAILS.get(k)
        if not dq:
            continue
        while dq and agora - dq[0] > _LOGIN_WINDOW_S:
            dq.popleft()
        if len(dq) >= _LOGIN_MAX_FAILS:
            return True
    return False

def _login_registrar_falha(chaves):
    agora = _time.time()
    for k in chaves:
        _LOGIN_FAILS.setdefault(k, _deque()).append(agora)

def _login_limpar(chaves):
    for k in chaves:
        _LOGIN_FAILS.pop(k, None)

@router.post("/api/login")
def login(req: LoginRequest, response: Response, request: Request):
    chaves = (f"ip:{_login_ip(request)}", f"user:{(req.usuario or '').strip().lower()}")
    if _login_bloqueado(chaves):
        raise HTTPException(status_code=429,
                            detail="Muitas tentativas de login. Aguarde alguns minutos e tente novamente.")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, perfil, COALESCE(primeiro_acesso, FALSE), COALESCE(time,'Projetos'), COALESCE(cargo,''), senha_hash FROM usuarios WHERE usuario=%s AND ativo=TRUE",
                    (req.usuario,))
        row = cur.fetchone()
        if not row or not senha_confere(req.senha, row[6]):
            cur.close(); conn.close()
            _login_registrar_falha(chaves)
            raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
        # Migração transparente: quem ainda estava no hash antigo tem a senha
        # reescrita em bcrypt neste login, sem precisar trocar de senha.
        if not row[6].startswith("$2"):
            cur.execute("UPDATE usuarios SET senha_hash=%s WHERE id=%s", (hash_senha(req.senha), row[0]))
        token = secrets.token_hex(32)
        cur.execute("""
            INSERT INTO sessoes (token, usuario_id, nome, perfil, time_usuario, pagina, cargo, expira_em)
            VALUES (%s, %s, %s, %s, %s, 'dashboard', %s, NOW() + INTERVAL '24 hours')
        """, (token, row[0], row[1], row[2], row[4], row[5]))
        # Registra o último acesso (data/hora do login bem-sucedido)
        cur.execute("UPDATE usuarios SET ultimo_acesso = NOW() WHERE id = %s", (row[0],))
        conn.commit(); cur.close(); conn.close()
        _login_limpar(chaves)  # login OK zera o contador de falhas
        response.set_cookie("faiston_token", token, httponly=True, samesite="lax", secure=True, max_age=86400)
        # Cookie de CSRF (double-submit) -- deliberadamente NÃO httponly, o JS
        # do front precisa ler o valor pra ecoar no header X-CSRF-Token em toda
        # requisição que muda estado. A proteção não depende de sigilo desse
        # valor, depende de um site de outra origem não conseguir LER o cookie
        # (same-origin policy) pra montar o header correspondente.
        response.set_cookie("csrf_token", secrets.token_hex(16), httponly=False, samesite="lax", secure=True, max_age=86400)
        return {"sucesso": True, "perfil": row[2], "cargo": row[5], "nome": row[1], "primeiro_acesso": bool(row[3])}
    except HTTPException: raise
    except Exception:
        raise HTTPException(status_code=500, detail="Erro ao processar login")

@router.post("/api/logout")
def logout(response: Response, faiston_token: str = Cookie(None)):
    if faiston_token:
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM sessoes WHERE token = %s", (faiston_token,))
                conn.commit(); cur.close(); conn.close()
            except Exception: pass
    response.delete_cookie("faiston_token")
    response.delete_cookie("csrf_token")
    return {"sucesso": True}

@router.post("/api/trocar-senha")
def trocar_senha(body: TrocarSenhaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    erro = _senha_fraca(body.nova_senha, sess.get("nome", ""))
    if erro: raise HTTPException(status_code=400, detail=erro)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET senha_hash=%s, primeiro_acesso=FALSE WHERE id=%s",
                    (hash_senha(body.nova_senha), sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Esqueci minha senha (2026-07-30) ──────────────────────────────────────
# Token de uso único, expira em 30 min, e o e-mail nunca confirma se o
# usuário existe (evita enumerar contas válidas por tentativa e erro).
_RESET_JANELA_S = 900          # 15 min
_RESET_MAX_PEDIDOS = 3         # pedidos por janela antes de segurar
_RESET_PEDIDOS: dict = {}

class EsqueciSenhaModel(BaseModel):
    email: str

class RedefinirSenhaModel(BaseModel):
    token: str
    nova_senha: str

def _reset_bloqueado(chave) -> bool:
    agora = _time.time()
    dq = _RESET_PEDIDOS.get(chave)
    if not dq: return False
    while dq and agora - dq[0] > _RESET_JANELA_S: dq.popleft()
    return len(dq) >= _RESET_MAX_PEDIDOS

def _reset_registrar_pedido(chave):
    _RESET_PEDIDOS.setdefault(chave, _deque()).append(_time.time())

def _gerar_token_redefinicao(cur, uid: int) -> str:
    """Token de definição/redefinição de senha, uso único, expira em 30min.
    Substitui qualquer token anterior ainda ativo pra esse usuário. Reaproveitado
    pelo "esqueci minha senha" e pelo email de boas-vindas de conta nova (sem
    commit -- quem chama decide quando commitar)."""
    cur.execute("DELETE FROM senha_reset_tokens WHERE usuario_id=%s", (uid,))
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cur.execute("""
        INSERT INTO senha_reset_tokens (usuario_id, token_hash, expira_em)
        VALUES (%s, %s, NOW() + INTERVAL '30 minutes')
    """, (uid, token_hash))
    return token

@router.post("/api/esqueci-senha")
def esqueci_senha(body: EsqueciSenhaModel, request: Request):
    resposta_generica = {"sucesso": True, "mensagem": "Se o email existir, um e-mail com instruções foi enviado."}
    chave_ip = f"reset:ip:{_login_ip(request)}"
    chave_email = f"reset:email:{(body.email or '').strip().lower()}"
    if _reset_bloqueado(chave_ip) or _reset_bloqueado(chave_email):
        # Mesma resposta genérica de sucesso -- não revela rate limit pra
        # quem está tentando enumerar/abusar, só não manda o e-mail de novo.
        return resposta_generica
    _reset_registrar_pedido(chave_ip)
    _reset_registrar_pedido(chave_email)
    conn = get_db()
    if not conn: return resposta_generica
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, email FROM usuarios WHERE LOWER(email)=LOWER(%s) AND ativo=TRUE", ((body.email or "").strip(),))
        row = cur.fetchone()
        if row and row[2]:
            uid, nome, email = row
            token = _gerar_token_redefinicao(cur, uid)
            conn.commit()
            system_url = _resolver_system_url(request)
            link = f"{system_url}/redefinir-senha?token={token}"
            corpo = f"""
                <p style="color:#3D4152;font-size:14.5px;margin:0 0 18px;line-height:1.6">Olá, {nome}. Recebemos um pedido pra redefinir sua senha no Faiston OPS.</p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
                  <tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
                    <a href="{link}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">Redefinir minha senha &nbsp;&rarr;</a>
                  </td></tr>
                </table>
                <p style="color:#8A8FA3;font-size:12.5px;margin:0;line-height:1.6">Esse link expira em <strong>30 minutos</strong> e só funciona uma vez. Se você não pediu essa troca, pode ignorar este e-mail — sua senha continua a mesma.</p>
            """
            _brevo_send(email, "Redefinir sua senha — Faiston OPS",
                        _shell_email("Redefinir senha", "Link expira em 30 minutos", corpo))
        cur.close(); conn.close()
    except Exception as e:
        print(f"[esqueci-senha] erro: {e}")
    return resposta_generica

@router.post("/api/redefinir-senha")
def redefinir_senha(body: RedefinirSenhaModel):
    if not body.token: raise HTTPException(status_code=400, detail="Link inválido ou expirado.")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        token_hash = hashlib.sha256(body.token.encode()).hexdigest()
        cur.execute("""
            SELECT id, usuario_id FROM senha_reset_tokens
            WHERE token_hash=%s AND usado_em IS NULL AND expira_em > NOW()
        """, (token_hash,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail="Link inválido ou expirado. Peça um novo.")
        rid, uid = row
        cur.execute("SELECT usuario FROM usuarios WHERE id=%s", (uid,))
        usuario_row = cur.fetchone()
        erro = _senha_fraca(body.nova_senha, usuario_row[0] if usuario_row else "")
        if erro:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail=erro)
        cur.execute("UPDATE usuarios SET senha_hash=%s, primeiro_acesso=FALSE WHERE id=%s",
                    (hash_senha(body.nova_senha), uid))
        # Uso único: marca gasto -- essa mesma consulta nunca mais bate no
        # WHERE usado_em IS NULL de cima, então o link não é reaproveitável.
        cur.execute("UPDATE senha_reset_tokens SET usado_em=NOW() WHERE id=%s", (rid,))
        # Derruba sessões ativas -- se a conta foi comprometida e por isso
        # pediu reset, a sessão de quem invadiu também precisa cair.
        cur.execute("DELETE FROM sessoes WHERE usuario_id=%s", (uid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/me")
def me(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    return sess

# Tutorial guiado do N2 (2026-07-30) -- flag por usuário, não por sessão,
# pra "primeiro login" valer entre dispositivos/navegadores diferentes.
@router.get("/api/tutorial-n2/status")
def tutorial_n2_status(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(tutorial_n2_visto, FALSE) FROM usuarios WHERE id=%s", (sess["id"],))
        row = cur.fetchone()
        cur.close(); conn.close()
        return {"visto": bool(row[0]) if row else False}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tutorial-n2/visto")
def tutorial_n2_marcar_visto(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET tutorial_n2_visto=TRUE WHERE id=%s", (sess["id"],))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# Tour do gestor (2026-10-07) -- mesmo padrão do tutorial do N2: abre sozinho
# no primeiro acesso ao /dashboard e pode ser revisto pelo menu.
@router.get("/api/tutorial-gestor/status")
def tutorial_gestor_status(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(tutorial_gestor_visto, FALSE) FROM usuarios WHERE id=%s", (sess["id"],))
        row = cur.fetchone()
        cur.close(); conn.close()
        return {"visto": bool(row[0]) if row else False}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tutorial-gestor/visto")
def tutorial_gestor_marcar_visto(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET tutorial_gestor_visto=TRUE WHERE id=%s", (sess["id"],))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
