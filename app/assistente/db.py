"""Acesso a banco do assistente OPS.

Reaproveita o mesmo Postgres e o mesmo padrão de conexão do resto do
Faiston OPS (psycopg2 síncrono, uma conexão por chamada — ver main.py
get_db()/get_session()). Isolado num módulo próprio para que o assistente
só toque nas tabelas que são dele (assistente_log, e futuramente
documento/documento_chunk) e nunca em tabela de negócio.
"""
import os
from typing import Optional

import psycopg2


def get_conn():
    """Uma conexão nova por chamada, igual ao get_db() do main.py."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    try:
        return psycopg2.connect(dsn)
    except Exception as e:
        print(f"[assistente/db] Erro ao conectar: {e}")
        return None


def get_session(token: Optional[str]) -> Optional[dict]:
    """Lê a sessão pelo cookie faiston_token. Mesma tabela `sessoes` que o
    resto do sistema usa — a pergunta roda com a permissão de quem
    perguntou, nunca com usuário técnico privilegiado (regra 5 do
    CLAUDE.md do assistente). Não atualiza last_seen/pagina: quem faz
    isso é a navegação normal do OPS, não o widget."""
    if not token:
        return None
    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT usuario_id, nome, perfil, time_usuario, cargo
            FROM sessoes WHERE token = %s AND expira_em > NOW()
            """,
            (token,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        perfil_real = row[2]
        # Mesmo tratamento do main.py: perfil 'dev' tem acesso equivalente a admin.
        perfil = "admin" if perfil_real == "dev" else perfil_real
        return {
            "id": row[0],
            "nome": row[1],
            "perfil": perfil,
            "perfil_real": perfil_real,
            "time": row[3],
            "cargo": row[4] or "",
        }
    except Exception as e:
        print(f"[assistente/db] Erro get_session: {e}")
        return None


def setup_schema() -> None:
    """Cria a tabela do log do assistente se ainda não existir. Chamada uma
    vez na subida do app (main.py), logo depois do setup_banco() do resto
    do sistema. Fase 1: só assistente_log — documento/documento_chunk
    entram na Fase 3 (capacidade B), quando existir de fato o que indexar."""
    conn = get_conn()
    if not conn:
        print("[assistente/db] Banco offline, schema do assistente não criado.")
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS assistente_log (
                id              BIGSERIAL PRIMARY KEY,
                criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
                usuario_id      INTEGER     NOT NULL REFERENCES usuarios(id),
                pergunta        TEXT        NOT NULL,
                contexto_tela   TEXT,
                capacidade      TEXT,
                resposta        TEXT,
                fontes          JSONB       NOT NULL DEFAULT '[]'::jsonb,
                respondida      BOOLEAN     NOT NULL DEFAULT false,
                motivo_falha    TEXT,
                tokens_entrada  INTEGER,
                tokens_saida    INTEGER,
                latencia_ms     INTEGER,
                feedback        SMALLINT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistente_log_criado ON assistente_log (criado_em DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistente_log_pendente "
            "ON assistente_log (respondida, criado_em DESC)"
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/db] Erro criando schema: {e}")
