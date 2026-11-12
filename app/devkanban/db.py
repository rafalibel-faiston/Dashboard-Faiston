"""Banco do Kanban da Área de Dev (extensões da tabela dev_tarefas e tabelas
novas das conexões). Mesma convenção do assistente: uma conexão nova por
chamada, sessão lida da tabela `sessoes` (a ação roda com a permissão de quem
pediu). A tabela base dev_tarefas continua sendo criada em main.py:setup_banco."""
from app.assistente.db import get_conn, get_session  # noqa: F401  (reexport)


def eh_dev(sess) -> bool:
    return bool(sess) and sess.get("perfil_real") == "dev"


def setup_schema() -> None:
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Quando entrou em 'doing' (pra "em progresso há N dias" e detectar tarefa parada).
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS iniciado_em TIMESTAMP")
        cur.execute("UPDATE dev_tarefas SET iniciado_em = atualizado_em WHERE status = 'doing' AND iniciado_em IS NULL")
        # De onde a tarefa veio ('manual', 'ia', 'github', 'tiflux', 'hub', 'n8n', 'email'...) e a chave
        # dela no sistema de origem -- permite deduplicar o que chega por webhook/importação.
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS origem VARCHAR(30) NOT NULL DEFAULT 'manual'")
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS origem_ref TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_dev_tarefas_origem ON dev_tarefas(origem, origem_ref) WHERE origem_ref IS NOT NULL")
        # Evento do calendário (Outlook) ligado ao prazo da tarefa.
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS evento_id TEXT")
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS evento_mailbox TEXT")
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[devkanban/db] Erro na migração: {e}")
        conn.rollback()
    finally:
        conn.close()
