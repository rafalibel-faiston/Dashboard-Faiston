"""Resumo diário de alterações nas tarefas e alerta pessoal de pendências (e-mails).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.carga.router import CARGA_NIVEIS, calcular_carga_equipe
from app.core.agenda import _hoje_sp
from app.core.auth import get_session
from app.core.db import get_db
from app.core.email import _brevo_send, _resolver_system_url, _shell_email
from app.notificacoes.router import criar_notificacao

# --- corpo ---
router = APIRouter()


# ── Resumo diário de alterações nas tarefas ───────────────────────────────────
def montar_kpis(cur, hoje, time_filter: str, concluidas_hoje: int) -> str:
    """Painel-resumo no topo do e-mail: situação atual das tarefas em aberto.
    Conta o que está em andamento, o que foi concluído no dia e o que está em
    risco de estourar o prazo (atrasadas ou vencendo hoje), respeitando o time."""
    base = """FROM tarefas t
              LEFT JOIN usuarios u ON u.id = t.usuario_id
              WHERE t.status <> 'concluido'"""
    params = []
    if time_filter:
        base += " AND COALESCE(u.time,'Projetos') = %s"
        params.append(time_filter)
    cur.execute(f"""
        SELECT
          SUM(CASE WHEN t.status='em_andamento' THEN 1 ELSE 0 END),
          SUM(CASE WHEN t.data_prazo IS NOT NULL AND t.data_prazo < %s THEN 1 ELSE 0 END),
          SUM(CASE WHEN t.data_prazo = %s THEN 1 ELSE 0 END)
        {base}""", [hoje, hoje] + params)
    em_andamento, atrasadas, vence_hoje = (cur.fetchone() or (0, 0, 0))
    em_andamento = em_andamento or 0
    atrasadas = atrasadas or 0
    vence_hoje = vence_hoje or 0
    em_risco = atrasadas + vence_hoje

    def card(emoji, num, rotulo, cor, chip):
        return (f'<td width="32%" valign="top" style="padding:0">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:#FBFBFE;border:1px solid #ECEEF4;border-top:3px solid {cor};border-radius:13px">'
                f'<tr><td align="center" style="padding:17px 8px 15px">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 9px">'
                f'<tr><td style="width:38px;height:38px;background:{chip};border-radius:11px;text-align:center;'
                f'vertical-align:middle;font-size:18px;line-height:38px">{emoji}</td></tr></table>'
                f'<div style="font-size:32px;font-weight:800;color:{cor};line-height:1;letter-spacing:-1.2px">{num}</div>'
                f'<div style="font-size:10.5px;color:#8A90A2;text-transform:uppercase;letter-spacing:.7px;'
                f'font-weight:700;margin-top:6px">{rotulo}</div></td></tr></table></td>')

    eyebrow = ('<p style="margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#A78BFA">Panorama de hoje</p>')
    cards = (f'{eyebrow}'
             f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px">'
             f'<tr>'
             f'{card("✅", concluidas_hoje, "Concluídas hoje", "#0E9F6E", "#E3F6EE")}'
             f'<td width="10"></td>'
             f'{card("🚀", em_andamento, "Em andamento", "#5B2EE0", "#EEE8FE")}'
             f'<td width="10"></td>'
             f'{card("⚠️", em_risco, "Em risco do prazo", "#EF4444" if em_risco else "#9AA0AE", "#FDECEC" if em_risco else "#F1F2F6")}'
             f'</tr></table>')

    return cards


def montar_resumo_por_pessoa(cur, inicio, fim, time_filter: str = None) -> str:
    """Quebra por pessoa: quantas tarefas cada um concluiu no dia e quantas
    estão em andamento agora. A conclusão é atribuída ao dono da tarefa.
    Respeita o time (gestor vê só o seu). Retorna '' se não houver ninguém."""
    pessoas = {}  # nome -> {"concluidas": int, "andamento": int}

    # Concluídas no período (status → 'concluido'), atribuídas ao dono da tarefa
    q1 = """SELECT u.nome, COUNT(DISTINCT th.tarefa_id)
            FROM tarefa_historico th
            JOIN tarefas t ON t.id = th.tarefa_id
            JOIN usuarios u ON u.id = t.usuario_id
            WHERE th.acao = 'alterou' AND th.campo = 'status'
              AND th.valor_novo = 'concluido'
              AND th.criado_em >= %s AND th.criado_em < %s"""
    p1 = [inicio, fim]
    if time_filter:
        q1 += " AND COALESCE(u.time,'Projetos') = %s"
        p1.append(time_filter)
    q1 += " GROUP BY u.nome"
    cur.execute(q1, p1)
    for nome, n in cur.fetchall():
        pessoas.setdefault(nome, {"concluidas": 0, "andamento": 0})["concluidas"] = n or 0

    # Em andamento agora (estado atual das tarefas)
    q2 = """SELECT u.nome, COUNT(*)
            FROM tarefas t JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.status = 'em_andamento'"""
    p2 = []
    if time_filter:
        q2 += " AND COALESCE(u.time,'Projetos') = %s"
        p2.append(time_filter)
    q2 += " GROUP BY u.nome"
    cur.execute(q2, p2)
    for nome, n in cur.fetchall():
        pessoas.setdefault(nome, {"concluidas": 0, "andamento": 0})["andamento"] = n or 0

    if not pessoas:
        return ""

    # Quem produziu mais primeiro: concluídas desc, depois em andamento desc
    ordenado = sorted(pessoas.items(),
                      key=lambda kv: (-kv[1]["concluidas"], -kv[1]["andamento"], (kv[0] or "").lower()))
    medalhas = {0: "🥇", 1: "🥈", 2: "🥉"}
    cards = []
    for i, (nome, d) in enumerate(ordenado):
        concl, andam = d["concluidas"], d["andamento"]
        inicial = (nome or "?").strip()[:1].upper()
        medal = medalhas.get(i, "")
        medal_html = (f'&nbsp;<span style="font-size:13px;vertical-align:middle">{medal}</span>'
                      if medal and concl else "")

        # Barra de proporção: concluídas (verde) vs em andamento (roxo)
        tot = concl + andam
        if tot:
            w_concl = int(round(concl / tot * 100))
            seg_concl = (f'<td style="height:6px;background:#0E9F6E;font-size:0;line-height:0;'
                         f'width:{w_concl}%;border-radius:4px 0 0 4px">&nbsp;</td>' if concl else '')
            seg_andam = (f'<td style="height:6px;background:#8B5CF6;font-size:0;line-height:0;'
                         f'width:{100 - w_concl}%;border-radius:{"0 4px 4px 0" if concl else "4px"}">&nbsp;</td>'
                         if andam else '')
            barra = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     f'style="margin-top:12px"><tr>{seg_concl}{seg_andam}</tr></table>')
        else:
            barra = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     'style="margin-top:12px"><tr><td style="height:6px;background:#EEF0F6;'
                     'border-radius:4px;font-size:0;line-height:0">&nbsp;</td></tr></table>')

        def _stat(valor, rotulo, cor):
            return (f'<span style="display:inline-block;text-align:center;margin-left:16px;vertical-align:middle">'
                    f'<span style="display:block;font-size:20px;font-weight:800;color:{cor};line-height:1">{valor}</span>'
                    f'<span style="display:block;font-size:9px;font-weight:700;color:#8A90A2;'
                    f'text-transform:uppercase;letter-spacing:.4px;margin-top:3px">{rotulo}</span></span>')

        cards.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 10px;background:#fff;border:1px solid #ECEEF4;border-radius:14px">'
            f'<tr><td style="padding:14px 18px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="vertical-align:middle">'
            f'<span style="display:inline-block;width:34px;height:34px;'
            f'background:linear-gradient(135deg,#5B2EE0,#B826C9);color:#fff;border-radius:10px;'
            f'text-align:center;line-height:34px;font-size:14px;font-weight:800;margin-right:11px;'
            f'vertical-align:middle">{inicial}</span>'
            f'<span style="font-size:14px;font-weight:700;color:#11131C;vertical-align:middle">{nome or "—"}</span>'
            f'{medal_html}</td>'
            f'<td align="right" style="vertical-align:middle;white-space:nowrap">'
            f'{_stat(concl, "Concluídas", "#0E9F6E")}{_stat(andam, "Em andamento", "#5B2EE0")}'
            f'</td></tr></table>{barra}</td></tr></table>')

    eyebrow = ('<p style="margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#A78BFA">Status por profissional</p>')
    return f'{eyebrow}<div style="margin:0 0 22px">{"".join(cards)}</div>'


def montar_alerta_sobrecarga(cur, time_filter: str = None) -> str:
    """Bloco de sobrecarga do resumo diário: quem está com carga alta ou
    atolado agora. Mesma conta do painel do Kanban (calcular_carga_equipe) --
    a sinalização segue o gestor mesmo quando ele não abre o sistema.
    Retorna '' quando não há ninguém sinalizado."""
    dados = calcular_carga_equipe(cur, time_filter=time_filter)
    sinalizados = [p for p in dados["equipe"] if p["nivel"] in ("pesado", "atolado")]
    if not sinalizados:
        return ""
    livres = [p["nome"].split(" ")[0] for p in dados["equipe"] if p["tarefas"] == 0]

    linhas = []
    for p in sinalizados:
        cor = CARGA_NIVEIS[p["nivel"]]["cor"]
        detalhe = f'{p["tarefas"]} na fila · peso {p["peso_total"]}'
        if p["atrasadas"]:
            detalhe += f' · {p["atrasadas"]} atrasada' + ('s' if p["atrasadas"] > 1 else '')
        if p["indisponivel"]:
            detalhe += f' · {p["indisponivel"]}'
        largura = min(100, int(round(p["pontos"] / max(1, p["limite_atolado"]) * 100)))
        linhas.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 8px;background:#fff;border:1px solid #ECEEF4;border-left:3px solid {cor};border-radius:12px">'
            f'<tr><td style="padding:12px 16px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="vertical-align:middle">'
            f'<span style="font-size:14px;font-weight:700;color:#11131C">{p["nome"]}</span>&nbsp;'
            f'<span style="font-size:10px;font-weight:800;color:{cor};background:{cor}18;'
            f'padding:2px 8px;border-radius:20px;white-space:nowrap">{CARGA_NIVEIS[p["nivel"]]["rotulo"].upper()}</span>'
            f'<br><span style="font-size:11px;color:#8A90A2">{detalhe}</span></td>'
            f'<td align="right" style="vertical-align:middle;white-space:nowrap">'
            f'<span style="font-size:20px;font-weight:800;color:{cor};line-height:1">{p["pontos"]:g}</span>'
            f'<span style="font-size:9px;font-weight:700;color:#8A90A2;letter-spacing:.4px">&nbsp;PTS</span>'
            f'</td></tr></table>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px"><tr>'
            f'<td style="height:6px;background:{cor};font-size:0;line-height:0;width:{largura}%;border-radius:4px">&nbsp;</td>'
            f'<td style="height:6px;background:#EEF0F6;font-size:0;line-height:0;border-radius:4px">&nbsp;</td>'
            f'</tr></table></td></tr></table>')

    rodape = ''
    if livres:
        rodape = (f'<p style="margin:2px 0 0;font-size:11px;color:#8A90A2">'
                  f'Sem fila hoje: {", ".join(livres)} — dá pra redistribuir.</p>')
    eyebrow = ('<p style="margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#EF4444">Sinalização de carga</p>')
    return f'{eyebrow}<div style="margin:0 0 22px">{"".join(linhas)}{rodape}</div>'


def montar_resumo_diario(cur, inicio, fim, time_filter: str = None):
    """Monta (html, total) com o status geral do dia: painel-resumo (KPIs) +
    status por profissional (concluídas hoje e em andamento agora). `total` é o
    nº de alterações do período (usado como gate de envio); `time_filter`
    restringe ao time do dono da tarefa."""
    q = """SELECT COALESCE(NULLIF(projeto_nome,''),'Sem projeto') AS proj,
                  tarefa_id, tarefa_desc, autor_nome, acao, campo,
                  valor_antigo, valor_novo, criado_em
           FROM tarefa_historico
           WHERE criado_em >= %s AND criado_em < %s"""
    params = [inicio, fim]
    if time_filter:
        q += " AND time_tarefa = %s"
        params.append(time_filter)
    q += " ORDER BY proj, tarefa_id, criado_em"
    cur.execute(q, params)
    rows = cur.fetchall()

    total = len(rows)
    # Tarefas concluídas hoje (status → 'concluido'), para o painel-resumo geral.
    concluidas_hoje = len({tid for _proj, tid, _d, _a, acao, campo, _ant, novo, _q in rows
                           if acao == "alterou" and campo == "status" and novo == "concluido"})

    # Painel-resumo geral (KPIs) no topo do corpo
    hoje = inicio.date() if hasattr(inicio, "date") else inicio
    try:
        kpis = montar_kpis(cur, hoje, time_filter, concluidas_hoje)
    except Exception as e:
        print(f"[resumo-diario] KPIs indisponíveis: {e}")
        kpis = ""

    # Status por profissional: quantas cada um concluiu e quantas estão em andamento
    try:
        por_pessoa = montar_resumo_por_pessoa(cur, inicio, fim, time_filter)
    except Exception as e:
        print(f"[resumo-diario] Status por profissional indisponível: {e}")
        por_pessoa = ""

    if not por_pessoa:
        por_pessoa = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#FBFBFE;border:1px solid #ECEEF4;border-radius:14px">'
            '<tr><td align="center" style="padding:30px 20px">'
            '<div style="font-size:26px;margin-bottom:6px">🌙</div>'
            '<p style="color:#6B7280;font-size:14px;font-weight:600;margin:0">Nenhuma atividade por profissional hoje</p>'
            '<p style="color:#AEB3C2;font-size:12px;margin:4px 0 0">Dia tranquilo por aqui.</p></td></tr></table>')

    # Sinalização de sobrecarga (quem está atolado agora) -- entra antes do
    # status por profissional; some sozinha quando não há ninguém sinalizado.
    try:
        sobrecarga = montar_alerta_sobrecarga(cur, time_filter)
    except Exception as e:
        print(f"[resumo-diario] Sinalização de carga indisponível: {e}")
        sobrecarga = ""

    corpo = kpis + sobrecarga + por_pessoa
    return corpo, total


# ── Alerta pessoal de fim de expediente (pendências) ──────────────────────────
def montar_pendencias_funcionario(cur, usuario_id: int, hoje) -> dict:
    """Tarefas pendentes de UM funcionário: abertas, em andamento e atrasadas
    (prazo já vencido, independente do status). Usado no alerta de fim de
    expediente -- e-mail + notificação pessoal -- pra quem esquece de
    atualizar a tarefa antes do dia fechar."""
    cur.execute("""
        SELECT id, descricao, cliente, status, data_prazo
        FROM tarefas
        WHERE usuario_id = %s AND status <> 'concluido'
        ORDER BY CASE WHEN data_prazo IS NOT NULL AND data_prazo < %s THEN 0 ELSE 1 END,
                 data_prazo NULLS LAST, criado_em
    """, (usuario_id, hoje))
    tarefas = cur.fetchall()
    n_abertas = sum(1 for t in tarefas if t[3] == 'aberto')
    n_andamento = sum(1 for t in tarefas if t[3] == 'em_andamento')
    n_atrasadas = sum(1 for t in tarefas if t[4] and t[4] < hoje)
    return {"total": len(tarefas), "tarefas": tarefas,
            "n_abertas": n_abertas, "n_andamento": n_andamento, "n_atrasadas": n_atrasadas}


def _corpo_email_pendencias(dados: dict, hoje, primeiro_nome: str, system_url: str) -> str:
    """Corpo do e-mail pessoal de fim de expediente: saudação, 3 cards (abertas/
    andamento/atrasadas, mesmo estilo de montar_kpis), botão pro quadro do
    funcionário e a lista das tarefas pendentes -- cada uma como um cartão
    (ícone + descrição + selo), no mesmo padrão visual do painel de detalhe
    do calendário em static/index.html."""
    import html as _html

    saudacao = (f'<p style="margin:0 0 20px;font-size:14px;color:#5E647A;line-height:1.6">'
                f'Oi, <strong style="color:#0B0D1F">{_html.escape(primeiro_nome)}</strong> — antes de fechar o dia, '
                f'dá uma olhada no que ainda está pendente no Faiston OPS:</p>')

    def card(emoji, num, rotulo, cor, chip):
        return (f'<td width="32%" valign="top" style="padding:0">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:#FBFBFE;border:1px solid #ECEEF4;border-top:3px solid {cor};border-radius:13px">'
                f'<tr><td align="center" style="padding:16px 8px 14px">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 8px">'
                f'<tr><td style="width:34px;height:34px;background:{chip};border-radius:10px;text-align:center;'
                f'vertical-align:middle;font-size:16px;line-height:34px">{emoji}</td></tr></table>'
                f'<div style="font-size:28px;font-weight:800;color:{cor};line-height:1;letter-spacing:-1px">{num}</div>'
                f'<div style="font-size:10px;color:#8A90A2;text-transform:uppercase;letter-spacing:.6px;'
                f'font-weight:700;margin-top:5px">{rotulo}</div></td></tr></table></td>')

    cor_atraso = "#EF4444" if dados["n_atrasadas"] else "#9AA0AE"
    chip_atraso = "#FDECEC" if dados["n_atrasadas"] else "#F1F2F6"
    cards = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px"><tr>'
             + card("⚪", dados["n_abertas"], "Abertas", "#6B7280", "#F1F2F6")
             + '<td width="8"></td>'
             + card("🚀", dados["n_andamento"], "Em andamento", "#5B2EE0", "#EEE8FE")
             + '<td width="8"></td>'
             + card("⚠️", dados["n_atrasadas"], "Atrasadas", cor_atraso, chip_atraso)
             + '</tr></table>')

    botao = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px">'
             f'<tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">'
             f'<a href="{system_url}/funcionario" style="display:block;color:#ffffff;text-decoration:none;'
             f'padding:14px 24px;font-weight:700;font-size:14px;border-radius:12px">Ver minhas tarefas &nbsp;&rarr;</a>'
             f'</td></tr></table>')

    STATUS_UI = {
        "aberto": {"label": "Aberta", "cor": "#6B7280", "chip": "#F1F2F6"},
        "em_andamento": {"label": "Em andamento", "cor": "#5B2EE0", "chip": "#EEE8FE"},
    }
    linhas = []
    for _tid, desc, cliente, status, prazo in dados["tarefas"][:12]:
        atrasada = bool(prazo and prazo < hoje)
        ui = STATUS_UI.get(status, STATUS_UI["aberto"])
        selo_cor, selo_chip, selo_txt = (("#EF4444", "#FDECEC", "ATRASADA") if atrasada
                                          else (ui["cor"], ui["chip"], ui["label"]))
        icone = "⏰" if atrasada else ("🚀" if status == "em_andamento" else "⚪")
        prazo_txt = prazo.strftime("prazo %d/%m") if prazo else "sem prazo"
        desc_txt = _html.escape((desc or "")[:80])
        cliente_txt = _html.escape(cliente or "") or "—"
        linhas.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 8px;background:#FBFBFE;border:1px solid #ECEEF4;border-radius:12px">'
            f'<tr>'
            f'<td width="46" style="padding:12px 0 12px 12px">'
            f'<span style="display:inline-block;width:30px;height:30px;background:{selo_chip};border-radius:9px;'
            f'text-align:center;vertical-align:middle;font-size:14px;line-height:30px">{icone}</span></td>'
            f'<td style="padding:12px 8px">'
            f'<p style="margin:0;font-size:13px;font-weight:700;color:#0B0D1F;line-height:1.35">{desc_txt}</p>'
            f'<p style="margin:3px 0 0;font-size:11px;color:#9097AC">{cliente_txt} · {prazo_txt}</p></td>'
            f'<td align="right" style="padding:12px 12px 12px 0;white-space:nowrap;vertical-align:top">'
            f'<span style="display:inline-block;font-size:9.5px;font-weight:800;letter-spacing:.3px;'
            f'padding:4px 9px;border-radius:999px;background:{selo_chip};color:{selo_cor}">{selo_txt}</span></td>'
            f'</tr></table>')
    resto = dados["total"] - min(len(dados["tarefas"]), 12)
    if resto > 0:
        linhas.append(f'<p style="margin:2px 0 0;color:#AEB3C2;font-size:11px">e mais {resto} tarefa(s)…</p>')

    lista_label = ('<p style="margin:0 0 10px;font-size:11px;font-weight:800;letter-spacing:1px;'
                    'text-transform:uppercase;color:#A78BFA">Detalhe das pendências</p>')
    return saudacao + cards + botao + lista_label + "".join(linhas)


def enviar_alerta_pendencias(dia=None, system_url: str = "") -> dict:
    """Alerta pessoal de fim de expediente: e-mail + notificação in-app pros
    funcionários com tarefa aberta ou em andamento, pra não ficar coisa parada
    de um dia pro outro por esquecimento. Roda seg-sex, antes do resumo diário
    dos gestores -- mesmo padrão de gate: quem não tem pendência não recebe
    nada (nem e-mail, nem notificação)."""
    conn = get_db()
    if not conn:
        return {"sucesso": False, "erro": "Banco offline"}
    try:
        system_url = (system_url or _resolver_system_url()).rstrip("/")
        cur = conn.cursor()
        hoje = dia or _hoje_sp()
        cur.execute("""SELECT id, nome, COALESCE(email,'') FROM usuarios
                       WHERE ativo=TRUE AND perfil='funcionario'""")
        funcionarios = cur.fetchall()

        enviados, notificados = 0, 0
        for uid, nome, email in funcionarios:
            dados = montar_pendencias_funcionario(cur, uid, hoje)
            if dados["total"] == 0:
                continue
            msg = (f"⏰ Antes de fechar o dia: {dados['n_abertas']} aberta(s), "
                   f"{dados['n_andamento']} em andamento" +
                   (f", {dados['n_atrasadas']} atrasada(s)" if dados["n_atrasadas"] else "") +
                   " — dá uma olhada no Faiston OPS.")
            criar_notificacao(conn, "pendencias_fim_dia", msg, destinatario_id=uid)
            notificados += 1
            if email:
                primeiro_nome = (nome or "").split(" ")[0] or "você"
                corpo = _corpo_email_pendencias(dados, hoje, primeiro_nome, system_url)
                html = _shell_email("Antes de fechar o dia",
                                    f"{dados['total']} tarefa(s) pendente(s) — {primeiro_nome}", corpo)
                assunto = f"⏰ Faiston OPS — {dados['total']} tarefa(s) pendente(s) hoje"
                if _brevo_send(email, assunto, html):
                    enviados += 1
        conn.commit()
        cur.close(); conn.close()
        print(f"[alerta-pendencias] {hoje}: {notificados} notificado(s), {enviados} e-mail(s)")
        return {"sucesso": True, "notificados": notificados, "enviados": enviados, "dia": str(hoje)}
    except Exception as e:
        print(f"[alerta-pendencias] erro: {e}")
        return {"sucesso": False, "erro": str(e)}


def enviar_resumo_diario(dia=None) -> dict:
    """Gera e envia o resumo do dia. Gestores recebem o resumo do seu time;
    admins/diretores recebem o consolidado de todos os times."""
    from datetime import datetime, timedelta
    conn = get_db()
    if not conn:
        return {"sucesso": False, "erro": "Banco offline"}
    try:
        cur = conn.cursor()
        d = dia or _hoje_sp()
        inicio = datetime(d.year, d.month, d.day)
        fim = inicio + timedelta(days=1)
        data_label = inicio.strftime("%d/%m/%Y")

        cur.execute("""SELECT email, COALESCE(time,'Projetos'), perfil, nome FROM usuarios
                       WHERE ativo=TRUE AND perfil IN ('gestor','admin','diretor')
                       AND COALESCE(email,'') <> ''""")
        destinatarios = cur.fetchall()

        enviados, cache = 0, {}
        for email, time_u, perfil, nome in destinatarios:
            chave = "__all__" if perfil in ("admin", "diretor") else time_u
            if chave not in cache:
                cache[chave] = montar_resumo_diario(
                    cur, inicio, fim, None if chave == "__all__" else time_u)
            corpo, total = cache[chave]
            if total == 0:
                continue  # nada a reportar → não envia
            escopo = "todos os times" if chave == "__all__" else f"time {time_u}"
            html = _shell_email("Resumo do dia",
                                f"{data_label} · {total} alteração(ões) · {escopo}", corpo)
            if _brevo_send(email, f"🛰️ Faiston OPS — Resumo de {data_label}", html):
                enviados += 1
        cur.close(); conn.close()
        print(f"[resumo-diario] {data_label}: {enviados} e-mail(s) enviado(s)")
        return {"sucesso": True, "enviados": enviados, "dia": data_label}
    except Exception as e:
        print(f"[resumo-diario] erro: {e}")
        return {"sucesso": False, "erro": str(e)}


@router.get("/api/admin/resumo-diario")
def preview_resumo_diario(enviar: int = 0, dia: str = "", faiston_token: str = Cookie(None)):
    """Pré-visualiza (HTML) ou dispara manualmente o resumo do dia. Admin/gestor.
    ?enviar=1 envia os e-mails · ?dia=YYYY-MM-DD escolhe a data (default hoje)."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor", "demo"):
        raise HTTPException(status_code=403, detail="Sem permissão")
    from datetime import datetime, timedelta
    try:
        d = datetime.strptime(dia, "%Y-%m-%d").date() if dia else _hoje_sp()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida (use YYYY-MM-DD)")
    if enviar == 1:
        if sess["perfil"] not in ("admin", "gestor", "diretor"):
            raise HTTPException(status_code=403, detail="Sem permissão para disparar o envio")
        return enviar_resumo_diario(d)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        inicio = datetime(d.year, d.month, d.day)
        fim = inicio + timedelta(days=1)
        # Gestor vê apenas o próprio time; admin/diretor vê tudo.
        tf = None if sess["perfil"] in ("admin", "diretor") else sess.get("time", "Projetos")
        corpo, total = montar_resumo_diario(cur, inicio, fim, tf)
        cur.close(); conn.close()
        escopo = "todos os times" if tf is None else f"time {tf}"
        html = _shell_email("Resumo do dia",
                            f"{d.strftime('%d/%m/%Y')} · {total} alteração(ões) · {escopo}", corpo)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/admin/alerta-pendencias")
