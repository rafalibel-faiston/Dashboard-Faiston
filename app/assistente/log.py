"""Gravação em assistente_log.

Regra 6 do CLAUDE.md do assistente: toda pergunta vai para o log, antes de
responder, sempre — é o insumo que decide o que documentar e o que
programar em seguida. As funções aqui são síncronas (psycopg2, como o
resto do OPS); o router.py roda cada chamada numa threadpool pra não
travar o loop assíncrono do streaming.
"""
from typing import Optional

from app.assistente.db import get_conn


def criar_pergunta(usuario_id: int, pergunta: str, contexto_tela: Optional[str]) -> Optional[int]:
    """Grava a pergunta ANTES de qualquer chamada ao modelo. Devolve o
    log_id, ou None se o banco estiver fora (nesse caso o router segue e
    responde sem log — não deixa a tela do assistente cair por isso)."""
    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO assistente_log (usuario_id, pergunta, contexto_tela)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (usuario_id, pergunta, contexto_tela),
        )
        log_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return log_id
    except Exception as e:
        print(f"[assistente/log] Erro ao criar log: {e}")
        return None


def finalizar(
    log_id: Optional[int],
    *,
    resposta: Optional[str],
    respondida: bool,
    motivo_falha: Optional[str] = None,
    tokens_entrada: Optional[int] = None,
    tokens_saida: Optional[int] = None,
    latencia_ms: Optional[int] = None,
    capacidade: Optional[str] = None,
) -> None:
    if log_id is None:
        return
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE assistente_log
            SET resposta = %s, respondida = %s, motivo_falha = %s,
                tokens_entrada = %s, tokens_saida = %s, latencia_ms = %s,
                capacidade = COALESCE(%s, capacidade)
            WHERE id = %s
            """,
            (resposta, respondida, motivo_falha, tokens_entrada, tokens_saida,
             latencia_ms, capacidade, log_id),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/log] Erro ao finalizar log {log_id}: {e}")


def registrar_feedback(log_id: int, usuario_id: int, util: bool) -> bool:
    """Só o dono da pergunta pode avaliar a resposta dela. Devolve False se
    o log não existe ou não é do usuário (o router transforma isso em 404)."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE assistente_log
            SET feedback = %s
            WHERE id = %s AND usuario_id = %s
            """,
            (1 if util else -1, log_id, usuario_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return ok
    except Exception as e:
        print(f"[assistente/log] Erro ao registrar feedback: {e}")
        return False
