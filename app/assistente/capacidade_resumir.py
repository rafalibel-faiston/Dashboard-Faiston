"""Capacidade C — resumir. Regra 3 do CLAUDE.md do assistente: o modelo
nunca calcula número. Tudo aqui é SQL puro contra as tabelas que já
existem (`tarefas`, `status_atividades`); o router só pega o resultado e
pede ao modelo pra virar texto corrido em cima dele.

Reaproveita a mesma base de dados que `/api/ia/insights` (main.py) já usa,
adicionando o recorte por semana que faltava lá.
"""
import re
from datetime import date, timedelta
from typing import Optional

from app.assistente.db import get_conn


def periodo_semana_atual() -> tuple[date, date]:
    hoje = date.today()
    inicio = hoje - timedelta(days=hoje.weekday())  # segunda-feira desta semana
    return inicio, hoje


def montar_agregado_semana(
    data_inicio: Optional[date] = None, data_fim: Optional[date] = None
) -> Optional[dict]:
    """Devolve o agregado já calculado, ou None se o banco estiver fora
    (o router trata isso como falha do assistente, não inventa número)."""
    if data_inicio is None or data_fim is None:
        data_inicio, data_fim = periodo_semana_atual()

    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT u.nome,
                COUNT(*) FILTER (WHERE t.status = 'concluido' AND t.concluido_em::date BETWEEN %s AND %s) AS concluidas_semana,
                ROUND(COALESCE(SUM(t.segundos) FILTER (WHERE t.status = 'concluido' AND t.concluido_em::date BETWEEN %s AND %s), 0) / 3600.0, 1) AS horas_concluidas_semana,
                COUNT(*) FILTER (WHERE t.status IN ('aberto', 'em_andamento')) AS abertas_hoje
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
            WHERE u.perfil = 'funcionario' AND u.ativo = TRUE
            GROUP BY u.nome
            HAVING COUNT(*) FILTER (WHERE t.status = 'concluido' AND t.concluido_em::date BETWEEN %s AND %s) > 0
                OR COUNT(*) FILTER (WHERE t.status IN ('aberto', 'em_andamento')) > 0
            ORDER BY concluidas_semana DESC, abertas_hoje DESC
            """,
            (data_inicio, data_fim, data_inicio, data_fim, data_inicio, data_fim),
        )
        por_funcionario = [
            {
                "nome": r[0],
                "concluidas_semana": r[1],
                "horas_concluidas_semana": float(r[2]),
                "abertas_hoje": r[3],
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT cliente,
                COUNT(*) FILTER (WHERE status = 'concluido' AND concluido_em::date BETWEEN %s AND %s) AS concluidas_semana,
                COUNT(*) FILTER (WHERE status IN ('aberto', 'em_andamento')) AS abertas_hoje
            FROM tarefas
            WHERE cliente IS NOT NULL AND cliente != ''
            GROUP BY cliente
            HAVING COUNT(*) FILTER (WHERE status = 'concluido' AND concluido_em::date BETWEEN %s AND %s) > 0
                OR COUNT(*) FILTER (WHERE status IN ('aberto', 'em_andamento')) > 0
            ORDER BY concluidas_semana DESC
            LIMIT 10
            """,
            (data_inicio, data_fim, data_inicio, data_fim),
        )
        por_cliente = [
            {"cliente": r[0], "concluidas_semana": r[1], "abertas_hoje": r[2]}
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT COUNT(*) FROM tarefas WHERE status = 'aberto' AND criado_em < NOW() - INTERVAL '7 days'"
        )
        tickets_atrasados = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM status_atividades
            WHERE status = 'concluido' AND data BETWEEN %s AND %s
            """,
            (data_inicio, data_fim),
        )
        atendimentos_campo_semana = cur.fetchone()[0]

        cur.close()
        conn.close()

        return {
            "periodo_inicio": data_inicio.isoformat(),
            "periodo_fim": data_fim.isoformat(),
            "por_funcionario": por_funcionario,
            "por_cliente": por_cliente,
            # limite_dias_atraso vai explícito como valor (não só no nome do
            # campo) pra que "mais de 7 dias" no texto gerado bata com um
            # número de verdade do JSON, e não pareça número inventado.
            "limite_dias_atraso": 7,
            "tickets_abertos_acima_do_limite": tickets_atrasados,
            "atendimentos_campo_concluidos_na_semana": atendimentos_campo_semana,
        }
    except Exception as e:
        print(f"[assistente/capacidade_resumir] Erro ao montar agregado: {e}")
        return None


_NUMERO_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numeros_do_valor(valor) -> set:
    numeros = set()
    if isinstance(valor, dict):
        for v in valor.values():
            numeros |= _numeros_do_valor(v)
    elif isinstance(valor, list):
        for v in valor:
            numeros |= _numeros_do_valor(v)
    elif isinstance(valor, bool):
        pass
    elif isinstance(valor, (int, float)):
        numeros.add(str(valor))
        if isinstance(valor, float) and valor == int(valor):
            numeros.add(str(int(valor)))
    elif isinstance(valor, str):
        numeros |= set(_NUMERO_RE.findall(valor))
    return numeros


def numeros_do_agregado(agregado: dict) -> set:
    """Todo número que pode legitimamente aparecer no texto gerado —
    usado pelo critério de aceite da Fase 2 (todo número do texto vem do
    JSON de entrada)."""
    return _numeros_do_valor(agregado)


def numeros_do_texto(texto: str) -> set:
    return set(_NUMERO_RE.findall(texto))


def todos_numeros_batem(texto: str, agregado: dict) -> bool:
    return numeros_do_texto(texto).issubset(numeros_do_agregado(agregado))
