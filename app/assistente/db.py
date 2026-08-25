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
        # Commita aqui antes de seguir: cada fase abaixo tem seu próprio
        # commit/rollback isolado, pra uma fase falhando (ex.: extensão
        # vector indisponível) não desfazer o que as outras já
        # conseguiram criar -- histórico real: uma falha na Fase 6 já
        # levou junto a criação de `sinalizacao` (Fase 5) por estarem no
        # mesmo commit único no final.
        conn.commit()

        # --- Fase 3: base de procedimentos (capacidade B) ---------------
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
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
        except Exception as e:
            # Neon costuma ter as duas extensões liberadas, mas alguns
            # planos exigem habilitar manualmente no console -- não deixa
            # a Fase 1/2 (que não precisam disso) offline por causa disso.
            print(f"[assistente/db] Fase 3 (base de procedimentos) não pôde ser criada: {e}")
            conn.rollback()

        # --- Fase 5: sinalização (capacidade D) --------------------------
        # Sem tabela nova de atividade -- os detectores leem direto de
        # `tarefas` e `status_atividades`, que já existem (ver
        # capacidade_observar/detectores.py). Só `sinalizacao` é nova de
        # verdade, porque não tem equivalente hoje. Não depende da Fase 3
        # (nenhuma coluna vector), fase isolada de propósito.
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sinalizacao (
                    id           BIGSERIAL PRIMARY KEY,
                    usuario_id   INTEGER NOT NULL REFERENCES usuarios(id),
                    detector     TEXT NOT NULL,
                    assinatura   TEXT NOT NULL,
                    evidencia    JSONB NOT NULL,
                    texto        TEXT NOT NULL,
                    criado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    vista_em     TIMESTAMPTZ,
                    feedback     SMALLINT
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sinalizacao_unica "
                "ON sinalizacao (usuario_id, detector, assinatura, (criado_em::date))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sinalizacao_usuario "
                "ON sinalizacao (usuario_id, vista_em, criado_em DESC)"
            )
            conn.commit()
        except Exception as e:
            print(f"[assistente/db] Fase 5 (sinalizacao) não pôde ser criada: {e}")
            conn.rollback()

        # --- Fase 6: trilha de onboarding (capacidade E, ensinar) --------
        # Reaproveita `documento` (Fase 3) como conteúdo da trilha -- só
        # marca a posição de cada um no onboarding, sem sistema de
        # conteúdo novo. `onboarding_progresso` é só o que não existia:
        # em que etapa cada pessoa está. Depende de `documento` existir
        # (Fase 3) -- se ela não foi criada, esta fase falha sozinha, sem
        # afetar a Fase 5 acima (já commitada).
        try:
            cur.execute("ALTER TABLE documento ADD COLUMN IF NOT EXISTS ordem_onboarding INTEGER")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_documento_ordem_onboarding "
                "ON documento (ordem_onboarding) WHERE ordem_onboarding IS NOT NULL"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS onboarding_progresso (
                    usuario_id    INTEGER PRIMARY KEY REFERENCES usuarios(id),
                    etapa_atual   INTEGER NOT NULL DEFAULT 1,
                    iniciado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
                    concluido_em  TIMESTAMPTZ
                )
                """
            )
            conn.commit()
        except Exception as e:
            print(f"[assistente/db] Fase 6 (onboarding) não pôde ser criada: {e}")
            conn.rollback()

        # --- Fase 7: checkin diário (capacidade F) ------------------------
        # Na 1a vez que a pessoa loga no dia, o assistente resume o que ela
        # fez ontem (SQL, nunca o modelo) + o que ela tinha dito que ia
        # fazer (a resposta de ontem, guardada aqui) e pergunta o plano de
        # hoje. Um registro por (usuario_id, data) -- índice único garante
        # que só existe um checkin por pessoa por dia.
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_diario (
                    id               BIGSERIAL PRIMARY KEY,
                    usuario_id       INTEGER NOT NULL REFERENCES usuarios(id),
                    data             DATE NOT NULL,
                    resumo_enviado   TEXT,
                    resposta_usuario TEXT,
                    criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
                    respondido_em    TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_checkin_diario_unico "
                "ON checkin_diario (usuario_id, data)"
            )
            conn.commit()
        except Exception as e:
            print(f"[assistente/db] Fase 7 (checkin diário) não pôde ser criada: {e}")
            conn.rollback()

        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/db] Erro criando schema: {e}")
