"""Ferramentas do admin: quem está online, diagnóstico, uso do sistema e correções de dados.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import datetime
from pathlib import Path
import os

from fastapi import APIRouter, Cookie, HTTPException

from app.core.auth import get_session
from app.core.db import get_db
from app.tarefas.router import AtualizarSegundos

# --- corpo ---
router = APIRouter()


@router.get("/api/admin/online")
def usuarios_online(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT nome, perfil, pagina, last_seen
            FROM sessoes
            WHERE last_seen >= NOW() - INTERVAL '5 minutes'
              AND expira_em > NOW()
            ORDER BY last_seen DESC
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        # last_seen é gravado com NOW() do banco, que está em America/Sao_Paulo
        # (ver SET TIME ZONE em get_db). Usar datetime.now() local evita erro de ~3h.
        agora = datetime.now()
        online = []
        for r in rows:
            last = r[3].replace(tzinfo=None) if r[3].tzinfo else r[3]
            minutos = int((agora - last).total_seconds() / 60)
            online.append({
                "nome": r[0], "perfil": r[1],
                "page": r[2] or "—",
                "ultimo_acesso": f"há {minutos} min" if minutos > 0 else "agora"
            })
        return online
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/admin/diagnostico")
def diagnostico_tecnico(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)

    versao = {
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7] or "desconhecido",
        "commit_completo": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "desconhecido"),
        "branch": os.environ.get("RAILWAY_GIT_BRANCH", "desconhecido"),
        "ambiente": os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local"),
        "mensagem_commit": os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE", "")[:200],
    }

    banco = {"ok": False}
    conn = get_db()
    if conn:
        try:
            t0 = datetime.now()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            contagens = {}
            for nome_tabela, sql in [
                ("usuarios", "SELECT COUNT(*) FROM usuarios WHERE ativo=TRUE"),
                ("status_atividades", "SELECT COUNT(*) FROM status_atividades"),
                ("tarefas", "SELECT COUNT(*) FROM tarefas"),
                ("sessoes_ativas", "SELECT COUNT(*) FROM sessoes WHERE expira_em > NOW()"),
            ]:
                try:
                    cur.execute(sql)
                    contagens[nome_tabela] = cur.fetchone()[0]
                except Exception:
                    contagens[nome_tabela] = None
            cur.close(); conn.close()
            banco = {
                "ok": True,
                "latencia_ms": round((datetime.now() - t0).total_seconds() * 1000, 1),
                "contagens": contagens,
            }
        except Exception as e:
            banco = {"ok": False, "erro": str(e)}
    else:
        banco = {"ok": False, "erro": "Não foi possível conectar ao banco"}

    logs_recentes = []
    try:
        log_path = Path("errors.log")
        if log_path.exists():
            linhas = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            logs_recentes = linhas[-200:]
    except Exception as e:
        logs_recentes = [f"Erro ao ler errors.log: {e}"]

    return {"versao": versao, "banco": banco, "logs_recentes": logs_recentes}

@router.get("/api/admin/atividades")
def atividades_recentes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(u.nome, 'Sistema'), COALESCE(u.perfil, '—'), n.tipo, n.mensagem, n.criado_em
            FROM notificacoes n
            LEFT JOIN usuarios u ON n.usuario_id = u.id
            WHERE n.criado_em >= NOW() - INTERVAL '24 hours'
            ORDER BY n.criado_em DESC
            LIMIT 50
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"nome": r[0], "perfil": r[1], "tipo": r[2], "mensagem": r[3],
                 "quando": r[4].strftime("%H:%M")} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/admin/uso")
def uso_sistema(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.nome,
                   COUNT(t.id) as total,
                   SUM(CASE WHEN t.status='aberto' THEN 1 ELSE 0 END) as abertas,
                   SUM(CASE WHEN t.status='em_andamento' THEN 1 ELSE 0 END) as em_andamento,
                   SUM(CASE WHEN t.status='concluido' THEN 1 ELSE 0 END) as concluidas,
                   COALESCE(SUM(t.segundos), 0) as segundos_total
            FROM usuarios u
            LEFT JOIN tarefas t ON t.usuario_id = u.id
            WHERE u.perfil IN ('funcionario','gestor','demo') AND u.ativo = TRUE
            GROUP BY u.id, u.nome ORDER BY total DESC
        """)
        por_usuario = [{"nome": r[0], "total": int(r[1]), "abertas": int(r[2] or 0),
                        "em_andamento": int(r[3] or 0), "concluidas": int(r[4] or 0),
                        "horas": round(int(r[5])/3600, 1)} for r in cur.fetchall()]

        cur.execute("""
            SELECT TO_CHAR(criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM') as dia, COUNT(*) as total
            FROM notificacoes WHERE criado_em >= NOW() - INTERVAL '7 days'
            GROUP BY dia ORDER BY MIN(criado_em)
        """)
        atividade_diaria = [{"dia": r[0], "total": int(r[1])} for r in cur.fetchall()]

        cur.execute("""
            SELECT tipo, COUNT(*) as total FROM notificacoes
            WHERE criado_em >= NOW() - INTERVAL '30 days'
            GROUP BY tipo ORDER BY total DESC
        """)
        tipo_label = {"nova_tarefa": "Nova Tarefa", "tarefa_concluida": "Concluída",
                      "tarefa_iniciada": "Iniciada", "ia_insight": "IA Insight"}
        tipos = [{"tipo": tipo_label.get(r[0], r[0]), "total": int(r[1])} for r in cur.fetchall()]

        cur.execute("""
            SELECT u.nome, COUNT(n.id) as notas
            FROM usuarios u LEFT JOIN notas n ON n.usuario_id = u.id
            WHERE u.perfil IN ('funcionario','gestor','demo') AND u.ativo = TRUE
            GROUP BY u.id, u.nome ORDER BY notas DESC LIMIT 10
        """)
        notas = [{"nome": r[0], "notas": int(r[1])} for r in cur.fetchall()]

        cur.execute("""
            SELECT u.nome, COUNT(c.id) as carimbos
            FROM usuarios u LEFT JOIN carimbos c ON c.criado_por = u.id
            WHERE u.ativo = TRUE GROUP BY u.id, u.nome ORDER BY carimbos DESC LIMIT 10
        """)
        carimbos_uso = [{"nome": r[0], "carimbos": int(r[1])} for r in cur.fetchall()]

        cur.close(); conn.close()
        return {"por_usuario": por_usuario, "atividade_diaria": atividade_diaria,
                "tipos": tipos, "notas": notas, "carimbos": carimbos_uso}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/admin/ping")
