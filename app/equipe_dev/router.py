"""Equipe Dev: kanban interno, comentários, checklist e diário (restrito a perfil 'dev').

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.auth import _is_dev, get_session
from app.core.db import get_db

router = APIRouter()


class DevTarefaModel(BaseModel):
    titulo: str
    descricao: str = ""
    status: str = "todo"
    prioridade: str = "media"
    prazo: Optional[str] = None
    tags: List[str] = []
    link: str = ""
    atribuido_a: Optional[int] = None
    pausado: bool = False


class DevComentarioModel(BaseModel):
    texto: str


class DevChecklistItemModel(BaseModel):
    texto: str
    concluido: bool = False


# --- EQUIPE DEV (kanban interno, restrito a perfil 'dev') ---
DEV_STATUS_VALIDOS = ("backlog", "todo", "doing", "done")
DEV_PRIORIDADE_VALIDAS = ("baixa", "media", "alta", "urgente")

def _dev_prazo_ou_none(prazo):
    if not prazo: return None
    try:
        date.fromisoformat(prazo)
    except ValueError:
        raise HTTPException(status_code=400, detail="Prazo inválido — use o formato AAAA-MM-DD")
    return prazo

@router.get("/api/dev-tarefas/usuarios")
def dev_listar_usuarios(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome FROM usuarios WHERE perfil='dev' AND ativo=TRUE ORDER BY nome")
        out = [{"id": r[0], "nome": r[1]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/dev-tarefas")
def dev_listar_tarefas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.titulo, t.descricao, t.status, t.ordem, t.prioridade, t.prazo, t.tags, t.link,
                   t.criado_por, cp.nome, t.atribuido_a, at.nome,
                   t.criado_em, t.atualizado_em,
                   (SELECT COUNT(*) FROM dev_tarefa_checklist c WHERE c.tarefa_id = t.id),
                   (SELECT COUNT(*) FROM dev_tarefa_checklist c WHERE c.tarefa_id = t.id AND c.concluido),
                   (SELECT COUNT(*) FROM dev_tarefa_comentarios cm WHERE cm.tarefa_id = t.id),
                   t.pausado
            FROM dev_tarefas t
            LEFT JOIN usuarios cp ON cp.id = t.criado_por
            LEFT JOIN usuarios at ON at.id = t.atribuido_a
            ORDER BY t.status, t.ordem, t.criado_em
        """)
        out = [{
            "id": r[0], "titulo": r[1], "descricao": r[2], "status": r[3], "ordem": r[4],
            "prioridade": r[5], "prazo": r[6].isoformat() if r[6] else None, "tags": r[7] or [], "link": r[8],
            "criado_por": r[9], "criado_por_nome": r[10],
            "atribuido_a": r[11], "atribuido_a_nome": r[12],
            "criado_em": r[13].strftime("%d/%m/%Y %H:%M") if r[13] else "",
            "atualizado_em": r[14].strftime("%d/%m/%Y %H:%M") if r[14] else "",
            "checklist_total": r[15], "checklist_concluidos": r[16], "comentarios_total": r[17],
            "pausado": r[18],
        } for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/dev-tarefas")
def dev_criar_tarefa(t: DevTarefaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if not t.titulo.strip(): raise HTTPException(status_code=400, detail="Título obrigatório")
    status = t.status if t.status in DEV_STATUS_VALIDOS else "todo"
    prioridade = t.prioridade if t.prioridade in DEV_PRIORIDADE_VALIDAS else "media"
    tags = [tg.strip() for tg in t.tags if tg.strip()]
    prazo = _dev_prazo_ou_none(t.prazo)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(ordem), -1) + 1 FROM dev_tarefas WHERE status = %s", (status,))
        ordem = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO dev_tarefas (titulo, descricao, status, ordem, prioridade, prazo, tags, link, criado_por, atribuido_a, pausado)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (t.titulo.strip(), t.descricao, status, ordem, prioridade, prazo, tags, t.link, sess["id"], t.atribuido_a, t.pausado))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/dev-tarefas/{tid}")
