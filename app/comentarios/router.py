"""Comentários das tarefas.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.auth import get_session
from app.core.db import get_db

router = APIRouter()


# --- COMENTÁRIOS ---
class ComentarioModel(BaseModel):
    texto: str

@router.get("/api/tarefas/{tid}/comentarios")
def listar_comentarios(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""SELECT c.id, c.texto, c.criado_em, u.nome
            FROM comentarios c JOIN usuarios u ON c.usuario_id = u.id
            WHERE c.tarefa_id = %s ORDER BY c.criado_em ASC""", (tid,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "texto": r[1], "criado_em": str(r[2]), "autor": r[3]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tarefas/{tid}/comentarios")
def criar_comentario(tid: int, c: ComentarioModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO comentarios (tarefa_id, usuario_id, texto) VALUES (%s,%s,%s) RETURNING id",
                    (tid, sess["id"], c.texto))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/comentarios/{cid}")
def deletar_comentario(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] in ("admin", "gestor", "demo"):
            cur.execute("DELETE FROM comentarios WHERE id=%s", (cid,))
        else:
            cur.execute("DELETE FROM comentarios WHERE id=%s AND usuario_id=%s", (cid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
