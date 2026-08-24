"""Dados sintéticos pra testar a capacidade D (observar) sem precisar de
acesso direto ao banco. Fora do fluxo normal do assistente — chamado só
pelo botão de teste em /assistente/documentos (admin), nunca pelo job
real nem por nenhuma capacidade conversacional.

Exceção deliberada à regra 1 do CLAUDE.md do assistente ("o assistente
só lê"): aqui é ferramenta de QA, não capacidade exposta a pergunta de
usuário. Todo dado gerado carrega `cliente = 'Cliente Teste Observar'`,
o que torna `limpar()` seguro e completo -- nunca toca em nada que não
tenha esse marcador.
"""
from typing import Optional

from app.assistente.db import get_conn

_CLIENTE_TESTE = "Cliente Teste Observar"


def popular(usuario_id: int) -> dict:
    """Gera dados que disparam os três detectores da capacidade D pra
    `usuario_id`: 6 tarefas repetidas (mesmo tipo/cliente, concluídas
    rápido), 1 tarefa parada há 15 dias do mesmo tipo (bem acima do p90
    das 6 acima) e 1 tarefa editada 5 vezes (retrabalho)."""
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tipos_atividade WHERE ativo = true LIMIT 1")
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return {"erro": "sem_tipo_atividade"}
        tipo_id = row[0]

        for i in range(1, 7):
            cur.execute(
                """
                INSERT INTO tarefas
                    (usuario_id, descricao, cliente, status, segundos, tipo_atividade_id,
                     criado_em, concluido_em, atualizado_em)
                VALUES (
                    %s, %s, %s, 'concluido', 1800, %s,
                    NOW() - (%s || ' hours')::interval,
                    NOW() - (%s || ' hours')::interval + interval '1 hour',
                    NOW() - (%s || ' hours')::interval + interval '1 hour'
                )
                """,
                (usuario_id, f"Tarefa de teste (observar) {i}", _CLIENTE_TESTE, tipo_id, i, i, i),
            )

        cur.execute(
            """
            INSERT INTO tarefas (usuario_id, descricao, cliente, status, segundos, tipo_atividade_id, criado_em)
            VALUES (%s, %s, %s, 'aberto', 0, %s, NOW() - interval '15 days')
            """,
            (usuario_id, "Tarefa parada de teste (observar)", _CLIENTE_TESTE, tipo_id),
        )

        cur.execute(
            """
            INSERT INTO tarefas (usuario_id, descricao, cliente, status, segundos, tipo_atividade_id, criado_em)
            VALUES (%s, %s, %s, 'em_andamento', 600, %s, NOW() - interval '2 days')
            RETURNING id
            """,
            (usuario_id, "Tarefa com retrabalho de teste (observar)", _CLIENTE_TESTE, tipo_id),
        )
        tarefa_retrabalho_id = cur.fetchone()[0]

        cur.execute("SELECT nome FROM usuarios WHERE id = %s", (usuario_id,))
        row = cur.fetchone()
        nome = row[0] if row else "Teste"

        for i in range(1, 6):
            cur.execute(
                """
                INSERT INTO tarefa_historico (tarefa_id, tarefa_desc, autor_id, autor_nome, acao, criado_em)
                VALUES (%s, %s, %s, %s, 'editou', NOW() - (%s || ' hours')::interval)
                """,
                (tarefa_retrabalho_id, "Tarefa com retrabalho de teste (observar)", usuario_id, nome, i),
            )

        conn.commit()
        cur.close()
        conn.close()
        return {"sucesso": True, "tipo_atividade_id": tipo_id, "tarefa_retrabalho_id": tarefa_retrabalho_id}
    except Exception as e:
        print(f"[assistente/observar] Erro populando dados de teste: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"erro": "falha_insercao", "detalhe": str(e)}


def limpar() -> dict:
    """Remove só o que `popular()` gerou -- tudo com
    `cliente = 'Cliente Teste Observar'` (e o histórico correspondente).
    Nunca mexe em tarefa real, mesmo que tenha o mesmo `usuario_id`."""
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM tarefa_historico WHERE tarefa_desc LIKE %s",
            ("%teste (observar)%",),
        )
        historico_removido = cur.rowcount
        cur.execute("DELETE FROM tarefas WHERE cliente = %s", (_CLIENTE_TESTE,))
        tarefas_removidas = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {"sucesso": True, "tarefas_removidas": tarefas_removidas, "historico_removido": historico_removido}
    except Exception as e:
        print(f"[assistente/observar] Erro limpando dados de teste: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"erro": "falha_remocao"}
