"""Cadastro de clientes.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel
import psycopg2

from app.core.acesso import _times_validos_cur
from app.core.auth import get_session
from app.core.db import get_db

# --- corpo ---
router = APIRouter()


# --- CLIENTES ---
# 2026-07-28: removida uma migração "migrar clientes das tarefas" que
# rodava aqui a cada listagem (não uma vez só) -- recriava silenciosamente
# um cliente em texto livre de tarefas.cliente toda vez que alguém abria a
# tela, mesmo depois de apagado (foi assim que "Arcos" duplicado voltou
# depois de já ter sido limpo).
@router.get("/api/clientes")
def listar_clientes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(100) UNIQUE NOT NULL,
                contato VARCHAR(100),
                email VARCHAR(100),
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS time VARCHAR(50) DEFAULT 'Projetos'")
        conn.commit()
        if sess["perfil"] == "admin":
            cur.execute("SELECT id, nome, contato, email, ativo, criado_em, COALESCE(time,'Projetos') FROM clientes WHERE ativo=TRUE ORDER BY nome")
        else:
            cur.execute("SELECT id, nome, contato, email, ativo, criado_em, COALESCE(time,'Projetos') FROM clientes WHERE ativo=TRUE AND COALESCE(time,'Projetos')=%s ORDER BY nome", (sess.get("time","Projetos"),))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1], "contato": r[2], "email": r[3], "ativo": r[4], "criado_em": str(r[5])[:10], "time": r[6]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class ClienteModel(BaseModel):
    nome: str
    contato: str = ""
    email: str = ""
    ativo: bool = True
    time: str = "Projetos"

@router.post("/api/clientes")
def criar_cliente(c: ClienteModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        time_val = c.time if (sess["perfil"] == "admin" and c.time in _times_validos_cur(cur)) else sess.get("time", "Projetos")
        cur.execute("INSERT INTO clientes (nome, contato, email, time) VALUES (%s,%s,%s,%s) RETURNING id",
                    (c.nome, c.contato, c.email, time_val))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Cliente já existe")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/clientes/{cid}")
def atualizar_cliente(cid: int, c: ClienteModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE clientes SET nome=%s, contato=%s, email=%s, ativo=%s WHERE id=%s",
                    (c.nome, c.contato, c.email, c.ativo, cid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/clientes/{cid}")
def deletar_cliente(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE clientes SET ativo=FALSE WHERE id=%s", (cid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