def preview_alerta_pendencias(enviar: int = 0, dia: str = "", usuario_id: int = 0,
                              request: Request = None, faiston_token: str = Cookie(None)):
    """Pré-visualiza ou dispara manualmente o alerta pessoal de fim de expediente.
    ?enviar=1 dispara de verdade pra todo mundo com pendência (só admin, porque
    é um disparo pra empresa toda, não escopado por time) · ?usuario_id=N mostra
    o HTML do e-mail de um funcionário específico · sem parâmetros, lista quem
    receberia e quantas pendências cada um tem hoje."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor", "demo"):
        raise HTTPException(status_code=403, detail="Sem permissão")
    from datetime import datetime
    try:
        d = datetime.strptime(dia, "%Y-%m-%d").date() if dia else _hoje_sp()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida (use YYYY-MM-DD)")
    if enviar == 1:
        if sess["perfil"] != "admin":
            raise HTTPException(status_code=403, detail="Só admin pode disparar o envio pra todo mundo")
        return enviar_alerta_pendencias(d, _resolver_system_url(request))
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if usuario_id:
            cur.execute("SELECT nome, COALESCE(time,'Projetos') FROM usuarios WHERE id=%s AND perfil='funcionario'", (usuario_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Funcionário não encontrado")
            nome, time_u = row
            if sess["perfil"] == "gestor" and time_u != sess.get("time", "Projetos"):
                raise HTTPException(status_code=403, detail="Usuário não pertence ao seu time")
            dados = montar_pendencias_funcionario(cur, usuario_id, d)
            cur.close(); conn.close()
            primeiro_nome = (nome or "").split(" ")[0] or "você"
            corpo = _corpo_email_pendencias(dados, d, primeiro_nome, _resolver_system_url(request))
            html = _shell_email("Antes de fechar o dia",
                                f"{dados['total']} tarefa(s) pendente(s) — {primeiro_nome}", corpo)
            return HTMLResponse(content=html)
        # Sem usuario_id: lista quem receberia e quantas pendências cada um tem.
        tf = None if sess["perfil"] in ("admin", "diretor") else sess.get("time", "Projetos")
        q = "SELECT id, nome FROM usuarios WHERE ativo=TRUE AND perfil='funcionario'"
        params = []
        if tf:
            q += " AND COALESCE(time,'Projetos') = %s"
            params.append(tf)
        cur.execute(q, params)
        resultado = []
        for uid, nome in cur.fetchall():
            dados = montar_pendencias_funcionario(cur, uid, d)
            if dados["total"] > 0:
                resultado.append({"usuario_id": uid, "nome": nome, "total": dados["total"],
                                   "abertas": dados["n_abertas"], "andamento": dados["n_andamento"],
                                   "atrasadas": dados["n_atrasadas"]})
        cur.close(); conn.close()
        return {"dia": str(d), "qtd_receberiam": len(resultado), "detalhe": resultado}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
