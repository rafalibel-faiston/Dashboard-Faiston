"""Notificações.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""

from fastapi import APIRouter, Cookie, HTTPException

from app.core.auth import get_session
from app.core.db import get_db

router = APIRouter()


# --- NOTIFICAÇÕES ---
def criar_notificacao(conn, tipo: str, mensagem: str, usuario_id: int = None, destinatario_id: int = None):
    # Usa SAVEPOINT para que uma falha ao gravar a notificação NÃO aborte a
    # transação principal. destinatario_id=None → notificação global (gestores).
    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT sp_notif")
        cur.execute(
            "INSERT INTO notificacoes (tipo, mensagem, usuario_id, destinatario_id) VALUES (%s, %s, %s, %s)",
            (tipo, mensagem, usuario_id, destinatario_id)
        )
        cur.execute("DELETE FROM notificacoes WHERE id NOT IN (SELECT id FROM notificacoes ORDER BY criado_em DESC LIMIT 200)")
        cur.execute("RELEASE SAVEPOINT sp_notif")
    except Exception:
        try: cur.execute("ROLLBACK TO SAVEPOINT sp_notif")
        except Exception: pass
    finally:
        cur.close()

@router.get("/api/notificacoes")
def get_notificacoes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, tipo, mensagem, lida, criado_em FROM notificacoes WHERE destinatario_id IS NULL ORDER BY criado_em DESC LIMIT 20")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "tipo": r[1], "mensagem": r[2], "lida": r[3], "criado_em": str(r[4])} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/notificacoes/marcar-lidas")
def marcar_lidas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE notificacoes SET lida = TRUE WHERE lida = FALSE AND destinatario_id IS NULL")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/notificacoes/{notif_id}/marcar-lida")
def marcar_uma_lida(notif_id: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    if sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE notificacoes SET lida = TRUE WHERE id = %s AND destinatario_id IS NULL", (notif_id,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/notificacoes/nao-lidas")
def count_nao_lidas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    if sess["perfil"] not in ("admin", "gestor", "demo"): return {"count": 0}
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notificacoes WHERE lida = FALSE AND destinatario_id IS NULL")
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        return {"count": count}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/minhas-notificacoes")
def get_minhas_notificacoes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, tipo, mensagem, lida, criado_em FROM notificacoes WHERE destinatario_id = %s ORDER BY criado_em DESC LIMIT 20",
            (sess["id"],)
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "tipo": r[1], "mensagem": r[2], "lida": r[3], "criado_em": str(r[4])} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/minhas-notificacoes/marcar-lidas")
def marcar_minhas_lidas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE notificacoes SET lida = TRUE WHERE destinatario_id = %s AND lida = FALSE", (sess["id"],))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/minhas-notificacoes/{notif_id}/marcar-lida")
def marcar_minha_uma_lida(notif_id: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE notificacoes SET lida = TRUE WHERE id = %s AND destinatario_id = %s", (notif_id, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/minhas-notificacoes/nao-lidas")
def count_minhas_nao_lidas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notificacoes WHERE destinatario_id = %s AND lida = FALSE", (sess["id"],))
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        return {"count": count}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