def dev_atualizar_tarefa(tid: int, t: DevTarefaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if not t.titulo.strip(): raise HTTPException(status_code=400, detail="Título obrigatório")
    status = t.status if t.status in DEV_STATUS_VALIDOS else "todo"
    prioridade = t.prioridade if t.prioridade in DEV_PRIORIDADE_VALIDAS else "media"
    tags = [tg.strip() for tg in t.tags if tg.strip()]
    prazo = _dev_prazo_ou_none(t.prazo)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM dev_tarefas WHERE id = %s", (tid,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        if row[0] != status:
            cur.execute("SELECT COALESCE(MAX(ordem), -1) + 1 FROM dev_tarefas WHERE status = %s", (status,))
            ordem = cur.fetchone()[0]
            cur.execute("""
                UPDATE dev_tarefas SET titulo=%s, descricao=%s, status=%s, ordem=%s, prioridade=%s,
                       prazo=%s, tags=%s, link=%s, atribuido_a=%s, pausado=%s, atualizado_em=NOW() WHERE id=%s
            """, (t.titulo.strip(), t.descricao, status, ordem, prioridade, prazo, tags, t.link, t.atribuido_a, t.pausado, tid))
        else:
            cur.execute("""
                UPDATE dev_tarefas SET titulo=%s, descricao=%s, prioridade=%s, prazo=%s, tags=%s,
                       link=%s, atribuido_a=%s, pausado=%s, atualizado_em=NOW() WHERE id=%s
            """, (t.titulo.strip(), t.descricao, prioridade, prazo, tags, t.link, t.atribuido_a, t.pausado, tid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/dev-tarefas/{tid}")
def dev_deletar_tarefa(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dev_tarefas WHERE id = %s", (tid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- EQUIPE DEV: comentários por tarefa ---
@router.get("/api/dev-tarefas/{tid}/comentarios")
def dev_listar_comentarios(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, usuario_nome, texto, criado_em FROM dev_tarefa_comentarios
            WHERE tarefa_id = %s ORDER BY criado_em
        """, (tid,))
        out = [{"id": r[0], "usuario_nome": r[1], "texto": r[2],
                "criado_em": r[3].strftime("%d/%m/%Y %H:%M") if r[3] else ""} for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/dev-tarefas/{tid}/comentarios")
def dev_criar_comentario(tid: int, c: DevComentarioModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if not c.texto.strip(): raise HTTPException(status_code=400, detail="Comentário vazio")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM dev_tarefas WHERE id = %s", (tid,))
        if not cur.fetchone(): raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        cur.execute("""
            INSERT INTO dev_tarefa_comentarios (tarefa_id, usuario_id, usuario_nome, texto)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (tid, sess["id"], sess["nome"], c.texto.strip()))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/dev-tarefas/comentarios/{cid}")
def dev_deletar_comentario(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dev_tarefa_comentarios WHERE id = %s", (cid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- EQUIPE DEV: checklist por tarefa ---
@router.get("/api/dev-tarefas/{tid}/checklist")
def dev_listar_checklist(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, texto, concluido FROM dev_tarefa_checklist
            WHERE tarefa_id = %s ORDER BY ordem, id
        """, (tid,))
        out = [{"id": r[0], "texto": r[1], "concluido": r[2]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/dev-tarefas/{tid}/checklist")
def dev_criar_item_checklist(tid: int, item: DevChecklistItemModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if not item.texto.strip(): raise HTTPException(status_code=400, detail="Item vazio")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM dev_tarefas WHERE id = %s", (tid,))
        if not cur.fetchone(): raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        cur.execute("SELECT COALESCE(MAX(ordem), -1) + 1 FROM dev_tarefa_checklist WHERE tarefa_id = %s", (tid,))
        ordem = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO dev_tarefa_checklist (tarefa_id, texto, ordem) VALUES (%s, %s, %s) RETURNING id
        """, (tid, item.texto.strip(), ordem))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/dev-tarefas/checklist/{iid}")
def dev_atualizar_item_checklist(iid: int, item: DevChecklistItemModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if not item.texto.strip(): raise HTTPException(status_code=400, detail="Item vazio")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE dev_tarefa_checklist SET texto=%s, concluido=%s WHERE id=%s",
                    (item.texto.strip(), item.concluido, iid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/dev-tarefas/checklist/{iid}")
def dev_deletar_item_checklist(iid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dev_tarefa_checklist WHERE id = %s", (iid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- DIÁRIO DA EQUIPE DEV (registro do que foi mudado, pra não repetir trabalho) ---
class DevDiarioModel(BaseModel):
    titulo: str
    descricao: str = ""

@router.get("/api/dev-diario")
def dev_listar_diario(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, descricao, autor_id, autor_nome, criado_em
            FROM dev_diario ORDER BY criado_em DESC
        """)
        out = [{
            "id": r[0], "titulo": r[1], "descricao": r[2], "autor_id": r[3], "autor_nome": r[4],
            "criado_em": r[5].strftime("%d/%m/%Y %H:%M") if r[5] else "",
        } for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/dev-diario")
def dev_criar_diario(d: DevDiarioModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if not d.titulo.strip(): raise HTTPException(status_code=400, detail="Título obrigatório")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO dev_diario (titulo, descricao, autor_id, autor_nome)
            VALUES (%s,%s,%s,%s) RETURNING id
        """, (d.titulo.strip(), d.descricao.strip(), sess["id"], sess["nome"]))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/dev-diario/{did}")
def dev_deletar_diario(did: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dev_diario WHERE id = %s", (did,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
