"""Carimbos (textos prontos por área).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.auth import get_session
from app.core.db import get_db

router = APIRouter()


# --- CARIMBOS ---
class CarimboModel(BaseModel):
    titulo: str
    categoria: str = "Geral"
    conteudo: str = ""

@router.get("/api/carimbos")
def listar_carimbos(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS carimbos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER,
                titulo VARCHAR(200) NOT NULL,
                categoria VARCHAR(100) DEFAULT 'Geral',
                conteudo TEXT DEFAULT '',
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE carimbos ADD COLUMN IF NOT EXISTS criado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")
        cur.execute("ALTER TABLE carimbos ADD COLUMN IF NOT EXISTS time_usuario VARCHAR(50) DEFAULT 'Projetos'")
        conn.commit()
        # Admin vê tudo; demais perfis veem apenas os carimbos do próprio time
        time_sess = sess.get("time") or "Projetos"
        if sess["perfil"] == "admin":
            cur.execute("""
                SELECT c.id, c.titulo, c.categoria, c.conteudo, c.criado_em, c.atualizado_em, c.criado_por
                FROM carimbos c ORDER BY c.categoria, c.titulo
            """)
        else:
            cur.execute("""
                SELECT c.id, c.titulo, c.categoria, c.conteudo, c.criado_em, c.atualizado_em, c.criado_por
                FROM carimbos c WHERE c.time_usuario = %s ORDER BY c.categoria, c.titulo
            """, (time_sess,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "titulo": r[1], "categoria": r[2], "conteudo": r[3],
                 "criado_em": str(r[4])[:16], "atualizado_em": str(r[5])[:16],
                 "meu": r[6] == sess["id"] or sess["perfil"] == "admin"} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/carimbos")
def criar_carimbo(c: CarimboModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        time_sess = sess.get("time") or "Projetos"
        # Nome único por time (não globalmente)
        cur.execute("SELECT id FROM carimbos WHERE LOWER(titulo) = LOWER(%s) AND time_usuario = %s", (c.titulo, time_sess))
        if cur.fetchone(): raise HTTPException(status_code=400, detail="Já existe um carimbo com este nome no seu time")
        cur.execute("INSERT INTO carimbos (criado_por, titulo, categoria, conteudo, time_usuario) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (sess["id"], c.titulo, c.categoria, c.conteudo, time_sess))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/carimbos/{cid}")
def atualizar_carimbo(cid: int, c: CarimboModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        time_sess = sess.get("time") or "Projetos"
        cur.execute("SELECT id FROM carimbos WHERE LOWER(titulo) = LOWER(%s) AND id != %s AND time_usuario = %s", (c.titulo, cid, time_sess))
        if cur.fetchone(): raise HTTPException(status_code=400, detail="Já existe um carimbo com este nome no seu time")
        cur.execute("UPDATE carimbos SET titulo=%s, categoria=%s, conteudo=%s, atualizado_em=NOW() WHERE id=%s AND criado_por=%s",
                    (c.titulo, c.categoria, c.conteudo, cid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/carimbos/{cid}")
def deletar_carimbo(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] == "admin":
            cur.execute("DELETE FROM carimbos WHERE id=%s", (cid,))
        else:
            cur.execute("DELETE FROM carimbos WHERE id=%s AND criado_por=%s", (cid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
