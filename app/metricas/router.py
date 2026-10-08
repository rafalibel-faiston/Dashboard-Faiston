"""Métricas do dashboard.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException

from app.core.acesso import CARGO_VALIDOS
from app.core.agenda import _dias_bloqueados_periodo
from app.core.auth import get_session
from app.core.db import get_db

# --- corpo ---
router = APIRouter()


# --- MÉTRICAS DASHBOARD ---
@router.get("/api/metricas")
def get_metricas(cliente: str = "", data_inicio: str = "", data_fim: str = "", funcionario: str = "", projeto: str = "", time: str = "", frente: str = "", faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        conditions = []
        params = []
        # Filtro de time: gestor/demo vê só seu time; admin/diretor vê todos os
        # times (o diretor comanda as 3 áreas) e pode focar numa via ?time=.
        is_admin = sess["perfil"] in ("admin", "diretor")
        if not is_admin:
            conditions.append("COALESCE(u.time,'Projetos') = %s")
            params.append(sess.get("time", "Projetos"))
        elif time:
            conditions.append("COALESCE(u.time,'Projetos') = %s")
            params.append(time)
        if cliente:
            conditions.append("t.cliente = %s")
            params.append(cliente)
        if funcionario:
            conditions.append("u.nome ILIKE %s")
            params.append(f"%{funcionario}%")
        if data_inicio:
            conditions.append("t.criado_em >= %s")
            params.append(data_inicio + " 00:00:00")
        if data_fim:
            conditions.append("t.criado_em <= %s")
            params.append(data_fim + " 23:59:59")
        if projeto:
            conditions.append("p.nome = %s")
            params.append(projeto)
        # Frente (Analista/Backoffice/N2) dentro do time -- N2 migrou de
        # perfil próprio para cargo dentro de funcionario (2026-07-28) e
        # já entra em "tarefas" via N2-A (visita de campo finalizada gera
        # tarefa automática), então filtrar por cargo='n2' aqui funciona
        # igual às outras frentes.
        if frente in CARGO_VALIDOS:
            conditions.append("u.cargo = %s")
            params.append(frente)
        filtro = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params = tuple(params)

        # JOINs necessários — time/frente filter sempre precisa do JOIN com usuarios
        join_u = "JOIN usuarios u ON t.usuario_id = u.id" if (funcionario or not is_admin or time or frente) else ""
        join_p = "LEFT JOIN projetos p ON t.projeto_id = p.id" if projeto else ""
        # Helpers para adicionar condição de status sem quebrar o filtro existente
        def fwhere(extra): return f"{filtro} AND {extra}" if filtro else f"WHERE {extra}"

        # KPIs gerais
        joins = f"{join_u} {join_p}"
        cur.execute(f"SELECT COUNT(*) FROM tarefas t {joins} {filtro}", params)
        total = cur.fetchone()[0]

        w_aberto = fwhere("t.status = 'aberto'")
        cur.execute(f"SELECT COUNT(*) FROM tarefas t {joins} {w_aberto}", params)
        abertos = cur.fetchone()[0]

        w_concluido = fwhere("t.status = 'concluido'")
        cur.execute(f"SELECT COUNT(*) FROM tarefas t {joins} {w_concluido}", params)
        concluidos = cur.fetchone()[0]

        w_andamento = fwhere("t.status = 'em_andamento'")
        cur.execute(f"SELECT COUNT(*) FROM tarefas t {joins} {w_andamento}", params)
        em_andamento = cur.fetchone()[0]

        cur.execute(f"SELECT COALESCE(SUM(t.segundos),0) FROM tarefas t {joins} {filtro}", params)
        total_segundos = cur.fetchone()[0]

        # Clientes ativos (distintos) — respeita filtro de time
        cur.execute(f"SELECT COUNT(DISTINCT t.cliente) FROM tarefas t {join_u} {join_p} {filtro}", params)
        clientes_ativos = cur.fetchone()[0]

        # Funcionários com tarefas — respeita filtro de time
        cur.execute(f"SELECT COUNT(DISTINCT t.usuario_id) FROM tarefas t {join_u} {join_p} {filtro}", params)
        funcionarios_ativos = cur.fetchone()[0]

        # SLA: % de tarefas concluídas sobre o total
        sla = round((concluidos / total * 100)) if total > 0 else 0

        # Média de horas por funcionário — respeita todos os filtros
        cur.execute(
            f"SELECT COUNT(DISTINCT t.usuario_id), COALESCE(SUM(t.segundos),0) FROM tarefas t {joins} {filtro}",
            params
        )
        row_media = cur.fetchone()
        n_funcs = max(row_media[0], 1)
        media_horas_func = round(row_media[1] / 3600 / n_funcs, 1)

        # Horas por cliente — respeita todos os filtros
        cur.execute(
            f"SELECT t.cliente, COALESCE(SUM(t.segundos),0) as total_seg "
            f"FROM tarefas t {joins} {filtro} GROUP BY t.cliente ORDER BY total_seg DESC",
            params
        )
        horas_por_cliente = [{"cliente": r[0], "horas": round(r[1]/3600, 1)} for r in cur.fetchall()]
        # Peso por cliente — mesmo filtro, quebrado por (cliente, peso). Não
        # vira gráfico à parte; alimenta o drill-down ao clicar na barra do
        # cliente (visão geral primeiro, pedida em 2026-07-28 pra reduzir a
        # poluição visual de uma rosca por funcionário sempre visível).
        cur.execute(
            f"SELECT t.cliente, COALESCE(t.peso,0), COALESCE(SUM(t.segundos),0), COUNT(t.id) "
            f"FROM tarefas t {joins} {filtro} GROUP BY t.cliente, t.peso",
            params
        )
        peso_por_cliente = {}
        for cliente, peso, seg, qtd in cur.fetchall():
            peso_por_cliente.setdefault(cliente, []).append(
                {"peso": peso, "horas": round(seg/3600, 1), "tarefas": qtd})
        for item in horas_por_cliente:
            item["por_peso"] = sorted(peso_por_cliente.get(item["cliente"], []), key=lambda p: p["peso"])

        # Status da fila (para donut)
        cur.execute(f"SELECT t.status, COUNT(*) FROM tarefas t {joins} {filtro} GROUP BY t.status", params)
        status_fila = {r[0]: r[1] for r in cur.fetchall()}

        # Volume por dia da semana (últimos 7 dias — para área)
        cur.execute("""
            SELECT TO_CHAR(criado_em, 'Dy') as dia, COUNT(*) as total
            FROM tarefas
            WHERE criado_em >= NOW() - INTERVAL '7 days'
            GROUP BY TO_CHAR(criado_em, 'Dy'), DATE_TRUNC('day', criado_em)
            ORDER BY DATE_TRUNC('day', criado_em)
        """)
        volume_semana = [{"dia": r[0], "total": r[1]} for r in cur.fetchall()]

        # Funil de atendimento
        funil = {
            "Abertura": total,
            "Triagem": total - max(0, total - abertos),
            "Acionamento": em_andamento + concluidos,
            "Acompanhamento": em_andamento + concluidos,
            "Fechamento": concluidos
        }

        # Tarefas recentes
        q = f"""SELECT t.id, t.descricao, t.cliente, t.prioridade, t.status, t.segundos, t.criado_em, u.nome
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id {join_p}
            {filtro} ORDER BY t.criado_em DESC LIMIT 10"""
        cur.execute(q, params)
        recentes = [{"id": r[0], "descricao": r[1], "cliente": r[2], "prioridade": r[3],
                     "status": r[4], "segundos": r[5], "criado_em": str(r[6]), "funcionario": r[7]}
                    for r in cur.fetchall()]

        # Horas por funcionário — respeita todos os filtros incluindo time.
        # N2 entra igual a funcionário/backoffice/analista (decisão de
        # 2026-07-28: N2 é medido como qualquer outra frente, não à parte).
        func_conds = list(conditions) + ["u.perfil = 'funcionario'"]
        if not is_admin and not any("u.time" in c for c in func_conds):
            func_conds.append("COALESCE(u.time,'Projetos') = %s")
            func_params = params + (sess.get("time", "Projetos"),)
        else:
            func_params = params
        func_filtro = "WHERE " + " AND ".join(func_conds)
        cur.execute(
            f"SELECT u.id, u.nome, COALESCE(SUM(t.segundos),0), COUNT(t.id) as total_tarefas, "
            f"COALESCE(u.time,'Projetos') as area "
            f"FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id {join_p} "
            f"{func_filtro} GROUP BY u.id, u.nome, u.time ORDER BY SUM(t.segundos) DESC",
            func_params
        )
        horas_por_func_rows = cur.fetchall()
        # Peso: mesmo filtro de cima, quebrado por (funcionário, peso) --
        # alimenta o gráfico de rosca (horas dentro de cada faixa de peso).
        cur.execute(
            f"SELECT t.usuario_id, COALESCE(t.peso, 0), COALESCE(SUM(t.segundos),0), COUNT(t.id) "
            f"FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id {join_p} "
            f"{func_filtro} GROUP BY t.usuario_id, t.peso",
            func_params
        )
        peso_por_func = {}
        for uid, peso, seg, qtd in cur.fetchall():
            peso_por_func.setdefault(uid, []).append(
                {"peso": peso, "horas": round(seg/3600, 1), "tarefas": qtd})

        # Aderência a prazo por pessoa -- mesmo escopo/filtro da lista de
        # funcionários. Só entra quem tem prazo_status carimbado ('sem_prazo',
        # de tarefa anterior à etapa 2, fica fora do denominador).
        fw_conc = f"{func_filtro} AND t.status='concluido'" if func_filtro else "WHERE t.status='concluido'"
        cur.execute(
            f"SELECT t.usuario_id, COALESCE(t.prazo_status,''), COUNT(*) "
            f"FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id {join_p} "
            f"{fw_conc} GROUP BY t.usuario_id, t.prazo_status",
            func_params
        )
        prazo_por_func = {}
        for uid, pstatus, qtd in cur.fetchall():
            d = prazo_por_func.setdefault(uid, {"dentro": 0, "fora": 0})
            if pstatus in d:
                d[pstatus] += qtd
        # Dias trabalhados = dias do período filtrado menos os dias de
        # férias/afastamento/recorrência daquele funcionário -- produtividade
        # (horas/tarefas por dia) fica mais justa, e uma obs avisa quando a
        # pessoa ficou fora de parte do período (ponto 2 do feedback).
        dias_periodo = None
        if data_inicio and data_fim:
            try:
                dias_periodo = (datetime.strptime(data_fim, "%Y-%m-%d").date()
                                 - datetime.strptime(data_inicio, "%Y-%m-%d").date()).days + 1
            except ValueError:
                dias_periodo = None
        horas_por_func = []
        for uid, nome, segundos, qtd_tarefas, area in horas_por_func_rows:
            pf = prazo_por_func.get(uid, {"dentro": 0, "fora": 0})
            item = {"id": uid, "nome": nome, "horas": round(segundos/3600, 1), "tarefas": qtd_tarefas, "time": area,
                     "por_peso": sorted(peso_por_func.get(uid, []), key=lambda p: p["peso"]),
                     "prazo_dentro": pf["dentro"], "prazo_fora": pf["fora"]}
            if dias_periodo:
                dias_bloq, notas = _dias_bloqueados_periodo(cur, uid, data_inicio, data_fim)
                dias_trabalhados = max(1, dias_periodo - dias_bloq)
                item["dias_trabalhados"] = dias_trabalhados
                item["horas_por_dia"] = round(segundos/3600 / dias_trabalhados, 2)
                if dias_bloq:
                    item["obs"] = f"{dias_bloq} dia(s) fora no período ({', '.join(sorted(set(notas)))}) — produtividade tende a ser menor"
            horas_por_func.append(item)

        # Taxa de conclusão por cliente — respeita todos os filtros
        cur.execute(
            f"SELECT t.cliente, COUNT(*) as total, "
            f"SUM(CASE WHEN t.status='concluido' THEN 1 ELSE 0 END) as concluidas "
            f"FROM tarefas t {joins} {filtro} GROUP BY t.cliente ORDER BY total DESC",
            params)
        taxa_rows = cur.fetchall()
        taxa_conclusao = [{"cliente": r[0], "total": r[1], "concluidas": r[2],
            "taxa": round(r[2]/r[1]*100) if r[1] > 0 else 0} for r in taxa_rows]

        # Esforço por tipo de atividade -- substitui o funil, cujas etapas eram
        # derivadas por aritmética dos status (uma etapa podia ficar maior que a
        # anterior, o que não existe em funil).
        cur.execute(
            f"SELECT COALESCE(ta.nome,'Sem tipo'), COUNT(*), COALESCE(SUM(t.segundos),0), "
            f"COALESCE(SUM(COALESCE(t.peso,0)),0) "
            f"FROM tarefas t {joins} LEFT JOIN tipos_atividade ta ON ta.id = t.tipo_atividade_id "
            f"{filtro} GROUP BY 1 ORDER BY 4 DESC",
            params
        )
        por_tipo = [{"tipo": r[0], "tarefas": r[1], "horas": round(r[2]/3600, 1), "pontos": r[3]}
                    for r in cur.fetchall()]

        # Etapa 4 da medição: aderência a prazo e natureza da demanda.
        # 'sem_prazo' (tarefa anterior à etapa 2) fica fora do denominador --
        # senão a aderência despenca por falta de dado, não por atraso.
        cur.execute(f"SELECT COALESCE(t.prazo_status,''), COUNT(*) FROM tarefas t {joins} {w_concluido} GROUP BY t.prazo_status", params)
        pz = {r[0]: r[1] for r in cur.fetchall()}
        pz_dentro, pz_fora = pz.get('dentro', 0), pz.get('fora', 0)
        pz_base = pz_dentro + pz_fora
        cur.execute(f"SELECT COALESCE(NULLIF(t.natureza,''),'programada'), COUNT(*) FROM tarefas t {joins} {filtro} GROUP BY 1", params)
        nat = {r[0]: r[1] for r in cur.fetchall()}
        nat_urgente, nat_total = nat.get('urgente', 0), sum(nat.values())
        prazo = {
            "dentro": pz_dentro, "fora": pz_fora, "sem_prazo": pz.get('sem_prazo', 0),
            "aderencia": round(pz_dentro / pz_base * 100) if pz_base else None,
            "urgentes": nat_urgente, "total_natureza": nat_total,
            "pct_nao_programado": round(nat_urgente / nat_total * 100) if nat_total else 0,
        }

        cur.close(); conn.close()
        return {
            "kpis": {
                "total_tarefas": total,
                "tickets_abertos": abertos,
                "em_andamento": em_andamento,
                "concluidos": concluidos,
                "clientes_ativos": clientes_ativos,
                "funcionarios_ativos": funcionarios_ativos,
                "total_horas": round(total_segundos / 3600, 1),
                "sla": sla,
                "media_horas_func": media_horas_func
            },
            "horas_por_cliente": horas_por_cliente,
            "status_fila": status_fila,
            "volume_semana": volume_semana,
            "funil": funil,
            "por_tipo": por_tipo,
            "recentes": recentes,
            "horas_por_func": horas_por_func,
            "taxa_conclusao": taxa_conclusao,
            "prazo": prazo
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/tarefas-por-peso")
def tarefas_por_peso(usuario_id: Optional[int] = None, peso: Optional[int] = None, cliente: str = "",
                      prazo: str = "", data_inicio: str = "", data_fim: str = "", faiston_token: str = Cookie(None)):
    """Drill-down dos gráficos do Dashboard que envolvem peso: lista as
    tarefas no mesmo período filtrado, escopadas por funcionário+peso (rosca
    "Horas por Funcionário") ou só por cliente (barra "Esforço por Cliente" --
    aí sem filtrar peso, cada tarefa mostra o próprio peso na lista, decisão
    de 2026-07-28 de mostrar a visão geral primeiro). Mesma regra de escopo
    do /api/metricas -- admin/diretor veem qualquer um, resto só quem é do
    mesmo time.
    `prazo` filtra pelo prazo_status carimbado na conclusão ('dentro'/'fora')
    -- usado pelo clique no segmento "Fora do prazo" do gráfico de Aderência,
    pra listar as justificativas de atraso daquela pessoa."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not usuario_id and not cliente:
        raise HTTPException(status_code=400, detail="Informe usuario_id ou cliente")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cond, qparams = [], []
        if usuario_id:
            if sess["perfil"] not in ("admin", "diretor"):
                cur.execute("SELECT COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (usuario_id,))
                row = cur.fetchone()
                if not row or row[0] != sess.get("time", "Projetos"):
                    raise HTTPException(status_code=403, detail="Sem acesso a esse funcionário")
            cond.append("t.usuario_id = %s"); qparams.append(usuario_id)
            if peso is not None:
                cond.append("COALESCE(t.peso,0) = %s"); qparams.append(peso)
        else:
            # Cliente sozinho, sem restrição por usuário -- mesma regra de
            # time do /api/metricas: quem não é admin/diretor só vê tarefas
            # de gente do próprio time.
            cond.append("t.cliente = %s"); qparams.append(cliente)
            if sess["perfil"] not in ("admin", "diretor"):
                cond.append("COALESCE(u.time,'Projetos') = %s"); qparams.append(sess.get("time", "Projetos"))
        if prazo in ("dentro", "fora"):
            cond.append("t.prazo_status = %s"); qparams.append(prazo)
        if data_inicio:
            cond.append("t.criado_em >= %s"); qparams.append(data_inicio + " 00:00:00")
        if data_fim:
            cond.append("t.criado_em <= %s"); qparams.append(data_fim + " 23:59:59")
        cur.execute(f"""
            SELECT t.id, t.descricao, t.cliente, t.status, t.segundos, t.criado_em, COALESCE(ta.nome, ''),
                   COALESCE(t.peso, 0), u.nome, t.concluido_em, t.justificativa_atraso
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
            LEFT JOIN tipos_atividade ta ON ta.id = t.tipo_atividade_id
            WHERE {' AND '.join(cond)} ORDER BY t.criado_em DESC
        """, tuple(qparams))
        out = [{"id": r[0], "descricao": r[1], "cliente": r[2], "status": r[3],
                "horas": round(r[4]/3600, 1), "criado_em": str(r[5])[:16], "tipo": r[6],
                "peso": r[7], "funcionario": r[8],
                "concluido_em": str(r[9])[:16] if r[9] else None,
                "justificativa_atraso": r[10] or ""} for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
