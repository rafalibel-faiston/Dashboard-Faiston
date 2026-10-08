"""Painel de Controle do N2.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.acesso import _pode_ver_status_report
from app.core.agenda import _bloqueio_ativo
from app.core.auth import get_session
from app.core.db import get_db
from app.status_campo.router import _resolver_n2

# --- corpo ---
router = APIRouter()


# ── Painel de Controle do N2 ─────────────────────────────────────────────────
MODALIDADES_ESCALA = ('presencial', 'home', 'externo')

class EscalaN2Model(BaseModel):
    data: str
    n2_usuario_id: int
    horario_entrada: Optional[str] = None
    modalidade: str = "presencial"
    atribuicao: str = ""

@router.get("/api/escala-n2")
def listar_escala_n2(data: str = "", faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if data:
            cur.execute("""
                SELECT e.id, e.data, e.n2_usuario_id, u.nome, e.horario_entrada, e.modalidade, e.atribuicao
                FROM escala_n2 e JOIN usuarios u ON u.id = e.n2_usuario_id
                WHERE e.data = %s ORDER BY e.horario_entrada ASC NULLS LAST, u.nome
            """, (data,))
        else:
            cur.execute("""
                SELECT e.id, e.data, e.n2_usuario_id, u.nome, e.horario_entrada, e.modalidade, e.atribuicao
                FROM escala_n2 e JOIN usuarios u ON u.id = e.n2_usuario_id
                WHERE e.data = CURRENT_DATE ORDER BY e.horario_entrada ASC NULLS LAST, u.nome
            """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "data": str(r[1]), "n2_usuario_id": r[2], "n2_nome": r[3],
                 "horario_entrada": str(r[4])[:5] if r[4] else None, "modalidade": r[5], "atribuicao": r[6]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _escala_data_valida(data_str):
    """Valida a data antes de gravar em escala_n2 -- o Postgres aceita
    qualquer ano no tipo DATE (mesmo um com dígito a mais), mas o
    psycopg2/Python trava ao ler de volta (datetime só vai até 9999),
    derrubando a consulta inteira por causa de uma única linha ruim. Mesmo
    bug já visto e corrigido em dev_tarefas.prazo (2026-07-27) -- validar
    aqui evita repetir a história noutra tabela."""
    try:
        date.fromisoformat(data_str)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Data inválida — use o formato AAAA-MM-DD")

@router.post("/api/escala-n2")
def criar_escala_n2(e: EscalaN2Model, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    _escala_data_valida(e.data)
    modalidade = e.modalidade if e.modalidade in MODALIDADES_ESCALA else "presencial"
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        bloqueio = _bloqueio_ativo(cur, e.n2_usuario_id, e.data, e.horario_entrada)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"N2 indisponível nesta data/horário: {bloqueio}")
        cur.execute("""
            INSERT INTO escala_n2 (data, n2_usuario_id, horario_entrada, modalidade, atribuicao)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (e.data, e.n2_usuario_id, e.horario_entrada or None, modalidade, e.atribuicao))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e2: raise HTTPException(status_code=500, detail=str(e2))

@router.put("/api/escala-n2/{eid}")
def atualizar_escala_n2(eid: int, e: EscalaN2Model, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    _escala_data_valida(e.data)
    modalidade = e.modalidade if e.modalidade in MODALIDADES_ESCALA else "presencial"
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        bloqueio = _bloqueio_ativo(cur, e.n2_usuario_id, e.data, e.horario_entrada)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"N2 indisponível nesta data/horário: {bloqueio}")
        cur.execute("""
            UPDATE escala_n2 SET data=%s, n2_usuario_id=%s, horario_entrada=%s, modalidade=%s, atribuicao=%s, atualizado_em=NOW()
            WHERE id=%s
        """, (e.data, e.n2_usuario_id, e.horario_entrada or None, modalidade, e.atribuicao, eid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e2: raise HTTPException(status_code=500, detail=str(e2))

@router.delete("/api/escala-n2/{eid}")
def deletar_escala_n2(eid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM escala_n2 WHERE id=%s", (eid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class AtribuirLoteModel(BaseModel):
    cliente_id: int
    data: str
    quantidade: int
    n2_usuario_id: int

@router.post("/api/status-campo/atribuir-lote")
def atribuir_lote_status_campo(body: AtribuirLoteModel, faiston_token: str = Cookie(None)):
    """Usado pelo Gerador de Escala: atribui de verdade o N2 a até
    `quantidade` atividades já existentes (sem N2 ainda) daquele
    cliente/data -- gerar escala sem isso só criava um registro de
    plantão sem vínculo real com as atividades."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    if not _pode_ver_status_report(sess): raise HTTPException(status_code=403, detail="Status Report é restrito ao time de Projetos")
    try:
        if datetime.strptime(body.data, "%Y-%m-%d").date() < date.today():
            raise HTTPException(status_code=400, detail="Não é possível gerar escala pra uma data que já passou")
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        bloqueio = _bloqueio_ativo(cur, body.n2_usuario_id, body.data)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"N2 indisponível nesta data: {bloqueio}")
        _, n2_nome = _resolver_n2(cur, body.n2_usuario_id, "")
        cur.execute("""
            UPDATE status_atividades SET n2_usuario_id=%s, n2_responsavel=%s, atualizado_em=NOW()
            WHERE id IN (
                SELECT id FROM status_atividades
                WHERE cliente_id=%s AND data=%s AND n2_usuario_id IS NULL
                ORDER BY horario_agendado ASC NULLS LAST, id
                LIMIT %s
            )
            RETURNING id
        """, (body.n2_usuario_id, n2_nome, body.cliente_id, body.data, body.quantidade))
        ids = [r[0] for r in cur.fetchall()]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "atribuidas": len(ids), "ids": ids}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
