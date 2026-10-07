"""Acesso a banco do Service Desk.

Mesmo padrão do app/assistente/db.py: psycopg2 síncrono, uma conexão por
chamada, schema criado de forma idempotente no boot (CREATE ... IF NOT
EXISTS) -- o projeto não usa Alembic.
"""
import json
import os
from typing import Optional

import psycopg2


def get_conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn)
        # Mesmo fuso do get_db() do main.py: NOW() e as colunas TIMESTAMP
        # (sem tz) ficam no horário de Brasília, que é o da escala.
        cur = conn.cursor()
        cur.execute("SET TIME ZONE 'America/Sao_Paulo'")
        cur.close()
        return conn
    except Exception as e:
        print(f"[service_desk/db] Erro ao conectar: {e}")
        return None


def get_session(token: Optional[str]) -> Optional[dict]:
    """Lê a sessão pelo cookie faiston_token (mesma tabela `sessoes` do
    resto do sistema). 'dev' vira 'admin' em `perfil`, igual ao main.py.

    Perfil, área e cargo vêm do cadastro (`usuarios`), não da cópia gravada
    na sessão no login: quem é movido pro Service Desk (ou tirado dele) já
    vale na próxima tela, sem precisar sair e entrar de novo."""
    if not token:
        return None
    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE sessoes s SET last_seen = NOW()
            FROM usuarios u
            WHERE s.token = %s AND s.expira_em > NOW() AND u.id = s.usuario_id AND u.ativo
            RETURNING s.usuario_id, u.nome, u.perfil, u.time, u.cargo
        """, (token,))
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if not row:
            return None
        perfil_real = row[2]
        return {
            "id": row[0], "nome": row[1],
            "perfil": "admin" if perfil_real == "dev" else perfil_real,
            "perfil_real": perfil_real, "time": row[3] or "", "cargo": row[4] or "",
        }
    except Exception:
        try: conn.close()
        except Exception: pass
        return None


AREA_SERVICE_DESK = "Service Desk"
CARGOS_AREA = ["sd_operador", "sd_supervisor"]
PERFIS_AREA = ["funcionario", "gestor", "diretor", "admin", "dev"]

# Filas e canais que já aparecem na operação (prints do sistema atual).
# São só o ponto de partida: fila nova digitada no registro é criada na hora.
FILAS_INICIAIS = ["NOW_BACKOFFICE_SERVICEDESK", "NOW_ATENDIMENTO_SAP"]
CANAIS_INICIAIS = ["Chatbot", "ServiceNow", "Telefone", "E-mail", "Teams"]
CATEGORIAS_INICIAIS = ["Acesso e Senhas", "Suporte a Estação", "SAP", "VPN", "Hardware", "E-mail/Office"]
# (chave, rótulo, cor, finaliza?) -- 'finaliza' conta como tratado no KPI.
STATUS_INICIAIS = [
    ("concluido", "Concluído", "emerald", True),
    ("redirecionado", "Redirecionado", "amber", True),
    ("pendente", "Pendente", "sky", False),
    ("em_atendimento", "Em Atendimento", "blue", False),
    ("aguardando_usuario", "Aguardando Usuário", "violet", False),
]


def setup_schema():
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Dados de operador que não cabem em `usuarios` (matrícula, nível,
        # jornada, meta). 1:1 com usuarios.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sd_operadores (
                usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id) ON DELETE CASCADE,
                matricula VARCHAR(30) DEFAULT '',
                nivel VARCHAR(10) DEFAULT 'N1',
                site VARCHAR(60) DEFAULT '',
                jornada_inicio TIME,
                jornada_fim TIME,
                regime VARCHAR(20) DEFAULT '',
                meta_turno INTEGER DEFAULT 20,
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Log de status: cada troca fecha o evento aberto e abre outro.
        # fim NULL = status atual. Offline = nenhum evento aberto.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sd_status_eventos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                status VARCHAR(20) NOT NULL,
                motivo VARCHAR(60) DEFAULT '',
                inicio TIMESTAMP NOT NULL DEFAULT NOW(),
                fim TIMESTAMP,
                auto_encerrado BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sd_status_aberto ON sd_status_eventos(usuario_id) WHERE fim IS NULL")
        for tabela in ("sd_filas", "sd_canais", "sd_categorias"):
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {tabela} (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(120) UNIQUE NOT NULL,
                    ativo BOOLEAN DEFAULT TRUE,
                    criado_em TIMESTAMP DEFAULT NOW()
                )
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sd_status_tipos (
                chave VARCHAR(30) PRIMARY KEY,
                rotulo VARCHAR(60) NOT NULL,
                cor VARCHAR(20) DEFAULT 'slate',
                finaliza BOOLEAN DEFAULT FALSE,
                ordem INTEGER DEFAULT 100,
                ativo BOOLEAN DEFAULT TRUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sd_atendimentos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                solicitante_nome VARCHAR(150) NOT NULL,
                solicitante_login VARCHAR(80) DEFAULT '',
                canal VARCHAR(60) DEFAULT '',
                fila_entrada VARCHAR(120) NOT NULL,
                fila_destino VARCHAR(120) DEFAULT '',
                categoria VARCHAR(120) DEFAULT '',
                problema VARCHAR(300) NOT NULL,
                tratativa TEXT NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'concluido',
                chamado_externo VARCHAR(60) DEFAULT '',
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sd_atend_usuario_data ON sd_atendimentos(usuario_id, criado_em DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sd_escalas (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                rotulo VARCHAR(80) DEFAULT '',
                hora_inicio TIME NOT NULL,
                hora_fim TIME NOT NULL,
                tipo VARCHAR(20) NOT NULL DEFAULT 'turno',
                obs VARCHAR(200) DEFAULT '',
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sd_escalas_data ON sd_escalas(data)")

        # Área "Service Desk" no cadastro de áreas (tabela `areas`, criada no
        # setup_banco do main.py): aceita só os cargos do SD e não trabalha
        # por projeto. ON CONFLICT DO NOTHING -- ajuste do admin prevalece.
        cur.execute("SELECT to_regclass('public.areas')")
        if cur.fetchone()[0]:
            cur.execute("""
                INSERT INTO areas (nome, usa_projetos, cargos, perfis)
                VALUES (%s, FALSE, %s::jsonb, %s::jsonb) ON CONFLICT (nome) DO NOTHING
            """, (AREA_SERVICE_DESK, json.dumps(CARGOS_AREA), json.dumps(PERFIS_AREA)))
        for tabela, nomes in (("sd_filas", FILAS_INICIAIS), ("sd_canais", CANAIS_INICIAIS),
                              ("sd_categorias", CATEGORIAS_INICIAIS)):
            for nome in nomes:
                cur.execute(f"INSERT INTO {tabela} (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING", (nome,))
        for ordem, (chave, rotulo, cor, finaliza) in enumerate(STATUS_INICIAIS):
            cur.execute("""
                INSERT INTO sd_status_tipos (chave, rotulo, cor, finaliza, ordem)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (chave) DO NOTHING
            """, (chave, rotulo, cor, finaliza, ordem))
        conn.commit(); cur.close()
    except Exception as e:
        print(f"[service_desk/db] Erro no setup do schema: {e}")
    finally:
        conn.close()
