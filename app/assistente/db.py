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


def get_conn_vector():
    """Conexão com o tipo `vector` do pgvector registrado — só usada por
    ingestao.py e capacidade_explicar.py, depois que a extensão já existe
    (setup_schema já rodou). Sem isso o psycopg2 não sabe adaptar
    list[float] pro tipo VECTOR(N) da coluna embedding."""
    conn = get_conn()
    if not conn:
        return None
    try:
        from pgvector.psycopg2 import register_vector

        register_vector(conn)
        return conn
    except Exception as e:
        print(f"[assistente/db] Erro registrando tipo vector: {e}")
        try:
            conn.close()
        except Exception:
            pass
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
    """Cria o schema do assistente se ainda não existir. Chamada uma vez na
    subida do app (main.py), logo depois do setup_banco() do resto do
    sistema. Idempotente (IF NOT EXISTS em tudo), mesmo padrão que o resto
    do app já usa — sem runner de migração separado."""
    conn = get_conn()
    if not conn:
        print("[assistente/db] Banco offline, schema do assistente não criado.")
        return
    try:
        from app.assistente.embeddings import EMBEDDING_DIM

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
        # Commita aqui antes de tentar as extensões: se CREATE EXTENSION
        # falhar (ex.: plano sem permissão), o rollback do except abaixo
        # não pode levar junto a criação de assistente_log que já rodou.
        conn.commit()

        # --- Fase 3: base de procedimentos (capacidade B) ---------------
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except Exception as e:
            # Neon costuma ter as duas liberadas, mas alguns planos exigem
            # habilitar manualmente no console -- não deixa a Fase 1/2
            # (que não precisam disso) offline por causa disso.
            print(f"[assistente/db] Não consegui criar extensão vector/pg_trgm: {e}")
            conn.rollback()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS documento (
                id            BIGSERIAL PRIMARY KEY,
                titulo        TEXT NOT NULL UNIQUE,
                origem        TEXT,
                versao        TEXT,
                atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                ativo         BOOLEAN NOT NULL DEFAULT true
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS documento_chunk (
                id           BIGSERIAL PRIMARY KEY,
                documento_id BIGINT NOT NULL REFERENCES documento(id) ON DELETE CASCADE,
                ordem        INTEGER NOT NULL,
                texto        TEXT NOT NULL,
                embedding    VECTOR({EMBEDDING_DIM}) NOT NULL,
                tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('portuguese', texto)) STORED
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documento_chunk_hnsw "
            "ON documento_chunk USING hnsw (embedding vector_cosine_ops)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documento_chunk_tsv ON documento_chunk USING gin (tsv)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documento_chunk_ordem ON documento_chunk (documento_id, ordem)"
        )

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/db] Erro criando schema: {e}")
