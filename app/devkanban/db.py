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


# ── Leitura/escrita compartilhadas (endpoints do main.py, IA, webhooks, GitHub) ──

STATUS_VALIDOS = ("backlog", "todo", "doing", "done")
PRIORIDADES_VALIDAS = ("baixa", "media", "alta", "urgente")

_SQL_LISTAR = """
    SELECT t.id, t.titulo, t.descricao, t.status, t.ordem, t.prioridade, t.prazo, t.tags, t.link,
           t.criado_por, cp.nome, t.atribuido_a, at.nome,
           t.criado_em, t.atualizado_em,
           (SELECT COUNT(*) FROM dev_tarefa_checklist c WHERE c.tarefa_id = t.id),
           (SELECT COUNT(*) FROM dev_tarefa_checklist c WHERE c.tarefa_id = t.id AND c.concluido),
           (SELECT COUNT(*) FROM dev_tarefa_comentarios cm WHERE cm.tarefa_id = t.id),
           t.pausado, t.concluido_em, t.iniciado_em, t.origem, t.origem_ref, t.evento_id
    FROM dev_tarefas t
    LEFT JOIN usuarios cp ON cp.id = t.criado_por
    LEFT JOIN usuarios at ON at.id = t.atribuido_a
    {where}
    ORDER BY t.status, t.ordem, t.criado_em
"""


def _linha_para_dict(r) -> dict:
    return {
        "id": r[0], "titulo": r[1], "descricao": r[2], "status": r[3], "ordem": r[4],
        "prioridade": r[5], "prazo": r[6].isoformat() if r[6] else None, "tags": r[7] or [], "link": r[8],
        "criado_por": r[9], "criado_por_nome": r[10],
        "atribuido_a": r[11], "atribuido_a_nome": r[12],
        "criado_em": r[13].strftime("%d/%m/%Y %H:%M") if r[13] else "",
        "atualizado_em": r[14].strftime("%d/%m/%Y %H:%M") if r[14] else "",
        "checklist_total": r[15], "checklist_concluidos": r[16], "comentarios_total": r[17],
        "pausado": r[18],
        "concluido_em": r[19].isoformat() if r[19] else None,
        "iniciado_em": r[20].isoformat() if r[20] else None,
        "atualizado_iso": r[14].isoformat() if r[14] else None,
        "origem": r[21], "origem_ref": r[22], "evento_id": r[23],
        "atualizado_dt": r[14], "iniciado_dt": r[20],
    }


def listar_tarefas(cur, tid: int = None) -> list[dict]:
    """Tarefas no formato da API, ainda com atualizado_dt/iniciado_dt (datetime)
    que o foco usa e que `foco.anexar` remove antes de virar JSON."""
    if tid is None:
        cur.execute(_SQL_LISTAR.format(where=""))
    else:
        cur.execute(_SQL_LISTAR.format(where="WHERE t.id = %s"), (tid,))
    return [_linha_para_dict(r) for r in cur.fetchall()]


def criar_tarefa(cur, dados: dict, criado_por, origem: str = "manual", origem_ref: str = None):
    """INSERT único de tarefa (+ checklist opcional). `dados` já sanitizado.
    Devolve o id, ou None se (origem, origem_ref) já existia (dedupe por origem)."""
    status = dados.get("status", "todo")
    if status not in STATUS_VALIDOS:
        status = "todo"
    cur.execute("SELECT COALESCE(MAX(ordem), -1) + 1 FROM dev_tarefas WHERE status = %s", (status,))
    ordem = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO dev_tarefas (titulo, descricao, status, ordem, prioridade, prazo, tags, link, criado_por,
                                 atribuido_a, pausado, concluido_em, iniciado_em, origem, origem_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE,
                CASE WHEN %s = 'done' THEN NOW() END, CASE WHEN %s = 'doing' THEN NOW() END, %s, %s)
        ON CONFLICT (origem, origem_ref) WHERE origem_ref IS NOT NULL DO NOTHING
        RETURNING id
    """, (dados["titulo"], dados.get("descricao", ""), status, ordem, dados.get("prioridade", "media"),
          dados.get("prazo"), dados.get("tags", []), dados.get("link", ""), criado_por,
          dados.get("atribuido_a"), status, status, origem, origem_ref))
    row = cur.fetchone()
    if not row:
        return None
    tid = row[0]
    for i, texto in enumerate(dados.get("checklist", [])):
        cur.execute("INSERT INTO dev_tarefa_checklist (tarefa_id, texto, ordem) VALUES (%s, %s, %s)", (tid, texto, i))
    return tid


def mover_tarefa(cur, tid: int, status: str) -> bool:
    """Muda o status com as mesmas regras do PUT/reordenar (vai pro fim da coluna;
    zera pausado; concluido_em/iniciado_em acompanham). False se já estava lá."""
    cur.execute("""
        UPDATE dev_tarefas SET
            ordem = (SELECT COALESCE(MAX(ordem), -1) + 1 FROM dev_tarefas WHERE status = %s),
            pausado = FALSE,
            concluido_em = CASE WHEN %s = 'done' THEN NOW() END,
            iniciado_em = CASE WHEN %s = 'doing' THEN NOW() END,
            atualizado_em = NOW(), status = %s
        WHERE id = %s AND status <> %s
    """, (status, status, status, status, tid, status))
    return cur.rowcount > 0
