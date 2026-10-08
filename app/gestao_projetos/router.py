"""Gestão de projetos (PMO): projetos, comentários, meus projetos e contratos.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.acesso import _can_gestao
from app.core.auth import get_session
from app.core.db import get_db


router = APIRouter()


class GestaoProjetoUpdate(BaseModel):
    responsavel_id: Optional[int] = None
    status_gestao: str = "EM ANDAMENTO"
    data_inicio: Optional[str] = None
    data_termino: Optional[str] = None
    escopo: str = ""
    codigo: str = ""
    responsavel_texto: str = ""


class SeedProjeto(BaseModel):
    codigo: str = ""
    cliente_nome: str
    nome: str
    responsavel_texto: str = ""
    status_gestao: str = "EM ANDAMENTO"
    data_inicio: Optional[str] = None
    data_termino: Optional[str] = None
    escopo: str = ""


class SeedContrato(BaseModel):
    codigo: str = ""
    cliente_nome: str
    nome: str
    sdm: str = ""
    responsavel_texto: str = ""
    status: str = "EM IMPLANTAÇÃO"
    data_inicio: Optional[str] = None
    data_termino: Optional[str] = None


class SeedData(BaseModel):
    projetos: List[SeedProjeto] = []
    contratos: List[SeedContrato] = []


class ContratoGestao(BaseModel):
    cliente_id: int
    nome: str
    sdm: str = ""
    responsavel_id: Optional[int] = None
    status: str = "EM IMPLANTAÇÃO"
    data_inicio: Optional[str] = None
    data_termino: Optional[str] = None


class ComentarioProjeto(BaseModel):
    texto: str


class MeuProjetoStatus(BaseModel):
    status_gestao: str = ""


@router.get("/api/gestao/projetos")
def gestao_listar_projetos(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not _can_gestao(sess): raise HTTPException(status_code=403, detail="Sem permissão")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.nome, p.cliente_id, c.nome AS cliente_nome,
                   p.responsavel_id, u.nome AS responsavel_nome,
                   p.status_gestao, p.data_inicio, p.data_termino, p.escopo, p.ativo,
                   COALESCE(p.codigo,''), COALESCE(p.responsavel_texto,'')
            FROM projetos p
            JOIN clientes c ON c.id = p.cliente_id
            LEFT JOIN usuarios u ON u.id = p.responsavel_id
            WHERE p.ativo = TRUE
            ORDER BY p.cliente_id, p.nome
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1], "cliente_id": r[2], "cliente": r[3],
                 "responsavel_id": r[4], "responsavel": r[5] or r[12] or "",
                 "status_gestao": r[6] or "EM ANDAMENTO",
                 "data_inicio": r[7].isoformat() if r[7] else "",
                 "data_termino": r[8].isoformat() if r[8] else "",
                 "escopo": r[9] or "", "ativo": r[10],
                 "codigo": r[11], "responsavel_texto": r[12]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/gestao/projetos/{pid}")
def gestao_atualizar_projeto(pid: int, body: GestaoProjetoUpdate, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not _can_gestao(sess): raise HTTPException(status_code=403, detail="Sem permissão")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE projetos SET
                responsavel_id = %s, status_gestao = %s,
                data_inicio = %s, data_termino = %s, escopo = %s,
                codigo = %s, responsavel_texto = %s
            WHERE id = %s
        """, (body.responsavel_id, body.status_gestao,
              body.data_inicio or None, body.data_termino or None,
              body.escopo, body.codigo, body.responsavel_texto, pid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Comentários de projeto ───────────────────────────────────
@router.get("/api/gestao/projetos/{pid}/comentarios")
def gestao_listar_comentarios(pid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, usuario_nome, texto, criado_em
            FROM comentarios_projeto WHERE projeto_id = %s ORDER BY criado_em DESC
        """, (pid,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "usuario": r[1], "texto": r[2],
                 "criado_em": r[3].isoformat()} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/gestao/projetos/{pid}/comentarios")
def gestao_adicionar_comentario(pid: int, body: ComentarioProjeto, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not body.texto.strip(): raise HTTPException(status_code=400, detail="Texto vazio")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO comentarios_projeto (projeto_id, usuario_id, usuario_nome, texto)
            VALUES (%s, %s, %s, %s) RETURNING id, criado_em
        """, (pid, sess["id"], sess["nome"], body.texto.strip()))
        r = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        return {"id": r[0], "usuario": sess["nome"], "texto": body.texto.strip(),
                "criado_em": r[1].isoformat()}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Meus projetos (visão do responsável → PMO) ───────────────
# O funcionário responsável por um projeto acompanha aqui o que está sob sua
# responsabilidade: atualiza o status e registra comentários que sobem direto
# para o PMO (mesma tabela comentarios_projeto que a Gestão de Projetos lê).
@router.get("/api/meus-projetos")
def meus_projetos(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.nome, COALESCE(c.nome,''), p.status_gestao,
                   p.data_inicio, p.data_termino, p.escopo, COALESCE(p.codigo,''),
                   (SELECT COUNT(*) FROM comentarios_projeto cp WHERE cp.projeto_id = p.id)
            FROM projetos p
            LEFT JOIN clientes c ON c.id = p.cliente_id
            WHERE p.ativo = TRUE AND p.responsavel_id = %s
            ORDER BY p.nome
        """, (sess["id"],))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1], "cliente": r[2],
                 "status_gestao": r[3] or "EM ANDAMENTO",
                 "data_inicio": r[4].isoformat() if r[4] else "",
                 "data_termino": r[5].isoformat() if r[5] else "",
                 "escopo": r[6] or "", "codigo": r[7],
                 "comentarios": r[8]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/meus-projetos/{pid}/status")
def meus_projetos_status(pid: int, body: MeuProjetoStatus, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT responsavel_id FROM projetos WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="Projeto não encontrado")
        # Só o responsável pelo projeto (ou gestor/admin) pode mexer no status
        if row[0] != sess["id"] and not _can_gestao(sess):
            cur.close(); conn.close()
            raise HTTPException(status_code=403, detail="Sem permissão")
        cur.execute("UPDATE projetos SET status_gestao=%s WHERE id=%s",
                    (body.status_gestao, pid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Contratos ────────────────────────────────────────────────
@router.get("/api/gestao/contratos")
def gestao_listar_contratos(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not _can_gestao(sess): raise HTTPException(status_code=403, detail="Sem permissão")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ct.id, ct.cliente_id, c.nome AS cliente_nome,
                   ct.nome, ct.sdm, ct.responsavel_id, u.nome AS responsavel_nome,
                   ct.status, ct.data_inicio, ct.data_termino
            FROM contratos_gestao ct
            JOIN clientes c ON c.id = ct.cliente_id
            LEFT JOIN usuarios u ON u.id = ct.responsavel_id
            ORDER BY ct.cliente_id, ct.nome
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "cliente_id": r[1], "cliente": r[2],
                 "nome": r[3], "sdm": r[4] or "",
                 "responsavel_id": r[5], "responsavel": r[6] or "",
                 "status": r[7], "data_inicio": r[8].isoformat() if r[8] else "",
                 "data_termino": r[9].isoformat() if r[9] else ""} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/gestao/contratos")
def gestao_criar_contrato(body: ContratoGestao, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not _can_gestao(sess): raise HTTPException(status_code=403, detail="Sem permissão")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contratos_gestao (cliente_id, nome, sdm, responsavel_id, status, data_inicio, data_termino)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (body.cliente_id, body.nome, body.sdm, body.responsavel_id,
              body.status, body.data_inicio or None, body.data_termino or None))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"id": new_id, "sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/gestao/contratos/{cid}")
def gestao_atualizar_contrato(cid: int, body: ContratoGestao, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not _can_gestao(sess): raise HTTPException(status_code=403, detail="Sem permissão")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE contratos_gestao SET cliente_id=%s, nome=%s, sdm=%s,
                responsavel_id=%s, status=%s, data_inicio=%s, data_termino=%s
            WHERE id=%s
        """, (body.cliente_id, body.nome, body.sdm, body.responsavel_id,
              body.status, body.data_inicio or None, body.data_termino or None, cid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/gestao/contratos/{cid}")
def gestao_deletar_contrato(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not _can_gestao(sess): raise HTTPException(status_code=403, detail="Sem permissão")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM contratos_gestao WHERE id=%s", (cid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/gestao/importar")
def gestao_importar(body: SeedData, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Sem permissão — apenas admin")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        criados_proj = 0; ignorados_proj = 0; criados_ct = 0
        for p in body.projetos:
            cur.execute("SELECT id FROM clientes WHERE UPPER(nome) = UPPER(%s) AND ativo=TRUE", (p.cliente_nome,))
            row = cur.fetchone()
            if row:
                cliente_id = row[0]
            else:
                cur.execute("INSERT INTO clientes (nome, ativo) VALUES (%s, TRUE) RETURNING id", (p.cliente_nome,))
                cliente_id = cur.fetchone()[0]
            if p.codigo:
                cur.execute("SELECT id FROM projetos WHERE codigo = %s", (p.codigo,))
                if cur.fetchone():
                    ignorados_proj += 1
                    continue
            cur.execute("""
                INSERT INTO projetos (nome, cliente_id, codigo, responsavel_texto, status_gestao,
                    data_inicio, data_termino, escopo, ativo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """, (p.nome, cliente_id, p.codigo, p.responsavel_texto, p.status_gestao,
                  p.data_inicio or None, p.data_termino or None, p.escopo))
            criados_proj += 1
        for c in body.contratos:
            cur.execute("SELECT id FROM clientes WHERE UPPER(nome) = UPPER(%s) AND ativo=TRUE", (c.cliente_nome,))
            row = cur.fetchone()
            if row:
                cliente_id = row[0]
            else:
                cur.execute("INSERT INTO clientes (nome, ativo) VALUES (%s, TRUE) RETURNING id", (c.cliente_nome,))
                cliente_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO contratos_gestao (cliente_id, nome, sdm, status, data_inicio, data_termino)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (cliente_id, c.nome, c.sdm, c.status, c.data_inicio or None, c.data_termino or None))
            criados_ct += 1
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "projetos_criados": criados_proj, "projetos_ignorados": ignorados_proj, "contratos_criados": criados_ct}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/gestao/usuarios")
def gestao_listar_usuarios(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome FROM usuarios WHERE ativo=TRUE ORDER BY nome")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
