"""Data de hoje no fuso de Brasília e bloqueios de agenda (férias, afastamento, recorrência).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import date, datetime, timedelta


def _hoje_sp():
    """Data atual no fuso America/Sao_Paulo (mesmo usado pelo NOW() do banco)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    except Exception:
        return date.today()


def _bloqueio_ativo(cur, usuario_id, data_str, hora_str=None):
    """Retorna a descrição do bloqueio ativo desse funcionário nessa
    data/horário, ou None se estiver livre -- checado antes de criar/editar
    atividades de campo, escala e tarefas (ponto 4 do feedback: férias,
    afastamento médico ou recorrência semanal, ex. tratamento toda terça)."""
    if not usuario_id or not data_str:
        return None
    try:
        data = datetime.strptime(data_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    cur.execute("""
        SELECT tipo, data_inicio, data_fim, dia_semana, hora_inicio, hora_fim, descricao
        FROM funcionario_bloqueios WHERE usuario_id=%s
    """, (usuario_id,))
    for tipo, di, df, dia_semana, hi, hf, descricao in cur.fetchall():
        if tipo in ("ferias", "afastamento"):
            if di and df and di <= data <= df:
                return descricao or ("Férias" if tipo == "ferias" else "Afastamento")
        elif tipo == "recorrente" and dia_semana is not None and dia_semana == data.weekday():
            if hora_str and hi and hf:
                try:
                    hora = datetime.strptime(hora_str[:5], "%H:%M").time()
                    if not (hi <= hora <= hf):
                        continue
                except ValueError:
                    pass
            return descricao or "Recorrência semanal"
    return None


def _dias_bloqueados_periodo(cur, usuario_id, data_inicio_str, data_fim_str):
    """Conta quantos dias desse período o funcionário estava de férias,
    afastado ou numa recorrência semanal, pra virar 'dias trabalhados' nos
    dashboards (ponto 2 do feedback) -- produtividade calculada só com os
    dias em que a pessoa realmente estava disponível pra trabalhar, e uma
    nota (obs) avisando que a produtividade tende a cair nesse período."""
    if not usuario_id or not data_inicio_str or not data_fim_str:
        return 0, []
    try:
        d0 = datetime.strptime(str(data_inicio_str)[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(str(data_fim_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0, []
    if d0 > d1:
        return 0, []
    cur.execute("""
        SELECT tipo, data_inicio, data_fim, dia_semana, descricao
        FROM funcionario_bloqueios WHERE usuario_id=%s
    """, (usuario_id,))
    dias_bloqueados = set()
    notas = []
    for tipo, di, df, dia_semana, descricao in cur.fetchall():
        if tipo in ("ferias", "afastamento") and di and df:
            ini, fim = max(di, d0), min(df, d1)
            if ini <= fim:
                d = ini
                while d <= fim:
                    dias_bloqueados.add(d)
                    d += timedelta(days=1)
                notas.append(descricao or ("Férias" if tipo == "ferias" else "Afastamento"))
        elif tipo == "recorrente" and dia_semana is not None:
            achou = False
            d = d0
            while d <= d1:
                if d.weekday() == dia_semana:
                    dias_bloqueados.add(d)
                    achou = True
                d += timedelta(days=1)
            if achou:
                notas.append(descricao or "Recorrência semanal")
    return len(dias_bloqueados), notas
