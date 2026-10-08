"""Notas pessoais.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.auth import get_session
from app.core.db import get_db

router = APIRouter()


# --- NOTAS PESSOAIS ---
class NotaModel(BaseModel):
    titulo: str
    texto: str

@router.get("/api/notas")
def listar_notas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                titulo VARCHAR(200) NOT NULL,
                texto TEXT,
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.execute("""SELECT id, titulo, texto, criado_em, atualizado_em
            FROM notas WHERE usuario_id = %s ORDER BY atualizado_em DESC""",
            (sess["id"],))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "titulo": r[1], "texto": r[2],
                 "criado_em": str(r[3])[:16], "atualizado_em": str(r[4])[:16]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/notas")
def criar_nota(n: NotaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO notas (usuario_id, titulo, texto) VALUES (%s,%s,%s) RETURNING id",
                    (sess["id"], n.titulo, n.texto))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/notas/{nid}")
def atualizar_nota(nid: int, n: NotaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE notas SET titulo=%s, texto=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
                    (n.titulo, n.texto, nid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/notas/{nid}")
def deletar_nota(nid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM notas WHERE id=%s AND usuario_id=%s", (nid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/notas/{nid}/analise")
def analisar_nota(nid: int, faiston_token: str = Cookie(None)):
    import os, json as _json, http.client, ssl
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key: raise HTTPException(status_code=500, detail="GROQ_API_KEY não configurada")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT titulo, texto FROM notas WHERE id=%s AND usuario_id=%s", (nid, sess["id"]))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row: raise HTTPException(status_code=404, detail="Nota não encontrada")
        titulo, texto = row
        prompt = f"""Analise esta nota de trabalho e forneça insights práticos em português brasileiro.

TÍTULO: {titulo}
CONTEÚDO:
{texto or '(sem conteúdo)'}

Responda de forma estruturada com exatamente estas 3 seções:

📋 RESUMO
(1-2 frases objetivas sobre o conteúdo)

⚡ AÇÕES SUGERIDAS
(até 3 itens com "-" no início, foco no que fazer a seguir)

🏷️ TIPO
(classifique em uma palavra: Tarefa / Lembrete / Ideia / Problema / Reunião / Análise / Outro)

Seja direto e prático."""

        body_json = _json.dumps({
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.4
        })
        ctx = ssl.create_default_context()
        hconn = http.client.HTTPSConnection("api.groq.com", timeout=25, context=ctx)
        hconn.request("POST", "/openai/v1/chat/completions", body=body_json, headers={
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json",
            "User-Agent": "python-httpx/0.27",
            "Accept": "application/json",
        })
        resp = hconn.getresponse()
        resp_body = resp.read().decode()
        hconn.close()
        if resp.status != 200:
            raise HTTPException(status_code=500, detail=f"Groq {resp.status}: {resp_body}")
        result = _json.loads(resp_body)
        analise = result["choices"][0]["message"]["content"].strip()
        return {"analise": analise}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
