"""Diagnóstico admin da capacidade D — pra debugar por que um usuário
não recebeu sinalização (orçamento já usado? cooldown? nada detectado?)
sem precisar de acesso direto ao banco.

Exceção deliberada, e deliberadamente limitada, à regra 8 do CLAUDE.md
do assistente ("a sinalização é da pessoa, nunca sobre a pessoa"): só
expõe contagem e metadados (detector, quando, se foi vista) — NUNCA o
texto gerado nem o feedback dado. O texto da sinalização continua
inacessível fora de `sinalizacoes.listar_minhas`, que é sempre a
própria pessoa. Ferramenta de QA, admin-only, fora do fluxo
conversacional.
"""
from typing import Optional

from app.assistente.db import get_conn


def resumo(usuario_id: int) -> dict:
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sinalizacao WHERE usuario_id = %s", (usuario_id,))
        total = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM sinalizacao WHERE usuario_id = %s AND criado_em > now() - interval '7 days'",
            (usuario_id,),
        )
        essa_semana = cur.fetchone()[0]

        cur.execute(
            """
            SELECT detector, criado_em, vista_em IS NOT NULL
            FROM sinalizacao
            WHERE usuario_id = %s
            ORDER BY criado_em DESC
            LIMIT 10
            """,
            (usuario_id,),
        )
        recentes = [
            {"detector": r[0], "criado_em": r[1].isoformat() if r[1] else None, "vista": r[2]}
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT DISTINCT detector FROM sinalizacao WHERE usuario_id = %s AND feedback = -2",
            (usuario_id,),
        )
        detectores_desligados = [r[0] for r in cur.fetchall()]

        cur.close()
        conn.close()
        return {
            "usuario_id": usuario_id,
            "total_historico": total,
            "essa_semana": essa_semana,
            "vagas_restantes_essa_semana": max(2 - essa_semana, 0),
            "recentes": recentes,
            "detectores_desligados_por_feedback": detectores_desligados,
        }
    except Exception as e:
        print(f"[assistente/observar] Erro no diagnóstico: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return {"erro": "falha_consulta"}
