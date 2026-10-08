"""Histórico completo.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import FileResponse

from app.core.auth import get_session
from app.core.db import get_db
from app.core.paginas import _HTML_SEM_CACHE, _redirect_login_ou_home


router = APIRouter()


# --- HISTÓRICO COMPLETO ---
@router.get("/api/historico")
def get_historico(
    page: int = 1,
    por_pagina: int = 20,
    cliente: str = "",
    status: str = "",
    prioridade: str = "",
    funcionario: str = "",
    busca: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    faiston_token: str = Cookie(None)
):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        conditions = ["1=1"]
        params = []
        if cliente:
            conditions.append("t.cliente = %s")
            params.append(cliente)
        if status:
            conditions.append("t.status = %s")
            params.append(status)
        if prioridade:
            conditions.append("t.prioridade = %s")
            params.append(prioridade)
        if funcionario:
            conditions.append("u.nome ILIKE %s")
            params.append(f"%{funcionario}%")
        if busca:
            conditions.append("t.descricao ILIKE %s")
            params.append(f"%{busca}%")
        if data_inicio:
            conditions.append("t.criado_em >= %s")
            params.append(data_inicio + " 00:00:00")
        if data_fim:
            conditions.append("t.criado_em <= %s")
            params.append(data_fim + " 23:59:59")

        where = "WHERE " + " AND ".join(conditions)
        offset = (page - 1) * por_pagina

        # Total de registros
        cur.execute(f"SELECT COUNT(*) FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id {where}", params)
        total = cur.fetchone()[0]

        # Tarefas paginadas
        cur.execute(f"""
            SELECT t.id, t.descricao, t.cliente, t.prioridade, t.status,
                   t.segundos, t.criado_em, t.atualizado_em, u.nome
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
            {where} ORDER BY t.criado_em DESC
            LIMIT %s OFFSET %s
        """, params + [por_pagina, offset])
        rows = cur.fetchall()

        # Resumo do filtro atual
        cur.execute(f"""
            SELECT COALESCE(SUM(t.segundos),0),
                   SUM(CASE WHEN t.status='concluido' THEN 1 ELSE 0 END),
                   COUNT(*)
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id {where}
        """, params)
        resumo = cur.fetchone()

        cur.close(); conn.close()
        status_map = {"concluido": "Concluído", "em_andamento": "Em Andamento", "aberto": "Aberto"}
        return {
            "total": total,
            "pagina": page,
            "por_pagina": por_pagina,
            "total_paginas": max(1, -(-total // por_pagina)),
            "resumo": {
                "total_horas": round((resumo[0] or 0) / 3600, 1),
                "concluidas": resumo[1] or 0,
                "total": resumo[2] or 0
            },
            "tarefas": [{
                "id": r[0], "descricao": r[1], "cliente": r[2],
                "prioridade": r[3], "status": status_map.get(r[4], r[4]),
                "segundos": r[5], "criado_em": str(r[6])[:16],
                "atualizado_em": str(r[7])[:16], "funcionario": r[8]
            } for r in rows]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/historico")
def historico_page(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"):
        return _redirect_login_ou_home(sess)
    return FileResponse("static/historico.html", headers=_HTML_SEM_CACHE)
