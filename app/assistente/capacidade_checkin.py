"""Capacidade F — checkin diário. Na primeira vez que a pessoa loga no
dia, o assistente abre sozinho, resume o que ela fez ontem (dado real,
calculado em SQL — regra 3, o modelo nunca calcula número) junto do que
ela tinha dito que pretendia fazer (memória do checkin de ontem), e
pergunta o plano de hoje. A resposta vira a memória pro checkin de
amanhã, fechando o ciclo planejado × realizado.

Regra 8 do CLAUDE.md do assistente (mesma da capacidade D): a memória é
da pessoa, nunca sobre a pessoa — toda função aqui é escopada por
usuario_id, sem exceção, sem view agregada nem por gestor.
"""
from datetime import date, timedelta
from typing import Optional

from app.assistente.db import get_conn


def pendente_hoje(usuario_id: int) -> bool:
    """True se o checkin de hoje ainda não foi mandado pra essa pessoa."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM checkin_diario WHERE usuario_id = %s AND data = CURRENT_DATE",
            (usuario_id,),
        )
        existe = cur.fetchone() is not None
        cur.close()
        conn.close()
        return not existe
    except Exception as e:
        print(f"[assistente/checkin] Erro checando pendência: {e}")
        return False


def aguardando_resposta(usuario_id: int) -> bool:
    """True se o resumo de hoje já foi mandado mas a pessoa ainda não
    respondeu com o plano dela -- é isso que faz a próxima mensagem dela
    ser tratada como resposta do checkin, não como pergunta normal."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM checkin_diario "
            "WHERE usuario_id = %s AND data = CURRENT_DATE AND resposta_usuario IS NULL",
            (usuario_id,),
        )
        aguardando = cur.fetchone() is not None
        cur.close()
        conn.close()
        return aguardando
    except Exception as e:
        print(f"[assistente/checkin] Erro checando resposta pendente: {e}")
        return False


def contexto_ontem(usuario_id: int) -> dict:
    """Dado real de ontem (SQL) + o plano que a pessoa tinha dito que ia
    seguir (memória do checkin de ontem, se ela respondeu). O modelo só
    redige em cima disso, nunca inventa nem recalcula os números."""
    ontem = date.today() - timedelta(days=1)
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(segundos), 0)
            FROM tarefas
            WHERE usuario_id = %s AND status = 'concluido' AND concluido_em::date = %s
            """,
            (usuario_id, ontem),
        )
        total, segundos = cur.fetchone()
        cur.execute(
            """
            SELECT descricao, cliente FROM tarefas
            WHERE usuario_id = %s AND status = 'concluido' AND concluido_em::date = %s
            ORDER BY concluido_em DESC
            LIMIT 5
            """,
            (usuario_id, ontem),
        )
        exemplos = [{"descricao": r[0], "cliente": r[1]} for r in cur.fetchall()]
        cur.execute(
            "SELECT resposta_usuario FROM checkin_diario WHERE usuario_id = %s AND data = %s",
            (usuario_id, ontem),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return {
            "data": ontem.isoformat(),
            "tarefas_concluidas": total,
            "tempo_total_s": segundos,
            "exemplos": exemplos,
            "plano_que_a_pessoa_tinha_dito_ontem": row[0] if row and row[0] else None,
        }
    except Exception as e:
        print(f"[assistente/checkin] Erro montando contexto de ontem: {e}")
        return {"erro": "falha_consulta"}


def marcar_enviado(usuario_id: int, resumo_enviado: str) -> bool:
    """Cria o registro de hoje -- a partir daqui `pendente_hoje` já
    devolve False, então recarregar a página não manda o checkin de
    novo."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO checkin_diario (usuario_id, data, resumo_enviado) "
            "VALUES (%s, CURRENT_DATE, %s) ON CONFLICT (usuario_id, data) DO NOTHING",
            (usuario_id, resumo_enviado),
        )
        gravado = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return gravado
    except Exception as e:
        print(f"[assistente/checkin] Erro marcando enviado: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def registrar_resposta(usuario_id: int, resposta: str) -> bool:
    """Guarda o plano do dia -- só grava se ainda não tinha resposta de
    hoje, pra não sobrescrever silenciosamente se algo chamar duas
    vezes."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE checkin_diario SET resposta_usuario = %s, respondido_em = now() "
            "WHERE usuario_id = %s AND data = CURRENT_DATE AND resposta_usuario IS NULL",
            (resposta, usuario_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return ok
    except Exception as e:
        print(f"[assistente/checkin] Erro registrando resposta: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False
