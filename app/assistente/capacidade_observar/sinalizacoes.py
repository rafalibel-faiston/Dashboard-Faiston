"""Capacidade D — acesso a `sinalizacao` pra API. Regra 8 do CLAUDE.md do
assistente: a sinalização é da pessoa, nunca sobre a pessoa — toda função
aqui é escopada por `usuario_id`, sem exceção. Não existe (e não pode
existir) uma função que devolva sinalização de mais de uma pessoa."""
from typing import List, Optional

from app.assistente.db import get_conn

FEEDBACK_VALIDOS = (1, -1, -2)


def listar_minhas(usuario_id: int, limite: int = 20) -> List[dict]:
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, detector, texto, criado_em, vista_em, feedback
            FROM sinalizacao
            WHERE usuario_id = %s
            ORDER BY criado_em DESC
            LIMIT %s
            """,
            (usuario_id, limite),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {
                "id": r[0],
                "detector": r[1],
                "texto": r[2],
                "criado_em": r[3].isoformat() if r[3] else None,
                "vista_em": r[4].isoformat() if r[4] else None,
                "feedback": r[5],
            }
            for r in linhas
        ]
    except Exception as e:
        print(f"[assistente/observar] Erro listando sinalizações: {e}")
        return []


def contar_nao_vistas(usuario_id: int) -> int:
    conn = get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM sinalizacao WHERE usuario_id = %s AND vista_em IS NULL",
            (usuario_id,),
        )
        total = cur.fetchone()[0]
        cur.close()
        conn.close()
        return total
    except Exception as e:
        print(f"[assistente/observar] Erro contando não vistas: {e}")
        return 0


def marcar_vista(sinalizacao_id: int, usuario_id: int) -> bool:
    """Devolve False se a sinalização não existe OU não é dessa pessoa —
    o router trata os dois casos como 403, pra nunca confirmar pra quem
    pergunta se um ID de outra pessoa existe."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sinalizacao SET vista_em = now() WHERE id = %s AND usuario_id = %s AND vista_em IS NULL",
            (sinalizacao_id, usuario_id),
        )
        # rowcount = 0 tanto se o ID não existe/não é dela quanto se já
        # estava vista -- por isso confirma dono separado, pra devolver
        # sucesso (idempotente) no segundo caso e 403 só no primeiro.
        if cur.rowcount == 0:
            cur.execute("SELECT 1 FROM sinalizacao WHERE id = %s AND usuario_id = %s", (sinalizacao_id, usuario_id))
            eh_dela = cur.fetchone() is not None
        else:
            eh_dela = True
        conn.commit()
        cur.close()
        conn.close()
        return eh_dela
    except Exception as e:
        print(f"[assistente/observar] Erro marcando vista: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def registrar_feedback(sinalizacao_id: int, usuario_id: int, feedback: int) -> bool:
    """feedback: 1 útil | -1 não útil | -2 nunca mais este detector.
    -2 é lido por job.py (_detector_desligado) e desliga aquele detector
    pra essa pessoa permanentemente — sem prazo de validade, sem retentar
    depois."""
    if feedback not in FEEDBACK_VALIDOS:
        raise ValueError(f"feedback inválido: {feedback!r}")
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sinalizacao SET feedback = %s WHERE id = %s AND usuario_id = %s",
            (feedback, sinalizacao_id, usuario_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return ok
    except Exception as e:
        print(f"[assistente/observar] Erro registrando feedback: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False