def ping_session(page: str = "", faiston_token: str = Cookie(None)):
    get_session(faiston_token, page)
    return {"ok": True}

@router.get("/api/admin/listar-carimbos-times")
def listar_carimbos_times(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.titulo, c.categoria, c.time_usuario, u.nome as criador
            FROM carimbos c LEFT JOIN usuarios u ON u.id = c.criado_por
            ORDER BY c.time_usuario, c.categoria, c.titulo
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "titulo": r[1], "categoria": r[2],
                 "time_usuario": r[3], "criador": r[4]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/admin/carimbo-time/{cid}")
def corrigir_time_carimbo(cid: int, body: dict, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE carimbos SET time_usuario=%s WHERE id=%s", (body.get("time_usuario"), cid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/admin/horas-corrompidas")
def listar_horas_corrompidas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        # Considera corrompido qualquer tarefa com mais de 30 dias contínuos (2592000s)
        cur.execute("""
            SELECT t.id, t.descricao, t.cliente, u.nome, t.segundos
            FROM tarefas t JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.segundos > 2592000
            ORDER BY t.segundos DESC
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "descricao": r[1], "cliente": r[2], "funcionario": r[3],
                 "segundos": r[4], "horas_display": f"{r[4]//3600}h {(r[4]%3600)//60}m"} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/admin/corrigir-horas/{tid}")
def corrigir_horas_tarefa(tid: int, body: AtualizarSegundos, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        # Timer rodando continua contando a partir do valor corrigido.
        cur.execute("""UPDATE tarefas SET segundos=%s,
                              timer_inicio = CASE WHEN timer_inicio IS NULL THEN NULL ELSE NOW() END
                        WHERE id=%s""", (body.segundos, tid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/admin/limpar-tarefas")
def limpar_todas_tarefas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Apenas admin pode limpar tarefas")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tarefas")
        deleted = cur.rowcount
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "removidas": deleted}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
