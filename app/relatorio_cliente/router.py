"""Relatório por cliente.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from app.core.auth import get_session
from app.core.db import get_db

router = APIRouter()


# --- RELATÓRIO POR CLIENTE ---
@router.get("/api/relatorio/{cliente}")
def get_relatorio(cliente: str, mes: str = "", faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        params_base = [cliente]
        filtro_mes = ""
        if mes:
            filtro_mes = "AND DATE_TRUNC('month', t.criado_em) = DATE_TRUNC('month', %s::date)"
            params_base.append(mes + "-01")

        cur.execute(
            "SELECT t.id, t.descricao, t.prioridade, t.status, t.segundos, t.criado_em, t.atualizado_em, u.nome "
            "FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id "
            "WHERE t.cliente = %s " + filtro_mes + " ORDER BY t.criado_em DESC",
            params_base)
        tarefas = cur.fetchall()

        cur.execute(
            "SELECT u.nome, COUNT(t.id), COALESCE(SUM(t.segundos),0) "
            "FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id "
            "WHERE t.cliente = %s " + filtro_mes + " GROUP BY u.nome ORDER BY SUM(t.segundos) DESC",
            params_base)
        por_func = cur.fetchall()

        cur.execute(
            "SELECT status, COUNT(*) FROM tarefas t WHERE t.cliente = %s " + filtro_mes + " GROUP BY status",
            params_base)
        status_counts = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute(
            "SELECT COALESCE(SUM(segundos),0) FROM tarefas t WHERE t.cliente = %s " + filtro_mes,
            params_base)
        total_seg = cur.fetchone()[0]

        cur.close(); conn.close()
        status_map = {"concluido": "Concluído", "em_andamento": "Em Andamento", "aberto": "Aberto"}
        return {
            "cliente": cliente, "mes": mes,
            "resumo": {
                "total_tarefas": len(tarefas),
                "concluidas": status_counts.get("concluido", 0),
                "em_andamento": status_counts.get("em_andamento", 0),
                "abertas": status_counts.get("aberto", 0),
                "total_horas": round(total_seg / 3600, 1),
                "sla": round(status_counts.get("concluido", 0) / len(tarefas) * 100) if tarefas else 0
            },
            "por_funcionario": [{"nome": r[0], "tarefas": r[1], "horas": round(r[2]/3600, 1)} for r in por_func],
            "tarefas": [{"id": r[0], "descricao": r[1], "prioridade": r[2],
                "status": status_map.get(r[3], r[3]), "horas": round(r[4]/3600, 1),
                "minutos": (r[4] % 3600) // 60, "criado_em": str(r[5])[:10],
                "funcionario": r[7]} for r in tarefas]
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/relatorio/{cliente}")
def relatorio_page(cliente: str, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    return FileResponse("static/relatorio.html")

@router.get("/apresentacao")
def apresentacao_page(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    return FileResponse("static/apresentacao.html")

@router.get("/video")
def video_page(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    return FileResponse("static/video-ops.html")
