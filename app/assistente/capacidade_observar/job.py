"""Capacidade D — job diário. Roda os detectores, aplica o orçamento de
interrupção e o cooldown, chama o modelo só pra redigir o achado
sobrevivente, grava em `sinalizacao`. Nunca roda no caminho da
requisição — só agendado (main.py, junto do resto do APScheduler que já
existe), de madrugada.

Orçamento (seção 8.4 da especificação):
- no máximo 2 sinalizações por pessoa por semana — sobra vira descarte,
  não fila acumulada;
- cooldown de 30 dias por (usuario_id, detector, assinatura);
- feedback = -2 desliga aquele detector pra aquela pessoa, permanentemente.
"""
import json
from collections import defaultdict
from typing import Optional

from app.assistente.capacidade_observar import detectores
from app.assistente.capacidade_observar.redacao import redigir
from app.assistente.db import get_conn

MAX_POR_SEMANA = 2
COOLDOWN_DIAS = 30


def _sinalizacoes_essa_semana(cur, usuario_id: int) -> int:
    cur.execute(
        "SELECT COUNT(*) FROM sinalizacao WHERE usuario_id = %s AND criado_em > now() - interval '7 days'",
        (usuario_id,),
    )
    return cur.fetchone()[0]


def _detector_desligado(cur, usuario_id: int, detector: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sinalizacao WHERE usuario_id = %s AND detector = %s AND feedback = -2 LIMIT 1",
        (usuario_id, detector),
    )
    return cur.fetchone() is not None


def _em_cooldown(cur, usuario_id: int, detector: str, assinatura: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM sinalizacao
        WHERE usuario_id = %s AND detector = %s AND assinatura = %s
          AND criado_em > now() - interval '30 days'
        LIMIT 1
        """,
        (usuario_id, detector, assinatura),
    )
    return cur.fetchone() is not None


def selecionar_candidatos(achados: list) -> dict:
    """Aplica orçamento e cooldown por pessoa, devolve
    {usuario_id: [achados sobreviventes, já ordenados por peso]}. Função
    separada pra ser testável sem precisar rodar o job inteiro (e sem
    precisar chamar o modelo)."""
    conn = get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        por_usuario = defaultdict(list)
        for a in achados:
            por_usuario[a["usuario_id"]].append(a)

        candidatos = {}
        for usuario_id, itens in por_usuario.items():
            vagas = MAX_POR_SEMANA - _sinalizacoes_essa_semana(cur, usuario_id)
            if vagas <= 0:
                continue
            sobreviventes = []
            for a in sorted(itens, key=lambda x: x["peso"], reverse=True):
                if _detector_desligado(cur, usuario_id, a["detector"]):
                    continue
                if _em_cooldown(cur, usuario_id, a["detector"], a["assinatura"]):
                    continue
                sobreviventes.append(a)
                if len(sobreviventes) >= vagas:
                    break
            if sobreviventes:
                candidatos[usuario_id] = sobreviventes
        cur.close()
        conn.close()
        return candidatos
    except Exception as e:
        print(f"[assistente/observar] Erro selecionando candidatos: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return {}


def _gravar(usuario_id: int, detector: str, assinatura: str, evidencia: dict, texto: str) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sinalizacao (usuario_id, detector, assinatura, evidencia, texto)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (usuario_id, detector, assinatura, json.dumps(evidencia, ensure_ascii=False, default=str), texto),
        )
        gravado = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return gravado
    except Exception as e:
        print(f"[assistente/observar] Erro gravando sinalização: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


async def rodar() -> dict:
    """Roda o job completo: detecta → seleciona → redige → grava. Devolve
    um resumo (contagens) pra log/depuração manual."""
    achados = detectores.rodar_todos()
    candidatos = selecionar_candidatos(achados)

    total_candidatos = sum(len(v) for v in candidatos.values())
    total_gravados = 0
    total_descartados_pelo_modelo = 0

    for usuario_id, itens in candidatos.items():
        for a in itens:
            texto = await redigir(a["detector"], a["evidencia"])
            if texto is None:
                total_descartados_pelo_modelo += 1
                continue
            if _gravar(usuario_id, a["detector"], a["assinatura"], a["evidencia"], texto):
                total_gravados += 1

    resumo = {
        "achados": len(achados),
        "candidatos": total_candidatos,
        "descartados_pelo_modelo": total_descartados_pelo_modelo,
        "gravados": total_gravados,
    }
    print(f"[assistente/observar] Job rodou: {resumo}")
    return resumo


def rodar_sync() -> Optional[dict]:
    """Wrapper síncrona pro APScheduler (BackgroundScheduler roda a
    função numa thread comum, não entende coroutine)."""
    import asyncio

    try:
        return asyncio.run(rodar())
    except Exception as e:
        print(f"[assistente/observar] Erro fatal no job: {e}")
        return None
