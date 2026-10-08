from fastapi import FastAPI, HTTPException, Response, Cookie, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
import psycopg2
import smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, timedelta, datetime
from calendar import monthrange
import os, hashlib, secrets, csv, io, logging, traceback, uuid, bcrypt, re
import contextvars, threading
import time as _time
from dotenv import load_dotenv
from pathlib import Path

try:
    import openpyxl
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("errors.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("faiston")

app = FastAPI(title="Faiston Ops - API", version="1.0")

# ── CSRF (double-submit cookie) ───────────────────────────────────────────
# Cookie de sessão é SameSite=Lax + HttpOnly, o que já barra cookie em POST
# de outra origem (form) e em fetch/XHR cross-site (bloqueado também por não
# haver CORS configurado aqui). O gap que sobra é ação de estado exposta via
# GET (SameSite=Lax ainda manda o cookie em navegação de topo por link) --
# corrigido à parte (seed-dados virou POST). Esse middleware é a camada
# redundante: toda rota /api/* que muda estado exige um header X-CSRF-Token
# batendo com o cookie csrf_token -- um site de fora não consegue ler esse
# cookie (same-origin policy) pra montar o header certo, mesmo que de alguma
# forma conseguisse disparar a requisição.
from starlette.middleware.base import BaseHTTPMiddleware

_CSRF_METODOS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_ISENTAS = {"/api/login", "/api/esqueci-senha", "/api/redefinir-senha"}  # fluxos de pré-login, sem cookie de csrf ainda

class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (request.method in _CSRF_METODOS
                and (request.url.path.startswith("/api/") or request.url.path.startswith("/assistente/"))
                and request.url.path not in _CSRF_ISENTAS):
            cookie_token = request.cookies.get("csrf_token")
            header_token = request.headers.get("x-csrf-token")
            if not cookie_token or not header_token or cookie_token != header_token:
                return JSONResponse({"detail": "Token CSRF ausente ou inválido"}, status_code=403)
        return await call_next(request)

app.add_middleware(CSRFMiddleware)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        error_id = uuid.uuid4().hex[:8].upper()
        # Tenta recuperar o traceback original (preservado em __context__ pelo `raise HTTPException` dentro de `except`)
        original = getattr(exc, "__context__", None)
        tb = (
            "".join(traceback.format_exception(type(original), original, original.__traceback__))
            if original
            else "sem traceback"
        )
        logger.error(
            f"[{error_id}] {request.method} {request.url.path}\n"
            f"Detalhe: {exc.detail}\n"
            f"{tb}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": f"{exc.detail}", "error_id": error_id},
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    error_id = uuid.uuid4().hex[:8].upper()
    logger.error(
        f"[{error_id}] Exceção não tratada: {request.method} {request.url.path}\n"
        f"{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Erro interno inesperado.", "error_id": error_id},
    )


# Banco (pool de conexões) e autenticação vivem em app/core, pra os routers de
# app/ usarem sem importar o main. Os nomes continuam acessíveis como main.X.
from app.core.db import (_request_conns, _abrir_conexao, _pool_db, _PoolConexoes, _ConexaoDoPool,
                         DB_POOL_MAX, DB_POOL_ESPERA_S, DB_POOL_TESTAR_APOS_S, get_db)
from app.core.auth import (hash_senha, _hash_legado, senha_confere, _senha_fraca,
                           SESSAO_LAST_SEEN_S, limpar_sessoes_expiradas, get_session, _is_dev)
from app.core.paginas import (_redirect_login_ou_home, _HTML_SEM_CACHE)  # core:paginas
from app.core.agenda import (_hoje_sp, _bloqueio_ativo, _dias_bloqueados_periodo)  # core:agenda
from app.core.email import (_resolver_system_url, enviar_email_acesso, enviar_email_boas_vindas, _brevo_send, _shell_email)  # core:email
from app.core.acesso import (TIMES_PADRAO, CARGO_VALIDOS, CARGOS_SERVICE_DESK, PERFIL_VALIDOS, TIME_STATUS_REPORT, _times_validos_cur, times_validos, _area_usa_projetos_cur, _area_lista_cur, _area_cargos_cur, _area_perfis_cur, _exigir_area_com_projeto, _eh_n2, _pode_ver_status_report, _exigir_status_report, _perfil_guia, _eh_backoffice, _can_gestao)  # core:acesso

# Toda conexão aberta num request é fechada (devolvida ao pool) no fim dele,
# mesmo nos caminhos de erro que dão `raise` sem `conn.close()`.
@app.middleware("http")
async def fechar_conexoes_db(request, call_next):
    conns = []
    token = _request_conns.set(conns)
    try:
        return await call_next(request)
    finally:
        for c in conns:
            try: c.close()
            except Exception: pass
        _request_conns.reset(token)

Path("static/css").mkdir(parents=True, exist_ok=True)
Path("static/js").mkdir(parents=True, exist_ok=True)


from app.service_desk.db import AREA_SERVICE_DESK  # nome da área que o módulo usa

# Régua inicial de complexidade (planilha "Performance projetos — Peso das
# atividades"). Serve só como carga inicial: o INSERT usa ON CONFLICT DO
# NOTHING, então peso ajustado na tela de admin não é sobrescrito quando o
# app reinicia.
SEED_PESOS = {
    ("Projetos", "Backoffice"): [
        ("Faturamento", 4, "Faturamento do projeto"),
        ("Dailys / gestão de agenda com o cliente", 4, "Rotina estratégica (ex.: McDonald's)"),
        ("Atualização de inventário", 4, "Ex.: inventários da NTT ou validação de equipamentos do McDonald's"),
        ("Criação de cronograma", 4, "Cronograma de atividades dos projetos"),
        ("Validação de pagamento de parceiro", 3, "Conferência e validação financeira do parceiro"),
        ("Atualização de controles / cronograma", 3, "Manutenção dos controles do projeto"),
        ("Caderno de serviço", 3, "Elaboração e atualização do caderno de serviço"),
        ("Atualização de dashboard do cliente", 3, "Ex.: dashboard do McDonald's, NTT etc."),
        ("Acompanhamento / tracking", 2, "Criação de grupos e monitoramento do andamento da atividade"),
        ("Interação básica com o cliente", 2, "Posicionamento e alinhamentos simples"),
        ("Relatório simples", 2, "Consolidação de informação do projeto"),
        ("Validação de seguro", 2, "Verificação de cobertura/seguro"),
        ("Solicitação de equipamentos", 2, "Pedido e controle de equipamentos"),
        ("Acionamento", 1, "Abertura/acionamento simples de chamado"),
        ("Interação em e-mails", 1, "Trocas de e-mail de rotina"),
    ],
    ("Projetos", "N2"): [
        ("Atendimento em campo", 4, "Execução técnica presencial no site"),
        ("Suporte remoto complexo", 4, "Suporte a ativos de rede e servidores"),
        ("Reorganização de rack", 4, "Organização física de rack"),
        ("Configuração/staging de equipamentos", 3, "Staging, atualização de IOS"),
        ("Criação de relatórios e manuais", 2, "Documentação de apoio N1/N2"),
        ("Suporte remoto simples", 1, "Acompanhamento remoto e coleta de evidências"),
        ("Gestão de incidentes / planilhas", 1, "Registro e controle de incidentes"),
    ],
}

def _seed_catalogo_pesos(cur):
    for (area, frente), tipos in SEED_PESOS.items():
        cur.execute("INSERT INTO frentes (area, nome) VALUES (%s,%s) ON CONFLICT (area, nome) DO NOTHING", (area, frente))
        cur.execute("SELECT id FROM frentes WHERE area=%s AND nome=%s", (area, frente))
        fid = cur.fetchone()[0]
        for nome, peso, desc in tipos:
            cur.execute(
                "INSERT INTO tipos_atividade (frente_id, nome, peso, descricao) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (frente_id, nome) DO NOTHING",
                (fid, nome, peso, desc)
            )
            

def setup_banco():
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) UNIQUE NOT NULL,
                senha_hash VARCHAR(64) NOT NULL,
                nome VARCHAR(100) NOT NULL,
                perfil VARCHAR(20) NOT NULL DEFAULT 'funcionario',
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS primeiro_acesso BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultimo_acesso TIMESTAMP DEFAULT NULL")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email VARCHAR(200) DEFAULT ''")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS time VARCHAR(50) DEFAULT 'Projetos'")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS cargo VARCHAR(20) DEFAULT ''")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS tutorial_n2_visto BOOLEAN DEFAULT FALSE")
        # Tour do gestor (2026-10-07). A coluna nasce TRUE pra quem já existe
        # (gestor antigo não leva o tour de surpresa) e o default passa a FALSE
        # pros cadastros novos -- o ADD COLUMN só roda uma vez, o SET DEFAULT
        # é idempotente. Quem já existe revê pelo "Rever tour" do menu.
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS tutorial_gestor_visto BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE usuarios ALTER COLUMN tutorial_gestor_visto SET DEFAULT FALSE")
        # bcrypt gera 60 caracteres; a coluna nasceu VARCHAR(64) e fica sem
        # folga. Ampliar é seguro (não trunca nada já gravado).
        cur.execute("ALTER TABLE usuarios ALTER COLUMN senha_hash TYPE VARCHAR(255)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tarefas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id),
                descricao TEXT NOT NULL,
                cliente VARCHAR(50),
                prioridade VARCHAR(20) DEFAULT 'Media',
                status VARCHAR(30) DEFAULT 'aberto',
                segundos INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comentarios (
                id SERIAL PRIMARY KEY,
                tarefa_id INTEGER REFERENCES tarefas(id) ON DELETE CASCADE,
                usuario_id INTEGER REFERENCES usuarios(id),
                texto TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notificacoes (
                id SERIAL PRIMARY KEY,
                tipo VARCHAR(50) NOT NULL,
                mensagem TEXT NOT NULL,
                lida BOOLEAN DEFAULT FALSE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE notificacoes ADD COLUMN IF NOT EXISTS usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")
        cur.execute("ALTER TABLE notificacoes ADD COLUMN IF NOT EXISTS destinatario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tarefa_colaboradores (
                tarefa_id INTEGER REFERENCES tarefas(id) ON DELETE CASCADE,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                PRIMARY KEY (tarefa_id, usuario_id)
            )
        """)
        # ── Histórico de alterações das tarefas (resumo diário) ──
        # Guarda snapshots (nome do autor, tarefa, projeto) para sobreviver a
        # exclusões e mudanças de cadastro. Cada linha = uma alteração de campo.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tarefa_historico (
                id SERIAL PRIMARY KEY,
                tarefa_id INTEGER,
                tarefa_desc TEXT,
                autor_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                autor_nome VARCHAR(100),
                acao VARCHAR(20),
                campo VARCHAR(40),
                valor_antigo TEXT,
                valor_novo TEXT,
                projeto_id INTEGER,
                projeto_nome VARCHAR(120),
                time_tarefa VARCHAR(50) DEFAULT 'Projetos',
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hist_criado ON tarefa_historico(criado_em)")
        cur.execute("SELECT id FROM usuarios WHERE usuario = 'admin'")
        if not cur.fetchone():
            # Nunca nasce com senha fixa. Usa ADMIN_INITIAL_PASSWORD se definida,
            # senão gera uma aleatória forte (impressa no log 1x). Em ambos os casos
            # exige troca no primeiro acesso (primeiro_acesso=TRUE).
            admin_pwd = os.environ.get("ADMIN_INITIAL_PASSWORD", "").strip()
            gerada = admin_pwd == ""
            if gerada:
                admin_pwd = secrets.token_urlsafe(12)
            cur.execute(
                "INSERT INTO usuarios (usuario, senha_hash, nome, perfil, primeiro_acesso) "
                "VALUES (%s, %s, %s, %s, TRUE)",
                ('admin', hash_senha(admin_pwd), 'Administrador', 'admin')
            )
            if gerada:
                print(f"[setup] Usuário 'admin' criado com senha TEMPORÁRIA gerada: {admin_pwd}  "
                      f"— troca obrigatória no primeiro acesso.")
            else:
                print("[setup] Usuário 'admin' criado a partir de ADMIN_INITIAL_PASSWORD "
                      "— troca obrigatória no primeiro acesso.")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave VARCHAR(100) PRIMARY KEY,
                valor TEXT NOT NULL,
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessoes (
                token VARCHAR(64) PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                nome VARCHAR(100) NOT NULL,
                perfil VARCHAR(20) NOT NULL,
                time_usuario VARCHAR(50) DEFAULT 'Projetos',
                pagina VARCHAR(100) DEFAULT '',
                last_seen TIMESTAMP DEFAULT NOW(),
                expira_em TIMESTAMP DEFAULT NOW() + INTERVAL '24 hours'
            )
        """)
        cur.execute("ALTER TABLE sessoes ADD COLUMN IF NOT EXISTS cargo VARCHAR(20) DEFAULT ''")
        # ── Clientes e Projetos (criados aqui para gestão funcionar sem acessar financeiro) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(100) UNIQUE NOT NULL,
                contato VARCHAR(100),
                email VARCHAR(100),
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS telefone VARCHAR(50) DEFAULT ''")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS cnpj VARCHAR(30) DEFAULT ''")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS observacoes TEXT DEFAULT ''")
        # Bug pré-existente: esta coluna só era criada dentro do handler GET
        # /api/clientes (main.py, listar_clientes), nunca no setup_banco().
        # Num banco novo, chamar POST /api/clientes antes de qualquer GET
        # quebrava com "column \"time\" of relation \"clientes\" does not
        # exist" — achado testando o módulo Status de Campo contra um
        # Postgres de staging genuinamente vazio.
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS time VARCHAR(50) DEFAULT 'Projetos'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projetos (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                nome VARCHAR(100) NOT NULL,
                descricao TEXT DEFAULT '',
                orcamento NUMERIC(14,2) DEFAULT 0,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS planilha_url TEXT DEFAULT ''")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS planilha_mapeamento JSONB")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS planilha_sync_em TIMESTAMP")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS planilha_replace BOOLEAN DEFAULT FALSE")
        # Projeto "leve" sem cliente/financeiro (2026-08-21) -- pra times como
        # o Desenvolvimento, que trabalham em iniciativa interna, não em
        # contrato de cliente. cliente_id vira opcional; quando NULL, o
        # próprio "time" da linha escopa o projeto (ver POST /api/projetos).
        cur.execute("ALTER TABLE projetos ALTER COLUMN cliente_id DROP NOT NULL")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS time VARCHAR(50) DEFAULT 'Projetos'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lancamentos (
                id SERIAL PRIMARY KEY,
                projeto_id INTEGER NOT NULL REFERENCES projetos(id),
                descricao VARCHAR(200) NOT NULL,
                categoria VARCHAR(50) DEFAULT 'Outros',
                valor NUMERIC(14,2) NOT NULL,
                data_lancamento DATE DEFAULT CURRENT_DATE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE lancamentos ADD COLUMN IF NOT EXISTS localidade VARCHAR(150) DEFAULT ''")
        cur.execute("ALTER TABLE lancamentos ADD COLUMN IF NOT EXISTS tecnico VARCHAR(150) DEFAULT ''")
        # ── Gestão de Projetos ──────────────────────────────────────────────
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS responsavel_id INTEGER REFERENCES usuarios(id) DEFAULT NULL")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS status_gestao VARCHAR(30) DEFAULT 'EM ANDAMENTO'")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS data_inicio DATE DEFAULT NULL")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS data_termino DATE DEFAULT NULL")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS escopo TEXT DEFAULT ''")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS codigo VARCHAR(20) DEFAULT ''")
        cur.execute("ALTER TABLE projetos ADD COLUMN IF NOT EXISTS responsavel_texto VARCHAR(100) DEFAULT ''")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contratos_gestao (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER REFERENCES clientes(id) ON DELETE CASCADE,
                nome VARCHAR(200) NOT NULL,
                sdm VARCHAR(100) DEFAULT '',
                responsavel_id INTEGER REFERENCES usuarios(id) DEFAULT NULL,
                status VARCHAR(30) DEFAULT 'EM IMPLANTAÇÃO',
                data_inicio DATE DEFAULT NULL,
                data_termino DATE DEFAULT NULL,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comentarios_projeto (
                id SERIAL PRIMARY KEY,
                projeto_id INTEGER REFERENCES projetos(id) ON DELETE CASCADE,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                usuario_nome VARCHAR(100) NOT NULL,
                texto TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # ── Status de Campo (despachos técnicos por site/cliente) ───────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS status_atividades (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER REFERENCES clientes(id),
                data DATE NOT NULL DEFAULT CURRENT_DATE,
                horario_agendado TIME,
                tecnico VARCHAR(150) DEFAULT '',
                n2_responsavel VARCHAR(150) DEFAULT '',
                site_sigla VARCHAR(50) DEFAULT '',
                site_nome VARCHAR(150) DEFAULT '',
                endereco TEXT DEFAULT '',
                cidade VARCHAR(100) DEFAULT '',
                uf VARCHAR(2) DEFAULT '',
                hora_chegada TIME,
                hora_termino TIME,
                detalhamento_tecnico TEXT DEFAULT '',
                status VARCHAR(20) NOT NULL DEFAULT 'agendado',
                observacoes TEXT DEFAULT '',
                criado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_status_ativ_data ON status_atividades(data)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_status_ativ_cliente ON status_atividades(cliente_id)")
        # n2_responsavel era só texto livre; n2_usuario_id referencia o usuário
        # de verdade (perfil n2) responsável, n2_responsavel vira cache de
        # exibição do nome (mesmo padrão de comentarios_projeto.usuario_nome).
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS n2_usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_status_ativ_n2 ON status_atividades(n2_usuario_id)")
        # Cadastro real de técnicos terceirizados (2026-08-11, importado de
        # planilha externa -- ~6300 registros). codigo_origem é o ID do
        # sistema de origem, UNIQUE pra permitir reimportar sem duplicar.
        # RG/CPF são dado sensível: nunca vão na listagem geral, só no
        # endpoint de detalhe (GET /api/tecnicos/{id}), gated por perfil.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tecnicos (
                id SERIAL PRIMARY KEY,
                codigo_origem VARCHAR(20) UNIQUE,
                nome VARCHAR(200) NOT NULL,
                rg VARCHAR(30) DEFAULT '',
                cpf_cnpj VARCHAR(20) DEFAULT '',
                estado VARCHAR(2) DEFAULT '',
                cidade VARCHAR(100) DEFAULT '',
                telefone VARCHAR(30) DEFAULT '',
                email VARCHAR(200) DEFAULT '',
                especialidade VARCHAR(200) DEFAULT '',
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tecnicos_estado ON tecnicos(estado)")
        # tecnico_id referencia o cadastro real (quando o nome digitado bate
        # com um técnico conhecido); tecnico (texto) continua existindo como
        # cache de exibição -- mesmo padrão de n2_usuario_id/n2_responsavel
        # acima. Fica NULL pra atividades antigas ou técnico não cadastrado.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS tecnico_id INTEGER REFERENCES tecnicos(id) ON DELETE SET NULL")
        # Particularidades (ex.: Reversa, Equipamento em posse do cliente) --
        # lista aberta em vez de booleans fixos, pra não exigir migração toda
        # vez que surgir uma nova. Material: se foi usado + detalhe em texto,
        # preenchido pelo N2 quando "sim".
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS particularidades TEXT[] NOT NULL DEFAULT '{}'")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS material_utilizado BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS material_detalhe TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS material_quantidade INTEGER")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS material_valor NUMERIC(10,2)")
        # Ticket de suporte associado (preenchido na criação, ex. via import
        # de planilha) -- e "hora_termino" passa a ser usada como "hora de
        # saída", preenchida pelo N2 ao finalizar (sem precisar de coluna nova).
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS ticket VARCHAR(100) DEFAULT ''")
        # Descrição livre do que está acontecendo, preenchida pelo N2 ao
        # mudar o status pra "em_andamento" (distinto de observacoes, que é
        # a nota final de encerramento). localizacao/acesso e o histórico
        # completo de atualizações ficam em status_atividade_andamentos --
        # essas 3 colunas aqui são só o "snapshot" mais recente, pra listar
        # e filtrar sem precisar de JOIN toda hora.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS andamento_descricao TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS localizacao VARCHAR(20)")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS acesso VARCHAR(20)")
        # Subprojeto: texto livre digitado por quem cria a atividade,
        # reaproveitado como sugestão (autocomplete) pros próximos cadastros
        # do mesmo cliente -- ver /api/status-campo/subprojetos.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS subprojeto VARCHAR(150) DEFAULT ''")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS equipamento_removido_detalhe TEXT NOT NULL DEFAULT ''")
        # Detalhe estruturado de equipamento instalado/removido ao finalizar
        # (particularidades 'equipamento_instalado'/'equipamento_removido'
        # dizem O QUE aconteceu; estas colunas guardam serial/partnumber).
        # A posse (técnico/cliente) do equipamento removido reaproveita a
        # particularidade 'equipamento_em_posse_do_cliente' já existente.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS equipamento_instalado_serial VARCHAR(100) DEFAULT ''")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS equipamento_removido_partnumber VARCHAR(100) DEFAULT ''")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS equipamento_removido_serial VARCHAR(100) DEFAULT ''")
        # Contato local no momento do atendimento, pro carimbo de encerramento.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS contato_local_nome VARCHAR(150) DEFAULT ''")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS contato_local_matricula VARCHAR(50) DEFAULT ''")
        # Fluxo escalonado do N2 (tela n2.html): localizacao -> (se no_local)
        # hora_chegada + acesso -> (se com_acesso) hora_inicio_atividade +
        # andamento_tipo (o que está fazendo agora). Snapshot mais recente
        # aqui, histórico completo em status_atividade_andamentos.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS hora_inicio_atividade TIME")
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS andamento_tipo VARCHAR(20)")
        # O que especificamente está sendo instalado/trocado/removido (ex.:
        # "Switch Catalyst 9300") -- deixa o andamento_tipo (categoria) bem
        # mais evidente quando exibido junto, em vez de só a categoria sozinha.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS andamento_equipamento VARCHAR(200) DEFAULT ''")
        # N2-A (integração com medição de peso): ao finalizar a visita, o
        # sistema cria sozinho uma tarefa "Atendimento em campo" (peso 4).
        # Guarda o id gerado aqui pra reeditar a mesma tarefa se a atividade
        # for finalizada de novo (corrigir status/data), em vez de duplicar.
        cur.execute("ALTER TABLE status_atividades ADD COLUMN IF NOT EXISTS tarefa_gerada_id INTEGER REFERENCES tarefas(id) ON DELETE SET NULL")
        # Equipamentos instalados/removidos ao finalizar -- lista (não mais
        # um campo único), porque uma atividade pode envolver mais de uma
        # unidade instalada e/ou removida ao mesmo tempo. Substitui as
        # colunas equipamento_instalado_serial/equipamento_removido_partnumber/
        # equipamento_removido_serial acima (mantidas na tabela, não usadas
        # mais, pra não exigir DROP COLUMN destrutivo nesta etapa).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS status_atividade_equipamentos (
                id SERIAL PRIMARY KEY,
                atividade_id INTEGER NOT NULL REFERENCES status_atividades(id) ON DELETE CASCADE,
                tipo VARCHAR(20) NOT NULL,
                partnumber VARCHAR(100) DEFAULT '',
                serial VARCHAR(100) DEFAULT '',
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Posse (técnico/cliente) por item removido (2026-08-11) -- antes era
        # uma decisão única pra atividade inteira (particularidade
        # 'equipamento_em_posse_do_cliente'), mas cada equipamento removido
        # pode ter destino diferente. Só é lida/usada quando tipo='removido'.
        cur.execute("ALTER TABLE status_atividade_equipamentos ADD COLUMN IF NOT EXISTS posse VARCHAR(20) DEFAULT NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_status_equip_ativ ON status_atividade_equipamentos(atividade_id)")
        # Materiais utilizados ao finalizar -- lista (não mais um campo
        # único), porque uma visita pode usar mais de um material (ex.:
        # cabo de rede + conectores). quantidade é NUMERIC (não INTEGER)
        # porque nem todo material se conta em unidades -- ex. "28" com
        # unidade "metro" pra cabo de rede. Substitui material_detalhe/
        # material_quantidade/material_valor em status_atividades (mantidas
        # na tabela, não usadas mais, pra não exigir DROP COLUMN destrutivo).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS status_atividade_materiais (
                id SERIAL PRIMARY KEY,
                atividade_id INTEGER NOT NULL REFERENCES status_atividades(id) ON DELETE CASCADE,
                descricao TEXT NOT NULL DEFAULT '',
                quantidade NUMERIC(10,2),
                unidade VARCHAR(20) DEFAULT 'unidade',
                valor NUMERIC(10,2),
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_status_material_ativ ON status_atividade_materiais(atividade_id)")
        # Backfill único: atividades já finalizadas antes desta migração que
        # tinham material_detalhe preenchido (campo único antigo) ganham uma
        # linha equivalente na tabela nova, pra não sumir do histórico/report.
        cur.execute("""
            INSERT INTO status_atividade_materiais (atividade_id, descricao, quantidade, unidade, valor)
            SELECT a.id, a.material_detalhe, a.material_quantidade, 'unidade', a.material_valor
            FROM status_atividades a
            WHERE a.material_utilizado = TRUE AND COALESCE(a.material_detalhe, '') != ''
              AND NOT EXISTS (SELECT 1 FROM status_atividade_materiais m WHERE m.atividade_id = a.id)
        """)
        # Histórico de atualizações de andamento (item: N2 pode registrar
        # quantas atualizações quiser durante a atividade -- ex. fixação no
        # rack, configuração, validação -- em vez de um campo único que só
        # dá pra preencher uma vez).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS status_atividade_andamentos (
                id SERIAL PRIMARY KEY,
                atividade_id INTEGER NOT NULL REFERENCES status_atividades(id) ON DELETE CASCADE,
                localizacao VARCHAR(20),
                acesso VARCHAR(20),
                descricao TEXT NOT NULL DEFAULT '',
                criado_por INTEGER,
                criado_por_nome VARCHAR(100),
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_status_andamento_ativ ON status_atividade_andamentos(atividade_id)")
        cur.execute("ALTER TABLE status_atividade_andamentos ADD COLUMN IF NOT EXISTS hora_chegada TIME")
        cur.execute("ALTER TABLE status_atividade_andamentos ADD COLUMN IF NOT EXISTS hora_inicio_atividade TIME")
        cur.execute("ALTER TABLE status_atividade_andamentos ADD COLUMN IF NOT EXISTS andamento_tipo VARCHAR(20)")
        cur.execute("ALTER TABLE status_atividade_andamentos ADD COLUMN IF NOT EXISTS andamento_equipamento VARCHAR(200) DEFAULT ''")
        # ── Escala N2 (plantão do dia: quem, horário, home/presencial) ──────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS escala_n2 (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL DEFAULT CURRENT_DATE,
                n2_usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                horario_entrada TIME,
                modalidade VARCHAR(20) NOT NULL DEFAULT 'presencial',
                atribuicao VARCHAR(200) DEFAULT '',
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_escala_n2_data ON escala_n2(data)")

        # ── Bloqueios de agenda (férias, afastamento, recorrência semanal) ──
        # Vale pra qualquer funcionário do sistema -- usado tanto pra
        # atividades de campo (N2) quanto pra tarefas em geral.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS funcionario_bloqueios (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                tipo VARCHAR(20) NOT NULL,
                data_inicio DATE,
                data_fim DATE,
                dia_semana SMALLINT,
                hora_inicio TIME,
                hora_fim TIME,
                descricao VARCHAR(200) DEFAULT '',
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_funcionario_bloqueios_usuario ON funcionario_bloqueios(usuario_id)")

        # ── Equipe Dev: kanban interno de tarefas do app, restrito a quem
        # tem perfil 'dev' (só os desenvolvedores do sistema, não admins comuns).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dev_tarefas (
                id SERIAL PRIMARY KEY,
                titulo VARCHAR(200) NOT NULL,
                descricao TEXT DEFAULT '',
                status VARCHAR(20) NOT NULL DEFAULT 'todo',
                ordem INTEGER NOT NULL DEFAULT 0,
                criado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                atribuido_a INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dev_tarefas_status ON dev_tarefas(status)")
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS prioridade VARCHAR(10) NOT NULL DEFAULT 'media'")
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS prazo DATE")
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'")
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS link TEXT NOT NULL DEFAULT ''")
        # Pausar volta a tarefa pra 'todo' (A Fazer), mas com destaque visual de que
        # já foi iniciada -- diferencia de uma tarefa que nunca foi começada.
        cur.execute("ALTER TABLE dev_tarefas ADD COLUMN IF NOT EXISTS pausado BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dev_tarefa_comentarios (
                id SERIAL PRIMARY KEY,
                tarefa_id INTEGER NOT NULL REFERENCES dev_tarefas(id) ON DELETE CASCADE,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                usuario_nome VARCHAR(100) NOT NULL,
                texto TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dev_coment_tarefa ON dev_tarefa_comentarios(tarefa_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dev_tarefa_checklist (
                id SERIAL PRIMARY KEY,
                tarefa_id INTEGER NOT NULL REFERENCES dev_tarefas(id) ON DELETE CASCADE,
                texto VARCHAR(300) NOT NULL,
                concluido BOOLEAN NOT NULL DEFAULT FALSE,
                ordem INTEGER NOT NULL DEFAULT 0,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dev_checklist_tarefa ON dev_tarefa_checklist(tarefa_id)")
        # Diário da equipe dev: registro cronológico simples do que foi
        # mudado no sistema, pra quem não estava na sessão entender o que
        # já foi feito sem precisar reconstruir pelo git log ou perguntar.
        # De propósito sem status/tags/prioridade -- só título, descrição,
        # autor e data, o mínimo pra não repetir trabalho.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dev_diario (
                id SERIAL PRIMARY KEY,
                titulo VARCHAR(200) NOT NULL,
                descricao TEXT DEFAULT '',
                autor_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                autor_nome VARCHAR(100) NOT NULL,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Solicitação de suporte: qualquer usuário logado pode abrir (pedido
        # explícito adiado desde 2026-07-24, "sentiram falta de algo pra
        # relatar pro suporte"). Só quem é dev (perfil_real='dev', ver
        # _is_dev) vê/gerencia -- é canal de envio, sem acompanhamento de
        # status pro usuário que abriu. Anexo em base64 direto no Postgres
        # por simplicidade (Railway não tem disco persistente entre
        # deploys, mesmo motivo já documentado pra foto de serial).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suporte_solicitacoes (
                id SERIAL PRIMARY KEY,
                titulo VARCHAR(200) NOT NULL,
                descricao TEXT NOT NULL,
                categoria VARCHAR(20) NOT NULL DEFAULT 'duvida',
                anexo_base64 TEXT,
                anexo_nome VARCHAR(200),
                status VARCHAR(20) NOT NULL DEFAULT 'aberto',
                criado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                criado_por_nome VARCHAR(150) NOT NULL,
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Resposta do dev pra quem abriu a solicitação (2026-08-11) -- antes
        # era só um canal de envio sem volta nenhuma pra quem pediu.
        cur.execute("ALTER TABLE suporte_solicitacoes ADD COLUMN IF NOT EXISTS resposta TEXT")
        cur.execute("ALTER TABLE suporte_solicitacoes ADD COLUMN IF NOT EXISTS resposta_por_nome VARCHAR(150)")
        cur.execute("ALTER TABLE suporte_solicitacoes ADD COLUMN IF NOT EXISTS resposta_em TIMESTAMP")
        # Thread de mensagens do chamado (2026-09-09) -- substitui a resposta
        # única acima por um vai-e-vem de verdade: dev responde, quem abriu
        # recebe por e-mail e responde de volta pelo sistema. Migra em toda
        # subida (idempotente via NOT EXISTS) a resposta única já registrada
        # pra virar a primeira mensagem da thread, sem perder histórico.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suporte_mensagens (
                id SERIAL PRIMARY KEY,
                solicitacao_id INTEGER NOT NULL REFERENCES suporte_solicitacoes(id) ON DELETE CASCADE,
                autor_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                autor_nome VARCHAR(150) NOT NULL,
                autor_tipo VARCHAR(10) NOT NULL DEFAULT 'dev',
                mensagem TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_suporte_mensagens_solicitacao ON suporte_mensagens(solicitacao_id)")
        cur.execute("""
            INSERT INTO suporte_mensagens (solicitacao_id, autor_nome, autor_tipo, mensagem, criado_em)
            SELECT s.id, s.resposta_por_nome, 'dev', s.resposta, COALESCE(s.resposta_em, s.atualizado_em)
            FROM suporte_solicitacoes s
            WHERE s.resposta IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM suporte_mensagens m WHERE m.solicitacao_id = s.id)
        """)
        # Reset de senha por e-mail (2026-07-30). Guarda o HASH do token, não
        # o token em si -- mesmo princípio de senha_hash: se o banco vazar, o
        # hash sozinho não deixa ninguém reutilizar o link. sha256 (sem salt)
        # é suficiente aqui porque o token é aleatório de alta entropia
        # (32 bytes), diferente de senha de humano -- não tem o que quebrar
        # por força bruta/rainbow table num espaço desse tamanho.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS senha_reset_tokens (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                criado_em TIMESTAMP DEFAULT NOW(),
                expira_em TIMESTAMP NOT NULL,
                usado_em TIMESTAMP DEFAULT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reset_token_hash ON senha_reset_tokens(token_hash)")
        # Integração Microsoft Loop (2026-08-04, pedido do Jeff) -- snapshot
        # periódico de página(s) do Loop, exportadas como HTML via Graph API
        # e cacheadas aqui. Só-leitura (não escreve de volta no Loop). Uma
        # linha por "chave" configurada (ver LOOP_ITENS/_loop_sync_job mais
        # abaixo) -- hoje area_dev e gestao_projetos. Ver
        # wiki/entities/dashboard-faiston-integracao-microsoft-loop.md pro
        # desenho completo e o que falta (Entra ID App Registration).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS loop_snapshots (
                chave VARCHAR(50) PRIMARY KEY,
                html_conteudo TEXT NOT NULL DEFAULT '',
                atualizado_em TIMESTAMP,
                erro TEXT
            )
        """)
                # --- CATÁLOGO DE COMPLEXIDADE ---
        # Peso de esforço por tipo de atividade, por frente (N2, Backoffice...)
        # dentro da área (Projetos, Logística, Rede Credenciada). Cadastro em
        # vez de planilha: o gestor ajusta a régua pela tela de admin.
        # Cadastro de áreas (2026-08-28). `usa_projetos=FALSE` é a área que não
        # trabalha por projeto: a tarefa dela é sempre demanda avulsa, ela não
        # aparece como dona de projeto e a API recusa os dois caminhos (ver
        # _exigir_area_com_projeto). Antes disso a lista era fixa no código.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS areas (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(50) UNIQUE NOT NULL,
                usa_projetos BOOLEAN NOT NULL DEFAULT TRUE,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Carga inicial só em banco novo (tabela vazia). Rodando a cada boot com
        # ON CONFLICT, área renomeada pelo admin voltava com o nome antigo,
        # vazia, no deploy seguinte. 'Projetos' é exceção: é a área padrão
        # do sistema (TIME_STATUS_REPORT) e não pode ser renomeada.
        cur.execute("SELECT COUNT(*) FROM areas")
        for _area_seed in (TIMES_PADRAO if cur.fetchone()[0] == 0 else ("Projetos",)):
            cur.execute("INSERT INTO areas (nome) VALUES (%s) ON CONFLICT (nome) DO NOTHING",
                        (_area_seed,))
        # Quais cargos e perfis a área aceita (2026-08-28). Guardado como lista
        # explícita, não como "NULL = todos": assim o admin enxerga na tela
        # exatamente o que está ligado, e um cargo novo no catálogo do código
        # não entra sozinho em área nenhuma.
        import json as _json
        cur.execute("ALTER TABLE areas ADD COLUMN IF NOT EXISTS cargos JSONB")
        cur.execute("ALTER TABLE areas ADD COLUMN IF NOT EXISTS perfis JSONB")
        cur.execute("UPDATE areas SET cargos=%s WHERE cargos IS NULL",
                    (_json.dumps([c for c in CARGO_VALIDOS if c not in CARGOS_SERVICE_DESK]),))
        cur.execute("UPDATE areas SET perfis=%s WHERE perfis IS NULL", (_json.dumps(list(PERFIL_VALIDOS)),))
        # Área criada pela tela antes de 2026-10-08 nascia com os cargos do SD
        # ligados (ver _cargos_da_area) -- tira de todas menos a do SD.
        cur.execute("""UPDATE areas SET cargos = cargos - 'sd_operador' - 'sd_supervisor'
                       WHERE nome <> %s AND (cargos ? 'sd_operador' OR cargos ? 'sd_supervisor')""",
                    (AREA_SERVICE_DESK,))
        cur.execute("""
            CREATE TABLE IF NOT EXISTS frentes (
                id SERIAL PRIMARY KEY,
                area VARCHAR(50) NOT NULL DEFAULT 'Projetos',
                nome VARCHAR(50) NOT NULL,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW(),
                UNIQUE (area, nome)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tipos_atividade (
                id SERIAL PRIMARY KEY,
                frente_id INTEGER NOT NULL REFERENCES frentes(id) ON DELETE CASCADE,
                nome VARCHAR(120) NOT NULL,
                peso SMALLINT NOT NULL CHECK (peso BETWEEN 1 AND 4),
                descricao TEXT NOT NULL DEFAULT '',
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW(),
                UNIQUE (frente_id, nome)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tipos_ativ_frente ON tipos_atividade(frente_id)")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS frente_id INTEGER REFERENCES frentes(id)")
        # Migração 2026-07-28: N2 deixa de ser perfil próprio e vira cargo
        # dentro de perfil='funcionario' (mesmo nível de Analista/Backoffice).
        # Idempotente -- não repete em usuário já migrado.
        cur.execute("UPDATE usuarios SET perfil='funcionario', cargo='n2' WHERE perfil='n2'")
        _seed_catalogo_pesos(cur)
                # Medição de performance: tipo de atividade escolhido na abertura e
        # peso carimbado na própria tarefa -- não resolvido por JOIN, pra que
        # reajuste de régua não altere relatório de período já fechado.
        # Prazo/agendamento/projeto nasceram como migração silenciosa dentro do
        # /api/tarefas. Repetidos aqui (idempotente) pra que um banco novo já
        # suba completo: a sinalização de carga lê data_prazo sem depender de
        # alguém ter aberto a listagem de tarefas antes.
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS projeto_id INTEGER REFERENCES projetos(id)")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS data_prazo DATE")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS data_agendamento DATE")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS hora_prazo TIME")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS tipo_atividade_id INTEGER REFERENCES tipos_atividade(id)")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS peso SMALLINT")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS peso_estimado BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS natureza VARCHAR(12) NOT NULL DEFAULT 'programada'")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tarefas_tipo_ativ ON tarefas(tipo_atividade_id)")
        # Tarefas anteriores à medição entram com peso 2 (conservador -- a média
        # da régua é ~2,7) e marcadas como estimadas, pra poderem ser isoladas
        # nos indicadores depois.
        cur.execute("UPDATE tarefas SET peso = 2, peso_estimado = TRUE WHERE peso IS NULL")
        # Fechamento: quando a tarefa foi concluída de fato (atualizado_em não
        # serve, muda a cada edição), classificação dentro/fora do prazo
        # carimbada no momento do fechamento e justificativa do atraso.
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS concluido_em TIMESTAMP")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS prazo_status VARCHAR(10)")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS justificativa_atraso TEXT NOT NULL DEFAULT ''")
        conn.commit(); cur.close(); conn.close()
        print("✅ Banco configurado")
    except Exception as e:
        print(f"Erro setup: {e}")

setup_banco()

def _migrar_timer_tarefas():
    """Timer no servidor (2026-09-28). Antes o cronômetro vivia só na memória
    do navegador: um timer por vez, e recarregar/fechar a aba fazia a tarefa
    ficar 'em andamento' sem contar. Agora `timer_inicio` marca desde quando o
    timer daquela tarefa está rodando (NULL = pausado) e `segundos` guarda o
    acumulado das sessões já fechadas -- total = segundos + (agora - timer_inicio).
    Várias tarefas podem ter timer rodando ao mesmo tempo. Separado do
    setup_banco pra não depender de nenhum passo anterior dele ter dado certo."""
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS timer_inicio TIMESTAMPTZ")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tarefas_timer_ativo ON tarefas(timer_inicio) WHERE timer_inicio IS NOT NULL")
        conn.commit(); cur.close()
    except Exception as e:
        print(f"Erro migração timer: {e}")
    finally:
        conn.close()

_migrar_timer_tarefas()

from app.assistente.db import setup_schema as _setup_schema_assistente
from app.assistente.router import router as assistente_router
_setup_schema_assistente()
app.include_router(assistente_router)

# Service Desk (operação SGB): módulo isolado, só mexe nas tabelas sd_*.
from app.service_desk.db import setup_schema as _setup_schema_service_desk
from app.service_desk.router import router as service_desk_router
_setup_schema_service_desk()
app.include_router(service_desk_router)

# --- MODELOS ---
class LoginRequest(BaseModel):
    usuario: str
    senha: str


class NovoUsuario(BaseModel):
    usuario: str
    nome: str
    perfil: str
    email: str  # obrigatório (2026-08-10): é por ele que o acesso é enviado, não tem mais senha definida pelo admin
    time: str = "Projetos"
    cargo: str = ""  # só se aplica quando perfil == 'funcionario'

class AtualizarUsuario(BaseModel):
    nome: str
    perfil: str
    senha: str = ""
    ativo: bool = True
    email: str = ""
    time: str = "Projetos"
    cargo: str = ""  # só se aplica quando perfil == 'funcionario'

class TarefaModel(BaseModel):
    descricao: str
    cliente: str
    prioridade: str = "Media"
    status: str = "aberto"
    segundos: int = 0
    funcionario_id: Optional[int] = None
    projeto_id: Optional[int] = None
    data_prazo: Optional[str] = None
    data_agendamento: Optional[str] = None
    hora_prazo: Optional[str] = None
    tipo_atividade_id: Optional[int] = None
    natureza: Optional[str] = None
    justificativa_atraso: Optional[str] = None
    colaboradores: Optional[List[int]] = None

class AtualizarSegundos(BaseModel):
    segundos: int

class TrocarSenhaModel(BaseModel):
    nova_senha: str

class AcaoBackoffice(BaseModel):
    comando: str
    cliente: str = "Geral"


# ── Resumo diário de alterações nas tarefas ───────────────────────────────────
def montar_kpis(cur, hoje, time_filter: str, concluidas_hoje: int) -> str:
    """Painel-resumo no topo do e-mail: situação atual das tarefas em aberto.
    Conta o que está em andamento, o que foi concluído no dia e o que está em
    risco de estourar o prazo (atrasadas ou vencendo hoje), respeitando o time."""
    base = """FROM tarefas t
              LEFT JOIN usuarios u ON u.id = t.usuario_id
              WHERE t.status <> 'concluido'"""
    params = []
    if time_filter:
        base += " AND COALESCE(u.time,'Projetos') = %s"
        params.append(time_filter)
    cur.execute(f"""
        SELECT
          SUM(CASE WHEN t.status='em_andamento' THEN 1 ELSE 0 END),
          SUM(CASE WHEN t.data_prazo IS NOT NULL AND t.data_prazo < %s THEN 1 ELSE 0 END),
          SUM(CASE WHEN t.data_prazo = %s THEN 1 ELSE 0 END)
        {base}""", [hoje, hoje] + params)
    em_andamento, atrasadas, vence_hoje = (cur.fetchone() or (0, 0, 0))
    em_andamento = em_andamento or 0
    atrasadas = atrasadas or 0
    vence_hoje = vence_hoje or 0
    em_risco = atrasadas + vence_hoje

    def card(emoji, num, rotulo, cor, chip):
        return (f'<td width="32%" valign="top" style="padding:0">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:#FBFBFE;border:1px solid #ECEEF4;border-top:3px solid {cor};border-radius:13px">'
                f'<tr><td align="center" style="padding:17px 8px 15px">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 9px">'
                f'<tr><td style="width:38px;height:38px;background:{chip};border-radius:11px;text-align:center;'
                f'vertical-align:middle;font-size:18px;line-height:38px">{emoji}</td></tr></table>'
                f'<div style="font-size:32px;font-weight:800;color:{cor};line-height:1;letter-spacing:-1.2px">{num}</div>'
                f'<div style="font-size:10.5px;color:#8A90A2;text-transform:uppercase;letter-spacing:.7px;'
                f'font-weight:700;margin-top:6px">{rotulo}</div></td></tr></table></td>')

    eyebrow = ('<p style="margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#A78BFA">Panorama de hoje</p>')
    cards = (f'{eyebrow}'
             f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px">'
             f'<tr>'
             f'{card("✅", concluidas_hoje, "Concluídas hoje", "#0E9F6E", "#E3F6EE")}'
             f'<td width="10"></td>'
             f'{card("🚀", em_andamento, "Em andamento", "#5B2EE0", "#EEE8FE")}'
             f'<td width="10"></td>'
             f'{card("⚠️", em_risco, "Em risco do prazo", "#EF4444" if em_risco else "#9AA0AE", "#FDECEC" if em_risco else "#F1F2F6")}'
             f'</tr></table>')

    return cards


def montar_resumo_por_pessoa(cur, inicio, fim, time_filter: str = None) -> str:
    """Quebra por pessoa: quantas tarefas cada um concluiu no dia e quantas
    estão em andamento agora. A conclusão é atribuída ao dono da tarefa.
    Respeita o time (gestor vê só o seu). Retorna '' se não houver ninguém."""
    pessoas = {}  # nome -> {"concluidas": int, "andamento": int}

    # Concluídas no período (status → 'concluido'), atribuídas ao dono da tarefa
    q1 = """SELECT u.nome, COUNT(DISTINCT th.tarefa_id)
            FROM tarefa_historico th
            JOIN tarefas t ON t.id = th.tarefa_id
            JOIN usuarios u ON u.id = t.usuario_id
            WHERE th.acao = 'alterou' AND th.campo = 'status'
              AND th.valor_novo = 'concluido'
              AND th.criado_em >= %s AND th.criado_em < %s"""
    p1 = [inicio, fim]
    if time_filter:
        q1 += " AND COALESCE(u.time,'Projetos') = %s"
        p1.append(time_filter)
    q1 += " GROUP BY u.nome"
    cur.execute(q1, p1)
    for nome, n in cur.fetchall():
        pessoas.setdefault(nome, {"concluidas": 0, "andamento": 0})["concluidas"] = n or 0

    # Em andamento agora (estado atual das tarefas)
    q2 = """SELECT u.nome, COUNT(*)
            FROM tarefas t JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.status = 'em_andamento'"""
    p2 = []
    if time_filter:
        q2 += " AND COALESCE(u.time,'Projetos') = %s"
        p2.append(time_filter)
    q2 += " GROUP BY u.nome"
    cur.execute(q2, p2)
    for nome, n in cur.fetchall():
        pessoas.setdefault(nome, {"concluidas": 0, "andamento": 0})["andamento"] = n or 0

    if not pessoas:
        return ""

    # Quem produziu mais primeiro: concluídas desc, depois em andamento desc
    ordenado = sorted(pessoas.items(),
                      key=lambda kv: (-kv[1]["concluidas"], -kv[1]["andamento"], (kv[0] or "").lower()))
    medalhas = {0: "🥇", 1: "🥈", 2: "🥉"}
    cards = []
    for i, (nome, d) in enumerate(ordenado):
        concl, andam = d["concluidas"], d["andamento"]
        inicial = (nome or "?").strip()[:1].upper()
        medal = medalhas.get(i, "")
        medal_html = (f'&nbsp;<span style="font-size:13px;vertical-align:middle">{medal}</span>'
                      if medal and concl else "")

        # Barra de proporção: concluídas (verde) vs em andamento (roxo)
        tot = concl + andam
        if tot:
            w_concl = int(round(concl / tot * 100))
            seg_concl = (f'<td style="height:6px;background:#0E9F6E;font-size:0;line-height:0;'
                         f'width:{w_concl}%;border-radius:4px 0 0 4px">&nbsp;</td>' if concl else '')
            seg_andam = (f'<td style="height:6px;background:#8B5CF6;font-size:0;line-height:0;'
                         f'width:{100 - w_concl}%;border-radius:{"0 4px 4px 0" if concl else "4px"}">&nbsp;</td>'
                         if andam else '')
            barra = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     f'style="margin-top:12px"><tr>{seg_concl}{seg_andam}</tr></table>')
        else:
            barra = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     'style="margin-top:12px"><tr><td style="height:6px;background:#EEF0F6;'
                     'border-radius:4px;font-size:0;line-height:0">&nbsp;</td></tr></table>')

        def _stat(valor, rotulo, cor):
            return (f'<span style="display:inline-block;text-align:center;margin-left:16px;vertical-align:middle">'
                    f'<span style="display:block;font-size:20px;font-weight:800;color:{cor};line-height:1">{valor}</span>'
                    f'<span style="display:block;font-size:9px;font-weight:700;color:#8A90A2;'
                    f'text-transform:uppercase;letter-spacing:.4px;margin-top:3px">{rotulo}</span></span>')

        cards.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 10px;background:#fff;border:1px solid #ECEEF4;border-radius:14px">'
            f'<tr><td style="padding:14px 18px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="vertical-align:middle">'
            f'<span style="display:inline-block;width:34px;height:34px;'
            f'background:linear-gradient(135deg,#5B2EE0,#B826C9);color:#fff;border-radius:10px;'
            f'text-align:center;line-height:34px;font-size:14px;font-weight:800;margin-right:11px;'
            f'vertical-align:middle">{inicial}</span>'
            f'<span style="font-size:14px;font-weight:700;color:#11131C;vertical-align:middle">{nome or "—"}</span>'
            f'{medal_html}</td>'
            f'<td align="right" style="vertical-align:middle;white-space:nowrap">'
            f'{_stat(concl, "Concluídas", "#0E9F6E")}{_stat(andam, "Em andamento", "#5B2EE0")}'
            f'</td></tr></table>{barra}</td></tr></table>')

    eyebrow = ('<p style="margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#A78BFA">Status por profissional</p>')
    return f'{eyebrow}<div style="margin:0 0 22px">{"".join(cards)}</div>'


def montar_alerta_sobrecarga(cur, time_filter: str = None) -> str:
    """Bloco de sobrecarga do resumo diário: quem está com carga alta ou
    atolado agora. Mesma conta do painel do Kanban (calcular_carga_equipe) --
    a sinalização segue o gestor mesmo quando ele não abre o sistema.
    Retorna '' quando não há ninguém sinalizado."""
    dados = calcular_carga_equipe(cur, time_filter=time_filter)
    sinalizados = [p for p in dados["equipe"] if p["nivel"] in ("pesado", "atolado")]
    if not sinalizados:
        return ""
    livres = [p["nome"].split(" ")[0] for p in dados["equipe"] if p["tarefas"] == 0]

    linhas = []
    for p in sinalizados:
        cor = CARGA_NIVEIS[p["nivel"]]["cor"]
        detalhe = f'{p["tarefas"]} na fila · peso {p["peso_total"]}'
        if p["atrasadas"]:
            detalhe += f' · {p["atrasadas"]} atrasada' + ('s' if p["atrasadas"] > 1 else '')
        if p["indisponivel"]:
            detalhe += f' · {p["indisponivel"]}'
        largura = min(100, int(round(p["pontos"] / max(1, p["limite_atolado"]) * 100)))
        linhas.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 8px;background:#fff;border:1px solid #ECEEF4;border-left:3px solid {cor};border-radius:12px">'
            f'<tr><td style="padding:12px 16px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="vertical-align:middle">'
            f'<span style="font-size:14px;font-weight:700;color:#11131C">{p["nome"]}</span>&nbsp;'
            f'<span style="font-size:10px;font-weight:800;color:{cor};background:{cor}18;'
            f'padding:2px 8px;border-radius:20px;white-space:nowrap">{CARGA_NIVEIS[p["nivel"]]["rotulo"].upper()}</span>'
            f'<br><span style="font-size:11px;color:#8A90A2">{detalhe}</span></td>'
            f'<td align="right" style="vertical-align:middle;white-space:nowrap">'
            f'<span style="font-size:20px;font-weight:800;color:{cor};line-height:1">{p["pontos"]:g}</span>'
            f'<span style="font-size:9px;font-weight:700;color:#8A90A2;letter-spacing:.4px">&nbsp;PTS</span>'
            f'</td></tr></table>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px"><tr>'
            f'<td style="height:6px;background:{cor};font-size:0;line-height:0;width:{largura}%;border-radius:4px">&nbsp;</td>'
            f'<td style="height:6px;background:#EEF0F6;font-size:0;line-height:0;border-radius:4px">&nbsp;</td>'
            f'</tr></table></td></tr></table>')

    rodape = ''
    if livres:
        rodape = (f'<p style="margin:2px 0 0;font-size:11px;color:#8A90A2">'
                  f'Sem fila hoje: {", ".join(livres)} — dá pra redistribuir.</p>')
    eyebrow = ('<p style="margin:0 0 12px;font-size:11px;font-weight:800;letter-spacing:1.4px;'
               'text-transform:uppercase;color:#EF4444">Sinalização de carga</p>')
    return f'{eyebrow}<div style="margin:0 0 22px">{"".join(linhas)}{rodape}</div>'


def montar_resumo_diario(cur, inicio, fim, time_filter: str = None):
    """Monta (html, total) com o status geral do dia: painel-resumo (KPIs) +
    status por profissional (concluídas hoje e em andamento agora). `total` é o
    nº de alterações do período (usado como gate de envio); `time_filter`
    restringe ao time do dono da tarefa."""
    q = """SELECT COALESCE(NULLIF(projeto_nome,''),'Sem projeto') AS proj,
                  tarefa_id, tarefa_desc, autor_nome, acao, campo,
                  valor_antigo, valor_novo, criado_em
           FROM tarefa_historico
           WHERE criado_em >= %s AND criado_em < %s"""
    params = [inicio, fim]
    if time_filter:
        q += " AND time_tarefa = %s"
        params.append(time_filter)
    q += " ORDER BY proj, tarefa_id, criado_em"
    cur.execute(q, params)
    rows = cur.fetchall()

    total = len(rows)
    # Tarefas concluídas hoje (status → 'concluido'), para o painel-resumo geral.
    concluidas_hoje = len({tid for _proj, tid, _d, _a, acao, campo, _ant, novo, _q in rows
                           if acao == "alterou" and campo == "status" and novo == "concluido"})

    # Painel-resumo geral (KPIs) no topo do corpo
    hoje = inicio.date() if hasattr(inicio, "date") else inicio
    try:
        kpis = montar_kpis(cur, hoje, time_filter, concluidas_hoje)
    except Exception as e:
        print(f"[resumo-diario] KPIs indisponíveis: {e}")
        kpis = ""

    # Status por profissional: quantas cada um concluiu e quantas estão em andamento
    try:
        por_pessoa = montar_resumo_por_pessoa(cur, inicio, fim, time_filter)
    except Exception as e:
        print(f"[resumo-diario] Status por profissional indisponível: {e}")
        por_pessoa = ""

    if not por_pessoa:
        por_pessoa = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#FBFBFE;border:1px solid #ECEEF4;border-radius:14px">'
            '<tr><td align="center" style="padding:30px 20px">'
            '<div style="font-size:26px;margin-bottom:6px">🌙</div>'
            '<p style="color:#6B7280;font-size:14px;font-weight:600;margin:0">Nenhuma atividade por profissional hoje</p>'
            '<p style="color:#AEB3C2;font-size:12px;margin:4px 0 0">Dia tranquilo por aqui.</p></td></tr></table>')

    # Sinalização de sobrecarga (quem está atolado agora) -- entra antes do
    # status por profissional; some sozinha quando não há ninguém sinalizado.
    try:
        sobrecarga = montar_alerta_sobrecarga(cur, time_filter)
    except Exception as e:
        print(f"[resumo-diario] Sinalização de carga indisponível: {e}")
        sobrecarga = ""

    corpo = kpis + sobrecarga + por_pessoa
    return corpo, total


# ── Alerta pessoal de fim de expediente (pendências) ──────────────────────────
def montar_pendencias_funcionario(cur, usuario_id: int, hoje) -> dict:
    """Tarefas pendentes de UM funcionário: abertas, em andamento e atrasadas
    (prazo já vencido, independente do status). Usado no alerta de fim de
    expediente -- e-mail + notificação pessoal -- pra quem esquece de
    atualizar a tarefa antes do dia fechar."""
    cur.execute("""
        SELECT id, descricao, cliente, status, data_prazo
        FROM tarefas
        WHERE usuario_id = %s AND status <> 'concluido'
        ORDER BY CASE WHEN data_prazo IS NOT NULL AND data_prazo < %s THEN 0 ELSE 1 END,
                 data_prazo NULLS LAST, criado_em
    """, (usuario_id, hoje))
    tarefas = cur.fetchall()
    n_abertas = sum(1 for t in tarefas if t[3] == 'aberto')
    n_andamento = sum(1 for t in tarefas if t[3] == 'em_andamento')
    n_atrasadas = sum(1 for t in tarefas if t[4] and t[4] < hoje)
    return {"total": len(tarefas), "tarefas": tarefas,
            "n_abertas": n_abertas, "n_andamento": n_andamento, "n_atrasadas": n_atrasadas}


def _corpo_email_pendencias(dados: dict, hoje, primeiro_nome: str, system_url: str) -> str:
    """Corpo do e-mail pessoal de fim de expediente: saudação, 3 cards (abertas/
    andamento/atrasadas, mesmo estilo de montar_kpis), botão pro quadro do
    funcionário e a lista das tarefas pendentes -- cada uma como um cartão
    (ícone + descrição + selo), no mesmo padrão visual do painel de detalhe
    do calendário em static/index.html."""
    import html as _html

    saudacao = (f'<p style="margin:0 0 20px;font-size:14px;color:#5E647A;line-height:1.6">'
                f'Oi, <strong style="color:#0B0D1F">{_html.escape(primeiro_nome)}</strong> — antes de fechar o dia, '
                f'dá uma olhada no que ainda está pendente no Faiston OPS:</p>')

    def card(emoji, num, rotulo, cor, chip):
        return (f'<td width="32%" valign="top" style="padding:0">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:#FBFBFE;border:1px solid #ECEEF4;border-top:3px solid {cor};border-radius:13px">'
                f'<tr><td align="center" style="padding:16px 8px 14px">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 8px">'
                f'<tr><td style="width:34px;height:34px;background:{chip};border-radius:10px;text-align:center;'
                f'vertical-align:middle;font-size:16px;line-height:34px">{emoji}</td></tr></table>'
                f'<div style="font-size:28px;font-weight:800;color:{cor};line-height:1;letter-spacing:-1px">{num}</div>'
                f'<div style="font-size:10px;color:#8A90A2;text-transform:uppercase;letter-spacing:.6px;'
                f'font-weight:700;margin-top:5px">{rotulo}</div></td></tr></table></td>')

    cor_atraso = "#EF4444" if dados["n_atrasadas"] else "#9AA0AE"
    chip_atraso = "#FDECEC" if dados["n_atrasadas"] else "#F1F2F6"
    cards = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px"><tr>'
             + card("⚪", dados["n_abertas"], "Abertas", "#6B7280", "#F1F2F6")
             + '<td width="8"></td>'
             + card("🚀", dados["n_andamento"], "Em andamento", "#5B2EE0", "#EEE8FE")
             + '<td width="8"></td>'
             + card("⚠️", dados["n_atrasadas"], "Atrasadas", cor_atraso, chip_atraso)
             + '</tr></table>')

    botao = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px">'
             f'<tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">'
             f'<a href="{system_url}/funcionario" style="display:block;color:#ffffff;text-decoration:none;'
             f'padding:14px 24px;font-weight:700;font-size:14px;border-radius:12px">Ver minhas tarefas &nbsp;&rarr;</a>'
             f'</td></tr></table>')

    STATUS_UI = {
        "aberto": {"label": "Aberta", "cor": "#6B7280", "chip": "#F1F2F6"},
        "em_andamento": {"label": "Em andamento", "cor": "#5B2EE0", "chip": "#EEE8FE"},
    }
    linhas = []
    for _tid, desc, cliente, status, prazo in dados["tarefas"][:12]:
        atrasada = bool(prazo and prazo < hoje)
        ui = STATUS_UI.get(status, STATUS_UI["aberto"])
        selo_cor, selo_chip, selo_txt = (("#EF4444", "#FDECEC", "ATRASADA") if atrasada
                                          else (ui["cor"], ui["chip"], ui["label"]))
        icone = "⏰" if atrasada else ("🚀" if status == "em_andamento" else "⚪")
        prazo_txt = prazo.strftime("prazo %d/%m") if prazo else "sem prazo"
        desc_txt = _html.escape((desc or "")[:80])
        cliente_txt = _html.escape(cliente or "") or "—"
        linhas.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 8px;background:#FBFBFE;border:1px solid #ECEEF4;border-radius:12px">'
            f'<tr>'
            f'<td width="46" style="padding:12px 0 12px 12px">'
            f'<span style="display:inline-block;width:30px;height:30px;background:{selo_chip};border-radius:9px;'
            f'text-align:center;vertical-align:middle;font-size:14px;line-height:30px">{icone}</span></td>'
            f'<td style="padding:12px 8px">'
            f'<p style="margin:0;font-size:13px;font-weight:700;color:#0B0D1F;line-height:1.35">{desc_txt}</p>'
            f'<p style="margin:3px 0 0;font-size:11px;color:#9097AC">{cliente_txt} · {prazo_txt}</p></td>'
            f'<td align="right" style="padding:12px 12px 12px 0;white-space:nowrap;vertical-align:top">'
            f'<span style="display:inline-block;font-size:9.5px;font-weight:800;letter-spacing:.3px;'
            f'padding:4px 9px;border-radius:999px;background:{selo_chip};color:{selo_cor}">{selo_txt}</span></td>'
            f'</tr></table>')
    resto = dados["total"] - min(len(dados["tarefas"]), 12)
    if resto > 0:
        linhas.append(f'<p style="margin:2px 0 0;color:#AEB3C2;font-size:11px">e mais {resto} tarefa(s)…</p>')

    lista_label = ('<p style="margin:0 0 10px;font-size:11px;font-weight:800;letter-spacing:1px;'
                    'text-transform:uppercase;color:#A78BFA">Detalhe das pendências</p>')
    return saudacao + cards + botao + lista_label + "".join(linhas)


def enviar_alerta_pendencias(dia=None, system_url: str = "") -> dict:
    """Alerta pessoal de fim de expediente: e-mail + notificação in-app pros
    funcionários com tarefa aberta ou em andamento, pra não ficar coisa parada
    de um dia pro outro por esquecimento. Roda seg-sex, antes do resumo diário
    dos gestores -- mesmo padrão de gate: quem não tem pendência não recebe
    nada (nem e-mail, nem notificação)."""
    conn = get_db()
    if not conn:
        return {"sucesso": False, "erro": "Banco offline"}
    try:
        system_url = (system_url or _resolver_system_url()).rstrip("/")
        cur = conn.cursor()
        hoje = dia or _hoje_sp()
        cur.execute("""SELECT id, nome, COALESCE(email,'') FROM usuarios
                       WHERE ativo=TRUE AND perfil='funcionario'""")
        funcionarios = cur.fetchall()

        enviados, notificados = 0, 0
        for uid, nome, email in funcionarios:
            dados = montar_pendencias_funcionario(cur, uid, hoje)
            if dados["total"] == 0:
                continue
            msg = (f"⏰ Antes de fechar o dia: {dados['n_abertas']} aberta(s), "
                   f"{dados['n_andamento']} em andamento" +
                   (f", {dados['n_atrasadas']} atrasada(s)" if dados["n_atrasadas"] else "") +
                   " — dá uma olhada no Faiston OPS.")
            criar_notificacao(conn, "pendencias_fim_dia", msg, destinatario_id=uid)
            notificados += 1
            if email:
                primeiro_nome = (nome or "").split(" ")[0] or "você"
                corpo = _corpo_email_pendencias(dados, hoje, primeiro_nome, system_url)
                html = _shell_email("Antes de fechar o dia",
                                    f"{dados['total']} tarefa(s) pendente(s) — {primeiro_nome}", corpo)
                assunto = f"⏰ Faiston OPS — {dados['total']} tarefa(s) pendente(s) hoje"
                if _brevo_send(email, assunto, html):
                    enviados += 1
        conn.commit()
        cur.close(); conn.close()
        print(f"[alerta-pendencias] {hoje}: {notificados} notificado(s), {enviados} e-mail(s)")
        return {"sucesso": True, "notificados": notificados, "enviados": enviados, "dia": str(hoje)}
    except Exception as e:
        print(f"[alerta-pendencias] erro: {e}")
        return {"sucesso": False, "erro": str(e)}


def enviar_resumo_diario(dia=None) -> dict:
    """Gera e envia o resumo do dia. Gestores recebem o resumo do seu time;
    admins/diretores recebem o consolidado de todos os times."""
    from datetime import datetime, timedelta
    conn = get_db()
    if not conn:
        return {"sucesso": False, "erro": "Banco offline"}
    try:
        cur = conn.cursor()
        d = dia or _hoje_sp()
        inicio = datetime(d.year, d.month, d.day)
        fim = inicio + timedelta(days=1)
        data_label = inicio.strftime("%d/%m/%Y")

        cur.execute("""SELECT email, COALESCE(time,'Projetos'), perfil, nome FROM usuarios
                       WHERE ativo=TRUE AND perfil IN ('gestor','admin','diretor')
                       AND COALESCE(email,'') <> ''""")
        destinatarios = cur.fetchall()

        enviados, cache = 0, {}
        for email, time_u, perfil, nome in destinatarios:
            chave = "__all__" if perfil in ("admin", "diretor") else time_u
            if chave not in cache:
                cache[chave] = montar_resumo_diario(
                    cur, inicio, fim, None if chave == "__all__" else time_u)
            corpo, total = cache[chave]
            if total == 0:
                continue  # nada a reportar → não envia
            escopo = "todos os times" if chave == "__all__" else f"time {time_u}"
            html = _shell_email("Resumo do dia",
                                f"{data_label} · {total} alteração(ões) · {escopo}", corpo)
            if _brevo_send(email, f"🛰️ Faiston OPS — Resumo de {data_label}", html):
                enviados += 1
        cur.close(); conn.close()
        print(f"[resumo-diario] {data_label}: {enviados} e-mail(s) enviado(s)")
        return {"sucesso": True, "enviados": enviados, "dia": data_label}
    except Exception as e:
        print(f"[resumo-diario] erro: {e}")
        return {"sucesso": False, "erro": str(e)}

# --- AUTH ---
# ── Rate limiting de login (in-memory; processo único no Railway) ─────────────
import time as _time
from collections import deque as _deque
_LOGIN_FAILS: dict = {}          # chave (ip:.. / user:..) -> deque[timestamps de falha]
_LOGIN_WINDOW_S = 300            # janela deslizante de 5 minutos
_LOGIN_MAX_FAILS = 8             # falhas por janela antes de bloquear temporariamente

def _login_ip(request: Request) -> str:
    """IP real do cliente — respeita X-Forwarded-For atrás do proxy do Railway."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"

def _login_bloqueado(chaves) -> bool:
    agora = _time.time()
    for k in chaves:
        dq = _LOGIN_FAILS.get(k)
        if not dq:
            continue
        while dq and agora - dq[0] > _LOGIN_WINDOW_S:
            dq.popleft()
        if len(dq) >= _LOGIN_MAX_FAILS:
            return True
    return False

def _login_registrar_falha(chaves):
    agora = _time.time()
    for k in chaves:
        _LOGIN_FAILS.setdefault(k, _deque()).append(agora)

def _login_limpar(chaves):
    for k in chaves:
        _LOGIN_FAILS.pop(k, None)

@app.post("/api/login")
def login(req: LoginRequest, response: Response, request: Request):
    chaves = (f"ip:{_login_ip(request)}", f"user:{(req.usuario or '').strip().lower()}")
    if _login_bloqueado(chaves):
        raise HTTPException(status_code=429,
                            detail="Muitas tentativas de login. Aguarde alguns minutos e tente novamente.")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, perfil, COALESCE(primeiro_acesso, FALSE), COALESCE(time,'Projetos'), COALESCE(cargo,''), senha_hash FROM usuarios WHERE usuario=%s AND ativo=TRUE",
                    (req.usuario,))
        row = cur.fetchone()
        if not row or not senha_confere(req.senha, row[6]):
            cur.close(); conn.close()
            _login_registrar_falha(chaves)
            raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
        # Migração transparente: quem ainda estava no hash antigo tem a senha
        # reescrita em bcrypt neste login, sem precisar trocar de senha.
        if not row[6].startswith("$2"):
            cur.execute("UPDATE usuarios SET senha_hash=%s WHERE id=%s", (hash_senha(req.senha), row[0]))
        token = secrets.token_hex(32)
        cur.execute("""
            INSERT INTO sessoes (token, usuario_id, nome, perfil, time_usuario, pagina, cargo, expira_em)
            VALUES (%s, %s, %s, %s, %s, 'dashboard', %s, NOW() + INTERVAL '24 hours')
        """, (token, row[0], row[1], row[2], row[4], row[5]))
        # Registra o último acesso (data/hora do login bem-sucedido)
        cur.execute("UPDATE usuarios SET ultimo_acesso = NOW() WHERE id = %s", (row[0],))
        conn.commit(); cur.close(); conn.close()
        _login_limpar(chaves)  # login OK zera o contador de falhas
        response.set_cookie("faiston_token", token, httponly=True, samesite="lax", secure=True, max_age=86400)
        # Cookie de CSRF (double-submit) -- deliberadamente NÃO httponly, o JS
        # do front precisa ler o valor pra ecoar no header X-CSRF-Token em toda
        # requisição que muda estado. A proteção não depende de sigilo desse
        # valor, depende de um site de outra origem não conseguir LER o cookie
        # (same-origin policy) pra montar o header correspondente.
        response.set_cookie("csrf_token", secrets.token_hex(16), httponly=False, samesite="lax", secure=True, max_age=86400)
        return {"sucesso": True, "perfil": row[2], "cargo": row[5], "nome": row[1], "primeiro_acesso": bool(row[3])}
    except HTTPException: raise
    except Exception:
        raise HTTPException(status_code=500, detail="Erro ao processar login")

@app.post("/api/logout")
def logout(response: Response, faiston_token: str = Cookie(None)):
    if faiston_token:
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM sessoes WHERE token = %s", (faiston_token,))
                conn.commit(); cur.close(); conn.close()
            except Exception: pass
    response.delete_cookie("faiston_token")
    response.delete_cookie("csrf_token")
    return {"sucesso": True}

@app.post("/api/trocar-senha")
def trocar_senha(body: TrocarSenhaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    erro = _senha_fraca(body.nova_senha, sess.get("nome", ""))
    if erro: raise HTTPException(status_code=400, detail=erro)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET senha_hash=%s, primeiro_acesso=FALSE WHERE id=%s",
                    (hash_senha(body.nova_senha), sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Esqueci minha senha (2026-07-30) ──────────────────────────────────────
# Token de uso único, expira em 30 min, e o e-mail nunca confirma se o
# usuário existe (evita enumerar contas válidas por tentativa e erro).
_RESET_JANELA_S = 900          # 15 min
_RESET_MAX_PEDIDOS = 3         # pedidos por janela antes de segurar
_RESET_PEDIDOS: dict = {}

class EsqueciSenhaModel(BaseModel):
    email: str

class RedefinirSenhaModel(BaseModel):
    token: str
    nova_senha: str

def _reset_bloqueado(chave) -> bool:
    agora = _time.time()
    dq = _RESET_PEDIDOS.get(chave)
    if not dq: return False
    while dq and agora - dq[0] > _RESET_JANELA_S: dq.popleft()
    return len(dq) >= _RESET_MAX_PEDIDOS

def _reset_registrar_pedido(chave):
    _RESET_PEDIDOS.setdefault(chave, _deque()).append(_time.time())

def _gerar_token_redefinicao(cur, uid: int) -> str:
    """Token de definição/redefinição de senha, uso único, expira em 30min.
    Substitui qualquer token anterior ainda ativo pra esse usuário. Reaproveitado
    pelo "esqueci minha senha" e pelo email de boas-vindas de conta nova (sem
    commit -- quem chama decide quando commitar)."""
    cur.execute("DELETE FROM senha_reset_tokens WHERE usuario_id=%s", (uid,))
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cur.execute("""
        INSERT INTO senha_reset_tokens (usuario_id, token_hash, expira_em)
        VALUES (%s, %s, NOW() + INTERVAL '30 minutes')
    """, (uid, token_hash))
    return token

@app.post("/api/esqueci-senha")
def esqueci_senha(body: EsqueciSenhaModel, request: Request):
    resposta_generica = {"sucesso": True, "mensagem": "Se o email existir, um e-mail com instruções foi enviado."}
    chave_ip = f"reset:ip:{_login_ip(request)}"
    chave_email = f"reset:email:{(body.email or '').strip().lower()}"
    if _reset_bloqueado(chave_ip) or _reset_bloqueado(chave_email):
        # Mesma resposta genérica de sucesso -- não revela rate limit pra
        # quem está tentando enumerar/abusar, só não manda o e-mail de novo.
        return resposta_generica
    _reset_registrar_pedido(chave_ip)
    _reset_registrar_pedido(chave_email)
    conn = get_db()
    if not conn: return resposta_generica
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, email FROM usuarios WHERE LOWER(email)=LOWER(%s) AND ativo=TRUE", ((body.email or "").strip(),))
        row = cur.fetchone()
        if row and row[2]:
            uid, nome, email = row
            token = _gerar_token_redefinicao(cur, uid)
            conn.commit()
            system_url = _resolver_system_url(request)
            link = f"{system_url}/redefinir-senha?token={token}"
            corpo = f"""
                <p style="color:#3D4152;font-size:14.5px;margin:0 0 18px;line-height:1.6">Olá, {nome}. Recebemos um pedido pra redefinir sua senha no Faiston OPS.</p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
                  <tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
                    <a href="{link}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">Redefinir minha senha &nbsp;&rarr;</a>
                  </td></tr>
                </table>
                <p style="color:#8A8FA3;font-size:12.5px;margin:0;line-height:1.6">Esse link expira em <strong>30 minutos</strong> e só funciona uma vez. Se você não pediu essa troca, pode ignorar este e-mail — sua senha continua a mesma.</p>
            """
            _brevo_send(email, "Redefinir sua senha — Faiston OPS",
                        _shell_email("Redefinir senha", "Link expira em 30 minutos", corpo))
        cur.close(); conn.close()
    except Exception as e:
        print(f"[esqueci-senha] erro: {e}")
    return resposta_generica

@app.post("/api/redefinir-senha")
def redefinir_senha(body: RedefinirSenhaModel):
    if not body.token: raise HTTPException(status_code=400, detail="Link inválido ou expirado.")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        token_hash = hashlib.sha256(body.token.encode()).hexdigest()
        cur.execute("""
            SELECT id, usuario_id FROM senha_reset_tokens
            WHERE token_hash=%s AND usado_em IS NULL AND expira_em > NOW()
        """, (token_hash,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail="Link inválido ou expirado. Peça um novo.")
        rid, uid = row
        cur.execute("SELECT usuario FROM usuarios WHERE id=%s", (uid,))
        usuario_row = cur.fetchone()
        erro = _senha_fraca(body.nova_senha, usuario_row[0] if usuario_row else "")
        if erro:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail=erro)
        cur.execute("UPDATE usuarios SET senha_hash=%s, primeiro_acesso=FALSE WHERE id=%s",
                    (hash_senha(body.nova_senha), uid))
        # Uso único: marca gasto -- essa mesma consulta nunca mais bate no
        # WHERE usado_em IS NULL de cima, então o link não é reaproveitável.
        cur.execute("UPDATE senha_reset_tokens SET usado_em=NOW() WHERE id=%s", (rid,))
        # Derruba sessões ativas -- se a conta foi comprometida e por isso
        # pediu reset, a sessão de quem invadiu também precisa cair.
        cur.execute("DELETE FROM sessoes WHERE usuario_id=%s", (uid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/me")
def me(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    return sess

# Tutorial guiado do N2 (2026-07-30) -- flag por usuário, não por sessão,
# pra "primeiro login" valer entre dispositivos/navegadores diferentes.
@app.get("/api/tutorial-n2/status")
def tutorial_n2_status(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(tutorial_n2_visto, FALSE) FROM usuarios WHERE id=%s", (sess["id"],))
        row = cur.fetchone()
        cur.close(); conn.close()
        return {"visto": bool(row[0]) if row else False}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tutorial-n2/visto")
def tutorial_n2_marcar_visto(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET tutorial_n2_visto=TRUE WHERE id=%s", (sess["id"],))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# Tour do gestor (2026-10-07) -- mesmo padrão do tutorial do N2: abre sozinho
# no primeiro acesso ao /dashboard e pode ser revisto pelo menu.
@app.get("/api/tutorial-gestor/status")
def tutorial_gestor_status(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(tutorial_gestor_visto, FALSE) FROM usuarios WHERE id=%s", (sess["id"],))
        row = cur.fetchone()
        cur.close(); conn.close()
        return {"visto": bool(row[0]) if row else False}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tutorial-gestor/visto")
def tutorial_gestor_marcar_visto(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET tutorial_gestor_visto=TRUE WHERE id=%s", (sess["id"],))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- USUÁRIOS ---
def _area_atual(sess: dict) -> str:
    """Área de quem está logado lida do cadastro, não da cópia gravada na
    sessão no login: renomear a área (Admin → Áreas) ou mover a pessoa por
    outro caminho não atualiza `sessoes`, e aí o gestor via/gravava a equipe
    da área antiga até deslogar."""
    fallback = sess.get("time") or "Projetos"
    conn = get_db()
    if not conn: return fallback
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (sess["id"],))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else fallback
    finally:
        conn.close()

@app.get("/api/usuarios")
def listar_usuarios(escopo: str = "", faiston_token: str = Cookie(None)):
    """Admin vê todo mundo (Admin → Usuários). Gestor vê só a própria área.
    `escopo=minha_area` (tela Minha Equipe) restringe à área de quem está
    logado para qualquer perfil -- inclusive admin/dev, que antes viam a
    empresa inteira na "Minha Equipe"."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] == "admin" and escopo != "minha_area":
            cur.execute("SELECT id, usuario, nome, perfil, ativo, criado_em, COALESCE(email,''), COALESCE(time,'Projetos'), COALESCE(primeiro_acesso, FALSE), ultimo_acesso, COALESCE(cargo,'') FROM usuarios WHERE ativo=TRUE ORDER BY criado_em DESC")
        else:
            cur.execute("SELECT id, usuario, nome, perfil, ativo, criado_em, COALESCE(email,''), COALESCE(time,'Projetos'), COALESCE(primeiro_acesso, FALSE), ultimo_acesso, COALESCE(cargo,'') FROM usuarios WHERE ativo=TRUE AND COALESCE(time,'Projetos')=%s ORDER BY criado_em DESC", (_area_atual(sess),))
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "usuario": r[1], "nome": r[2], "perfil": r[3], "ativo": r[4], "criado_em": str(r[5]), "email": r[6], "time": r[7],
                 "primeiro_acesso": bool(r[8]), "ultimo_acesso": str(r[9])[:19] if r[9] else None, "cargo": r[10]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/funcionarios")
def listar_funcionarios(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] == "admin":
            cur.execute("SELECT id, nome, COALESCE(cargo,'') FROM usuarios WHERE perfil='funcionario' AND ativo=TRUE ORDER BY nome")
        else:
            cur.execute("SELECT id, nome, COALESCE(cargo,'') FROM usuarios WHERE perfil='funcionario' AND ativo=TRUE AND COALESCE(time,'Projetos')=%s ORDER BY nome", (sess.get("time","Projetos"),))
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1], "cargo": r[2]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/usuarios")
def criar_usuario(u: NovoUsuario, bg: BackgroundTasks, request: Request, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    is_gestor = sess["perfil"] in ("gestor", "demo")
    if is_gestor and u.perfil not in ("funcionario", "demo"):
        raise HTTPException(status_code=403, detail="Gestores só podem criar funcionários")
    if u.perfil not in PERFIL_VALIDOS: raise HTTPException(status_code=400, detail="Perfil inválido")
    if not (u.email or "").strip(): raise HTTPException(status_code=400, detail="Email é obrigatório — é por ele que a pessoa recebe o acesso.")
    time_val = _area_atual(sess) if is_gestor else (u.time if u.time in times_validos() else "Projetos")
    cargo_val = u.cargo if (u.perfil == "funcionario" and u.cargo in CARGO_VALIDOS) else ""
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Cargo e perfil habilitados por área (cadastro em /api/areas): o admin
        # liga em cada área só o que faz sentido nela.
        if u.perfil not in _area_perfis_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O perfil {u.perfil} não está habilitado na área {time_val}")
        if cargo_val and cargo_val not in _area_cargos_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O cargo {cargo_val} não está habilitado na área {time_val}")
        # Sem senha definida pelo admin (2026-08-10): a coluna senha_hash ainda
        # não aceita NULL, então preenche com uma senha aleatória que nunca é
        # exibida nem enviada -- a pessoa define a própria senha pelo link de
        # "definir senha" abaixo, reaproveitando o token do "esqueci minha senha".
        senha_temp = secrets.token_urlsafe(24)
        cur.execute("INSERT INTO usuarios (usuario, senha_hash, nome, perfil, email, primeiro_acesso, time, cargo) VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) RETURNING id",
                    (u.usuario, hash_senha(senha_temp), u.nome, u.perfil, u.email, time_val, cargo_val))
        new_id = cur.fetchone()[0]
        token = _gerar_token_redefinicao(cur, new_id)
        conn.commit(); cur.close(); conn.close()
        system_url = _resolver_system_url(request)
        link = f"{system_url}/redefinir-senha?token={token}"
        bg.add_task(enviar_email_boas_vindas, u.email, u.nome, link)
        return {"sucesso": True, "id": new_id, "email_enviado": True}
    except psycopg2.errors.UniqueViolation: raise HTTPException(status_code=400, detail="Usuário já existe")
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/usuarios/{uid}")
def atualizar_usuario(uid: int, u: AtualizarUsuario, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    is_gestor = sess["perfil"] in ("gestor", "demo")
    if is_gestor:
        conn2 = get_db()
        if not conn2: raise HTTPException(status_code=500, detail="Banco offline")
        cur2 = conn2.cursor()
        cur2.execute("SELECT COALESCE(time,'Projetos'), perfil FROM usuarios WHERE id=%s", (uid,))
        row = cur2.fetchone(); cur2.close(); conn2.close()
        if not row or row[0] != _area_atual(sess):
            raise HTTPException(status_code=403, detail="Acesso negado — usuário não pertence ao seu time")
        if row[1] in ("admin", "gestor", "dev"):
            raise HTTPException(status_code=403, detail="Não é possível editar admins ou gestores")
        if u.perfil not in ("funcionario", "demo"):
            raise HTTPException(status_code=403, detail="Gestores só podem definir perfil funcionário/demo")
    if u.senha:
        erro = _senha_fraca(u.senha)
        if erro: raise HTTPException(status_code=400, detail=erro)
    time_val = _area_atual(sess) if is_gestor else (u.time if u.time in times_validos() else "Projetos")
    cargo_val = u.cargo if (u.perfil == "funcionario" and u.cargo in CARGO_VALIDOS) else ""
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT perfil, COALESCE(cargo,''), COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (uid,))
        atual = cur.fetchone()
        if not atual: raise HTTPException(status_code=404, detail="Usuário não encontrado")
        # Só checa o que mudou: alguém cadastrado antes de a área restringir
        # cargo/perfil continua editável (mudar o nome não pode dar 400) -- mas
        # trocar o perfil, o cargo ou a área passa pela regra da área de destino.
        if (u.perfil, time_val) != (atual[0], atual[2]) and u.perfil not in _area_perfis_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O perfil {u.perfil} não está habilitado na área {time_val}")
        if cargo_val and (cargo_val, time_val) != (atual[1], atual[2]) \
                and cargo_val not in _area_cargos_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O cargo {cargo_val} não está habilitado na área {time_val}")
        if u.senha:
            cur.execute("UPDATE usuarios SET nome=%s, perfil=%s, ativo=%s, email=%s, senha_hash=%s, primeiro_acesso=TRUE, time=%s, cargo=%s WHERE id=%s",
                        (u.nome, u.perfil, u.ativo, u.email, hash_senha(u.senha), time_val, cargo_val, uid))
        else:
            cur.execute("UPDATE usuarios SET nome=%s, perfil=%s, ativo=%s, email=%s, time=%s, cargo=%s WHERE id=%s",
                        (u.nome, u.perfil, u.ativo, u.email, time_val, cargo_val, uid))
        # sessoes guarda uma cópia de nome/perfil/time/cargo tirada no login.
        # get_session lê do cadastro desde 2026-10-08, mas a cópia ainda
        # aparece na lista de "quem está online" -- mantém em dia.
        cur.execute("UPDATE sessoes SET nome=%s, perfil=%s, time_usuario=%s, cargo=%s WHERE usuario_id=%s",
                    (u.nome, u.perfil, time_val, cargo_val, uid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/usuarios/{uid}/reenviar-email")
def reenviar_email_acesso(uid: int, request: Request, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome, usuario, email, perfil, COALESCE(cargo,''), primeiro_acesso FROM usuarios WHERE id=%s AND ativo=TRUE", (uid,))
        row = cur.fetchone()
        if not row: cur.close(); conn.close(); raise HTTPException(status_code=404, detail="Usuário não encontrado")
        nome, usuario, email, perfil_u, cargo_u, primeiro_acesso = row
        if not email: cur.close(); conn.close(); raise HTTPException(status_code=400, detail="Este usuário não tem email cadastrado")
        if primeiro_acesso:
            # Nunca definiu a própria senha ainda -- reenvia o link de definição
            # (token novo), não o email antigo de "use a senha que já tem".
            token = _gerar_token_redefinicao(cur, uid)
            conn.commit(); cur.close(); conn.close()
            system_url = _resolver_system_url(request)
            link = f"{system_url}/redefinir-senha?token={token}"
            enviado = enviar_email_boas_vindas(email, nome, link)
        else:
            cur.close(); conn.close()
            enviado = enviar_email_acesso(email, nome, usuario, None, _perfil_guia(perfil_u, cargo_u), _resolver_system_url(request))
        if not enviado: raise HTTPException(status_code=500, detail="Falha ao enviar email — verifique as variáveis EMAIL_USER e EMAIL_APP_PASSWORD no servidor")
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/usuarios/{uid}")
def deletar_usuario(uid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    if sess["perfil"] in ("gestor", "demo"):
        conn2 = get_db()
        if not conn2: raise HTTPException(status_code=500, detail="Banco offline")
        cur2 = conn2.cursor()
        cur2.execute("SELECT COALESCE(time,'Projetos'), perfil FROM usuarios WHERE id=%s AND usuario != 'admin'", (uid,))
        row = cur2.fetchone(); cur2.close(); conn2.close()
        if not row or row[0] != _area_atual(sess) or row[1] in ("admin", "gestor", "dev"):
            raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM usuarios WHERE id=%s AND usuario != 'admin'", (uid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

BLOQUEIO_TIPOS_VALIDOS = ('ferias', 'afastamento', 'recorrente')

class BloqueioModel(BaseModel):
    tipo: str  # 'ferias' | 'afastamento' | 'recorrente'
    data_inicio: Optional[str] = None
    data_fim: Optional[str] = None
    dia_semana: Optional[int] = None  # 0=segunda .. 6=domingo (igual date.weekday())
    hora_inicio: Optional[str] = None
    hora_fim: Optional[str] = None
    descricao: str = ""

@app.get("/api/usuarios/{uid}/bloqueios")
def listar_bloqueios(uid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, tipo, data_inicio, data_fim, dia_semana, hora_inicio, hora_fim, descricao
            FROM funcionario_bloqueios WHERE usuario_id=%s ORDER BY data_inicio NULLS LAST, dia_semana NULLS LAST
        """, (uid,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{
            "id": r[0], "tipo": r[1],
            "data_inicio": str(r[2]) if r[2] else None, "data_fim": str(r[3]) if r[3] else None,
            "dia_semana": r[4], "hora_inicio": str(r[5])[:5] if r[5] else None,
            "hora_fim": str(r[6])[:5] if r[6] else None, "descricao": r[7],
        } for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/usuarios/{uid}/bloqueios")
def criar_bloqueio(uid: int, b: BloqueioModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    if b.tipo not in BLOQUEIO_TIPOS_VALIDOS: raise HTTPException(status_code=400, detail="Tipo de bloqueio inválido")
    if b.tipo in ("ferias", "afastamento") and not (b.data_inicio and b.data_fim):
        raise HTTPException(status_code=400, detail="Informe data de início e fim")
    if b.tipo == "recorrente" and b.dia_semana is None:
        raise HTTPException(status_code=400, detail="Informe o dia da semana")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM usuarios WHERE id=%s", (uid,))
        if not cur.fetchone(): raise HTTPException(status_code=404, detail="Funcionário não encontrado")
        cur.execute("""
            INSERT INTO funcionario_bloqueios (usuario_id, tipo, data_inicio, data_fim, dia_semana, hora_inicio, hora_fim, descricao)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (uid, b.tipo, b.data_inicio or None, b.data_fim or None, b.dia_semana,
              b.hora_inicio or None, b.hora_fim or None, b.descricao))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/bloqueios/{bid}")
def deletar_bloqueio(bid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM funcionario_bloqueios WHERE id=%s", (bid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bloqueios-ativos-hoje")
def bloqueios_ativos_hoje(faiston_token: str = Cookie(None)):
    """Mapa usuario_id -> bloqueio ativo agora (férias/afastamento vigente ou
    recorrência que cai no dia da semana de hoje) -- alimenta o badge nas
    listas de Bloqueios de Agenda (Minha Equipe / Gerenciar Usuários), pra
    o gestor ver quem está bloqueado sem precisar abrir pessoa por pessoa."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        hoje = date.today()
        cur.execute("""
            SELECT usuario_id, tipo, data_fim, dia_semana, hora_inicio, hora_fim, descricao
            FROM funcionario_bloqueios
            WHERE (tipo IN ('ferias','afastamento') AND data_inicio <= %s AND data_fim >= %s)
               OR (tipo = 'recorrente' AND dia_semana = %s)
        """, (hoje, hoje, hoje.weekday()))
        out = {}
        for uid, tipo, data_fim, dia_semana, hi, hf, descricao in cur.fetchall():
            atual = out.get(uid)
            if atual and atual["tipo"] in ("ferias", "afastamento"):
                continue  # férias/afastamento tem prioridade sobre recorrência no badge
            out[str(uid)] = {
                "tipo": tipo,
                "descricao": descricao or "",
                "data_fim": data_fim.isoformat() if data_fim else None,
                "hora_inicio": hi.strftime("%H:%M") if hi else None,
                "hora_fim": hf.strftime("%H:%M") if hf else None,
            }
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- CATÁLOGO DE PESOS ---
class NovoTipoAtividade(BaseModel):
    frente_id: int
    nome: str
    peso: int
    descricao: str = ""

class PesoUpdate(BaseModel):
    peso: int

class NovaFrente(BaseModel):
    area: str
    nome: str

class AreaModel(BaseModel):
    nome: str
    usa_projetos: bool = True
    # None = não mexer. Antes o default era True e o PUT de renomear/ligar
    # projetos reativava área desativada sem ninguém pedir.
    ativo: Optional[bool] = None
    # None = não mexer (o PUT de renomear/ligar projetos não manda estas duas).
    # Lista vazia é tratada como "todos", pra não deixar área que não aceita
    # ninguém -- o admin tira o que não quer, não esvazia.
    cargos: Optional[List[str]] = None
    perfis: Optional[List[str]] = None

def _normalizar_lista_area(valores, catalogo):
    if valores is None:
        return None
    filtrada = [v for v in catalogo if v in valores]
    return filtrada or list(catalogo)

def _cargos_da_area(nome, cargos):
    """Cargos do Service Desk só valem na área Service Desk (regra de
    CARGOS_SERVICE_DESK) -- em qualquer outra área são descartados, senão quem
    recebesse o cargo lá passava a enxergar o módulo do SD."""
    if cargos is None or nome == AREA_SERVICE_DESK:
        return cargos
    return [c for c in cargos if c not in CARGOS_SERVICE_DESK] or            [c for c in CARGO_VALIDOS if c not in CARGOS_SERVICE_DESK]

# Áreas cujo NOME está amarrado no código: 'Projetos' decide o Status Report
# (TIME_STATUS_REPORT) e é o default de quem não tem área; 'Service Desk' é como
# o módulo do SD acha a equipe. Renomear qualquer uma quebrava o acesso e o
# boot recriava a área com o nome original, vazia.
AREAS_DO_SISTEMA = (TIME_STATUS_REPORT, AREA_SERVICE_DESK)

def _usuarios_ativos_na_area(cur, nome):
    cur.execute("SELECT COUNT(*) FROM usuarios WHERE COALESCE(time,'Projetos')=%s AND ativo=TRUE", (nome,))
    return cur.fetchone()[0]

@app.get("/api/areas")
def listar_areas(todas: bool = False, faiston_token: str = Cookie(None)):
    """Leitura liberada pra qualquer logado: o front monta com isto todo select
    de time e descobre se a área trabalha por projeto. `todas=1` (só admin)
    inclui as inativas -- é o que a tela de cadastro precisa."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    incluir_inativas = todas and sess["perfil"] == "admin"
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT a.id, a.nome, a.usa_projetos, a.ativo,
                   (SELECT COUNT(*) FROM usuarios u
                     WHERE COALESCE(u.time,'Projetos') = a.nome AND u.ativo = TRUE),
                   (SELECT COUNT(*) FROM projetos p
                     WHERE COALESCE(p.time,'Projetos') = a.nome AND p.ativo = TRUE),
                   a.cargos, a.perfis
            FROM areas a
            {'' if incluir_inativas else 'WHERE a.ativo = TRUE'}
            ORDER BY a.nome
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {
            # Catálogo junto da lista: a tela monta as caixas de seleção com
            # ele, e assim não repete os nomes em JavaScript.
            "catalogo": {"cargos": list(CARGO_VALIDOS), "perfis": list(PERFIL_VALIDOS)},
            "areas": [{"id": r[0], "nome": r[1], "usa_projetos": r[2], "ativo": r[3],
                       "usuarios": r[4], "projetos": r[5],
                       "cargos": [c for c in CARGO_VALIDOS if r[6] is None or c in r[6]],
                       "perfis": [pf for pf in PERFIL_VALIDOS if r[7] is None or pf in r[7]]}
                      for r in rows],
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/areas")
def criar_area(a: AreaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    nome = (a.nome or "").strip()
    if not nome: raise HTTPException(status_code=400, detail="Informe o nome da área")
    if len(nome) > 50: raise HTTPException(status_code=400, detail="Nome da área: no máximo 50 caracteres")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        import json as _json
        cargos = _cargos_da_area(nome, _normalizar_lista_area(a.cargos, CARGO_VALIDOS) or list(CARGO_VALIDOS))
        perfis = _normalizar_lista_area(a.perfis, PERFIL_VALIDOS) or list(PERFIL_VALIDOS)
        cur = conn.cursor()
        cur.execute("INSERT INTO areas (nome, usa_projetos, cargos, perfis) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (nome) DO NOTHING RETURNING id",
                    (nome, a.usa_projetos, _json.dumps(cargos), _json.dumps(perfis)))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=400, detail="Já existe uma área com esse nome")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": row[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/areas/{aid}")
def atualizar_area(aid: int, a: AreaModel, faiston_token: str = Cookie(None)):
    """Renomear propaga pras tabelas que guardam o NOME da área (usuarios,
    clientes, projetos, frentes): `time` nunca foi chave estrangeira, então sem
    a cascata o rename deixaria todo mundo apontando pro nome antigo. Tudo na
    mesma transação."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    nome = (a.nome or "").strip()
    if not nome: raise HTTPException(status_code=400, detail="Informe o nome da área")
    if len(nome) > 50: raise HTTPException(status_code=400, detail="Nome da área: no máximo 50 caracteres")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome, usa_projetos, ativo FROM areas WHERE id=%s", (aid,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Área não encontrada")
        nome_antigo, usava_projetos, estava_ativa = row[0], bool(row[1]), bool(row[2])
        if nome != nome_antigo and nome_antigo in AREAS_DO_SISTEMA:
            raise HTTPException(status_code=400, detail=(
                f"A área {nome_antigo} é usada pelo sistema e não pode ser renomeada"))
        # Desativar pelo PUT passa pela mesma regra do DELETE.
        if estava_ativa and a.ativo is False:
            n_users = _usuarios_ativos_na_area(cur, nome_antigo)
            if n_users:
                raise HTTPException(status_code=400, detail=(
                    f"A área {nome_antigo} tem {n_users} usuário(s) ativo(s) — "
                    "mova essas pessoas para outra área antes de desativar"))
        # Desligar o projeto de uma área que ainda tem projeto ativo deixaria
        # tarefa apontando pra projeto que nenhuma tela mostra mais. Barra e diz
        # quantos são, pro admin arquivar antes.
        if usava_projetos and not a.usa_projetos:
            cur.execute("SELECT COUNT(*) FROM projetos WHERE COALESCE(time,'Projetos')=%s AND ativo=TRUE",
                        (nome_antigo,))
            n_proj = cur.fetchone()[0]
            if n_proj:
                raise HTTPException(status_code=400, detail=(
                    f"A área {nome_antigo} tem {n_proj} projeto(s) ativo(s) — "
                    "arquive antes de marcá-la como área sem projetos"))
        if nome != nome_antigo:
            cur.execute("SELECT 1 FROM areas WHERE nome=%s AND id<>%s", (nome, aid))
            if cur.fetchone(): raise HTTPException(status_code=400, detail="Já existe uma área com esse nome")
            for tabela, coluna in (("usuarios", "time"), ("clientes", "time"),
                                   ("projetos", "time"), ("frentes", "area")):
                cur.execute(f"UPDATE {tabela} SET {coluna}=%s WHERE {coluna}=%s", (nome, nome_antigo))
        import json as _json
        cargos = _cargos_da_area(nome, _normalizar_lista_area(a.cargos, CARGO_VALIDOS))
        perfis = _normalizar_lista_area(a.perfis, PERFIL_VALIDOS)
        cur.execute("""UPDATE areas SET nome=%s, usa_projetos=%s, ativo=COALESCE(%s, ativo),
                              cargos=COALESCE(%s, cargos), perfis=COALESCE(%s, perfis)
                        WHERE id=%s""",
                    (nome, a.usa_projetos, a.ativo,
                     _json.dumps(cargos) if cargos is not None else None,
                     _json.dumps(perfis) if perfis is not None else None, aid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/areas/{aid}")
def desativar_area(aid: int, faiston_token: str = Cookie(None)):
    """Desativa em vez de apagar: usuário, cliente e projeto guardam o NOME da
    área, então um DELETE de verdade deixaria esse dado apontando pro vazio.
    Área com gente dentro não desativa -- esses usuários sumiriam de todo filtro
    por time."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome FROM areas WHERE id=%s", (aid,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Área não encontrada")
        n_users = _usuarios_ativos_na_area(cur, row[0])
        if n_users:
            raise HTTPException(status_code=400, detail=(
                f"A área {row[0]} tem {n_users} usuário(s) ativo(s) — "
                "mova essas pessoas para outra área antes de desativar"))
        cur.execute("UPDATE areas SET ativo=FALSE WHERE id=%s", (aid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/frentes")
def listar_frentes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, area, nome FROM frentes WHERE ativo=TRUE ORDER BY area, nome")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "area": r[1], "nome": r[2]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/frentes")
def criar_frente(f: NovaFrente, faiston_token: str = Cookie(None)):
    """Cria uma nova frente (agrupador de tipos de atividade) dentro de uma
    área/time -- cada área tem seu próprio catálogo de peso, independente das
    outras (ex.: o peso do time de Desenvolvimento não tem nada a ver com o
    peso do time de Projetos)."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    if not f.nome.strip(): raise HTTPException(status_code=400, detail="Informe o nome da frente")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Valida contra o cadastro de áreas, não contra lista fixa -- é o que
        # permite montar o catálogo de atividades de uma área recém-criada.
        if f.area not in _times_validos_cur(cur):
            raise HTTPException(status_code=400, detail="Área inválida")
        cur.execute(
            "INSERT INTO frentes (area, nome) VALUES (%s,%s) ON CONFLICT (area, nome) DO NOTHING RETURNING id",
            (f.area, f.nome.strip())
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Já existe uma frente com esse nome nessa área")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": row[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/frentes/{fid}")
def desativar_frente(fid: int, faiston_token: str = Cookie(None)):
    """Desativa em vez de apagar -- mesmo padrão de desativar_tipo_atividade,
    pra tarefas antigas não perderem a referência."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE frentes SET ativo=FALSE WHERE id=%s", (fid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tipos-atividade")
def listar_tipos_atividade(area: str = "", frente_id: Optional[int] = None,
                           faiston_token: str = Cookie(None)):
    """Leitura liberada pra qualquer usuário logado: todo mundo precisa
    consultar a tabela de pesos na hora de abrir a tarefa."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cond, params = ["f.ativo = TRUE", "t.ativo = TRUE"], []
        if area:
            cond.append("f.area = %s"); params.append(area)
        if frente_id:
            cond.append("t.frente_id = %s"); params.append(frente_id)
        cur.execute(f"""
            SELECT t.id, t.frente_id, f.area, f.nome, t.nome, t.peso, t.descricao
            FROM tipos_atividade t JOIN frentes f ON t.frente_id = f.id
            WHERE {' AND '.join(cond)}
            ORDER BY f.area, f.nome, t.peso DESC, t.nome
        """, tuple(params))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "frente_id": r[1], "area": r[2], "frente": r[3],
                 "nome": r[4], "peso": r[5], "descricao": r[6]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/tipos-atividade/{tid}/peso")
def atualizar_peso_atividade(tid: int, p: PesoUpdate, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    if not 1 <= p.peso <= 4: raise HTTPException(status_code=400, detail="Peso deve ser de 1 a 4")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tipos_atividade SET peso=%s WHERE id=%s", (p.peso, tid))
        if cur.rowcount == 0: raise HTTPException(status_code=404, detail="Tipo de atividade não encontrado")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tipos-atividade")
def criar_tipo_atividade(t: NovoTipoAtividade, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    if not t.nome.strip(): raise HTTPException(status_code=400, detail="Informe o nome da atividade")
    if not 1 <= t.peso <= 4: raise HTTPException(status_code=400, detail="Peso deve ser de 1 a 4")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tipos_atividade (frente_id, nome, peso, descricao) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (frente_id, nome) DO NOTHING RETURNING id",
            (t.frente_id, t.nome.strip(), t.peso, t.descricao)
        )
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=400, detail="Já existe uma atividade com esse nome nessa frente")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": row[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/tipos-atividade/{tid}")
def desativar_tipo_atividade(tid: int, faiston_token: str = Cookie(None)):
    """Desativa em vez de apagar -- tarefas antigas continuam apontando pro
    tipo, então DELETE de verdade quebraria o histórico."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tipos_atividade SET ativo=FALSE WHERE id=%s", (tid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


# --- HISTÓRICO DE TAREFAS (resumo diário) ---
HIST_STATUS_LABEL = {"aberto": "Aberto", "em_andamento": "Em andamento",
                     "concluido": "Concluído", "revisao": "Em revisão"}
# Campos rastreados: (atributo, rótulo amigável). Ordem define exibição.
HIST_CAMPOS = [
    ("descricao",         "Descrição"),
    ("cliente",           "Cliente"),
    ("status",            "Status"),
    ("prioridade",        "Prioridade"),
    ("projeto_nome",      "Projeto"),
    ("data_prazo",        "Prazo"),
    ("hora_prazo",        "Horário do prazo"),
    ("data_agendamento",  "Agendamento"),
]

def _hist_fmt(campo: str, valor) -> str:
    """Formata um valor para exibição amigável no resumo."""
    if valor is None or valor == "":
        return "—"
    v = str(valor)
    if campo == "status":
        return HIST_STATUS_LABEL.get(v, v)
    if campo in ("data_prazo", "data_agendamento") and len(v) >= 10:
        d = v[:10]
        return f"{d[8:10]}/{d[5:7]}/{d[0:4]}"
    if campo == "hora_prazo":
        return v[:5]
    return v

def _snapshot_tarefa(cur, tid: int) -> dict:
    """Lê o estado atual de uma tarefa + nome do projeto/time do dono."""
    cur.execute("""SELECT t.descricao, t.cliente, t.status, t.prioridade,
                          COALESCE(p.nome,''), t.data_prazo, t.hora_prazo, t.data_agendamento,
                          t.projeto_id, COALESCE(u.time,'Projetos')
                   FROM tarefas t
                   LEFT JOIN projetos p ON p.id = t.projeto_id
                   LEFT JOIN usuarios u ON u.id = t.usuario_id
                   WHERE t.id = %s""", (tid,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"descricao": r[0], "cliente": r[1], "status": r[2], "prioridade": r[3],
            "projeto_nome": r[4], "data_prazo": str(r[5]) if r[5] else "",
            "hora_prazo": str(r[6])[:5] if r[6] else "", "data_agendamento": str(r[7]) if r[7] else "",
            "projeto_id": r[8], "time": r[9]}

def registrar_historico(conn, sess, tid: int, acao: str, mudancas: list, snap: dict):
    """Grava no histórico. `mudancas` = lista de (campo, antigo, novo).
    Usa SAVEPOINT para nunca abortar a transação principal."""
    if not mudancas:
        return
    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT sp_hist")
        for campo, antigo, novo in mudancas:
            cur.execute("""INSERT INTO tarefa_historico
                (tarefa_id, tarefa_desc, autor_id, autor_nome, acao, campo,
                 valor_antigo, valor_novo, projeto_id, projeto_nome, time_tarefa)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tid, snap.get("descricao", ""), sess["id"], sess["nome"], acao, campo,
                 (str(antigo) if antigo not in (None, "") else None),
                 (str(novo) if novo not in (None, "") else None),
                 snap.get("projeto_id"), snap.get("projeto_nome", ""),
                 snap.get("time", "Projetos")))
        cur.execute("RELEASE SAVEPOINT sp_hist")
    except Exception as e:
        try: cur.execute("ROLLBACK TO SAVEPOINT sp_hist")
        except Exception: pass
        print(f"[historico] falha ao registrar: {e}")
    finally:
        cur.close()

# --- TIMER DAS TAREFAS (no servidor) ---
# Teto de segundos por tarefa (10.000h) -- o mesmo MAX_SEGUNDOS do front, evita
# overflow do INTEGER com valor corrompido.
MAX_SEGUNDOS = 36_000_000
# Tolerância pra cliente antigo que devolve no PUT os segundos que leu: se o
# valor enviado bate com o total atual (± isso), não é edição manual de horas,
# e o timer que está rodando não é zerado.
_TOLERANCIA_ECO_SEGUNDOS = 120

def _sql_decorrido(prefixo: str = "") -> str:
    """Segundos inteiros desde que o timer foi iniciado (0 se pausado)."""
    ti = f"{prefixo}timer_inicio"
    return f"COALESCE(GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (NOW() - {ti}))))::int, 0)"

def _estado_timer(cur, tid: int) -> dict:
    """Estado da tarefa depois de uma ação de timer/status, pro front
    sincronizar sem precisar recarregar a lista inteira."""
    cur.execute(f"""SELECT status, segundos, timer_inicio IS NOT NULL, {_sql_decorrido()}, concluido_em
                    FROM tarefas WHERE id=%s""", (tid,))
    r = cur.fetchone()
    if not r:
        return {"id": tid}
    return {"id": tid, "status": r[0], "segundos": r[1] or 0, "timer_ativo": bool(r[2]),
            "timer_decorrido": r[3] if r[2] else 0,
            "concluido_em": str(r[4]) if r[4] else None}

def _tarefa_do_usuario_para_update(cur, tid: int, usuario_id: int):
    """Trava a linha da tarefa (FOR UPDATE) e devolve (status, segundos,
    timer_rodando, decorrido). 404 se a tarefa não existe ou não é do usuário --
    antes o UPDATE simplesmente não achava a linha e o endpoint respondia
    'sucesso', a tela mostrava a tarefa concluída e ela 'voltava' no reload."""
    cur.execute(f"""SELECT status, segundos, timer_inicio IS NOT NULL, {_sql_decorrido()}
                    FROM tarefas WHERE id=%s AND usuario_id=%s FOR UPDATE""", (tid, usuario_id))
    r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada, ou você não é o responsável por ela. Recarregue a página.")
    return r[0], r[1] or 0, bool(r[2]), r[3] or 0

def consolidar_timers():
    """Job de 1 em 1 minuto: passa o tempo dos timers rodando pra `segundos`
    (e anda o timer_inicio junto, então o total não muda). Mantém métricas,
    relatórios e o quadro do gestor em dia mesmo com a aba do funcionário
    fechada. Cada linha é recalculada com os próprios valores atuais, então
    uma pausa concorrente nunca conta tempo em dobro."""
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE tarefas
               SET segundos = LEAST(COALESCE(segundos, 0) + {_sql_decorrido()}, {MAX_SEGUNDOS}),
                   timer_inicio = timer_inicio + {_sql_decorrido()} * INTERVAL '1 second'
             WHERE timer_inicio IS NOT NULL AND timer_inicio <= NOW() - INTERVAL '1 second'
        """)
        conn.commit(); cur.close()
    except Exception as e:
        logger.error(f"[timer] falha ao consolidar timers: {e}")
    finally:
        conn.close()

# --- TAREFAS ---
@app.get("/api/tarefas")
def listar_tarefas(view: str = "", faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # As colunas/tabela que eram criadas aqui ("migração silenciosa") já
        # vêm do setup_banco. ALTER TABLE pega lock exclusivo na tabela tarefas
        # mesmo com IF NOT EXISTS; rodando a cada listagem (várias por minuto,
        # por usuário), qualquer transação mais lenta enfileirava o sistema
        # inteiro atrás dele -- era a causa do "o sistema trava e não faz mais nada".
        base_sel = f"""SELECT t.id, t.descricao, t.cliente, t.prioridade, t.status, t.segundos,
                             t.criado_em, u.nome, t.projeto_id, COALESCE(p.nome,'') AS projeto_nome,
                             t.data_prazo, t.data_agendamento, t.usuario_id, t.hora_prazo,
                             t.tipo_atividade_id, t.peso, t.natureza,
                             t.concluido_em, t.prazo_status, t.justificativa_atraso,
                             t.timer_inicio IS NOT NULL, {_sql_decorrido('t.')}
                      FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
                      LEFT JOIN projetos p ON p.id = t.projeto_id"""
        if view == "func":
            # Quadro pessoal (funcionario.html / embed do gestor): qualquer perfil vê
            # apenas as próprias tarefas + aquelas em que é colaborador (ajudando).
            cur.execute(base_sel + """ WHERE t.usuario_id = %s
                OR t.id IN (SELECT tarefa_id FROM tarefa_colaboradores WHERE usuario_id = %s)
                ORDER BY t.criado_em DESC""", (sess["id"], sess["id"]))
        elif sess["perfil"] == "admin":
            cur.execute(base_sel + " ORDER BY t.criado_em DESC")
        elif sess["perfil"] in ("gestor", "demo"):
            cur.execute(base_sel + " WHERE COALESCE(u.time,'Projetos')=%s ORDER BY t.criado_em DESC", (sess.get("time","Projetos"),))
        else:
            # Funcionário vê as próprias tarefas + aquelas em que é colaborador (ajudando)
            cur.execute(base_sel + """ WHERE t.usuario_id = %s
                OR t.id IN (SELECT tarefa_id FROM tarefa_colaboradores WHERE usuario_id = %s)
                ORDER BY t.criado_em DESC""", (sess["id"], sess["id"]))
        rows = cur.fetchall()

        # Colaboradores por tarefa
        ids = [r[0] for r in rows]
        colab_map = {}
        if ids:
            cur.execute("""SELECT tc.tarefa_id, u.id, u.nome
                           FROM tarefa_colaboradores tc JOIN usuarios u ON u.id = tc.usuario_id
                           WHERE tc.tarefa_id = ANY(%s) ORDER BY u.nome""", (ids,))
            for tid_, uid_, unome_ in cur.fetchall():
                colab_map.setdefault(tid_, []).append({"id": uid_, "nome": unome_})
        cur.close(); conn.close()
        return [{"id": r[0], "descricao": r[1], "cliente": r[2], "prioridade": r[3],
                 "status": r[4], "segundos": r[5], "criado_em": str(r[6]), "funcionario": r[7],
                 "projeto_id": r[8], "projeto_nome": r[9],
                 "data_prazo": str(r[10]) if r[10] else None,
                 "data_agendamento": str(r[11]) if r[11] else None,
                 "usuario_id": r[12],
                 "hora_prazo": str(r[13])[:5] if r[13] else None,
                "tipo_atividade_id": r[14], "peso": r[15], "natureza": r[16],
                 "concluido_em": str(r[17]) if r[17] else None,
                 "prazo_status": r[18], "justificativa_atraso": r[19],
                 "timer_ativo": bool(r[20]), "timer_decorrido": r[21] if r[20] else 0,
                 "sou_colaborador": (r[12] != sess["id"]) and any(c["id"] == sess["id"] for c in colab_map.get(r[0], [])),
                 "colaboradores": colab_map.get(r[0], [])} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _sync_colaboradores(cur, tarefa_id: int, owner_id: int, colaboradores) -> list:
    """Substitui a lista de colaboradores. Retorna IDs dos novos colaboradores adicionados."""
    cur.execute("SELECT usuario_id FROM tarefa_colaboradores WHERE tarefa_id = %s", (tarefa_id,))
    existing = {r[0] for r in cur.fetchall()}
    cur.execute("DELETE FROM tarefa_colaboradores WHERE tarefa_id = %s", (tarefa_id,))
    ids = [int(c) for c in (colaboradores or []) if int(c) != owner_id]
    novos = []
    for uid in dict.fromkeys(ids):  # remove duplicados preservando ordem
        cur.execute("SELECT id FROM usuarios WHERE id=%s AND ativo=TRUE", (uid,))
        if cur.fetchone():
            cur.execute("INSERT INTO tarefa_colaboradores (tarefa_id, usuario_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (tarefa_id, uid))
            if uid not in existing:
                novos.append(uid)
    return novos

@app.post("/api/tarefas")
def criar_tarefa(t: TarefaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Gestor/admin pode atribuir a outro funcionário via funcionario_id
        uid = sess["id"]
        if t.funcionario_id and sess["perfil"] in ("admin", "gestor", "demo"):
            cur.execute("SELECT id FROM usuarios WHERE id=%s AND ativo=TRUE", (t.funcionario_id,))
            if cur.fetchone():
                uid = t.funcionario_id
        # Analista pode atribuir tarefa a um assistente de Backoffice do mesmo
        # time (2026-08-17) -- só nessa direção, não pra qualquer funcionário.
        elif (t.funcionario_id and t.funcionario_id != sess["id"]
              and sess["perfil"] == "funcionario" and sess.get("cargo") == "analista"):
            cur.execute("""
                SELECT id FROM usuarios WHERE id=%s AND ativo=TRUE AND perfil='funcionario'
                  AND cargo='backoffice' AND COALESCE(time,'Projetos')=%s
            """, (t.funcionario_id, sess.get("time", "Projetos")))
            if cur.fetchone():
                uid = t.funcionario_id
        data_checar = t.data_agendamento or t.data_prazo
        bloqueio = _bloqueio_ativo(cur, uid, data_checar, t.hora_prazo)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"Funcionário indisponível nesta data: {bloqueio}")
        # Área sem projeto (cadastro em /api/areas): a tarefa é sempre demanda
        # avulsa. Olha o time do DONO da tarefa (uid), não o de quem cria --
        # gestor de outra área atribuindo tarefa cai na mesma regra.
        if t.projeto_id:
            cur.execute("SELECT COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (uid,))
            row_area = cur.fetchone()
            if row_area:
                _exigir_area_com_projeto(cur, row_area[0])
        # 2A: campo ainda opcional. Quando vier preenchido, o peso da régua é
        # copiado pra tarefa. A obrigatoriedade entra no 2B, junto com o campo
        # no modal -- senão o frontend antigo pararia de criar tarefa.
        if not t.tipo_atividade_id:
            raise HTTPException(status_code=400, detail="Informe o tipo de atividade")
        if not t.data_prazo:
            raise HTTPException(status_code=400, detail="Informe a previsão de conclusão")
        peso = None
        if t.tipo_atividade_id:
            cur.execute("SELECT peso FROM tipos_atividade WHERE id=%s AND ativo=TRUE", (t.tipo_atividade_id,))
            row_tipo = cur.fetchone()
            if not row_tipo:
                raise HTTPException(status_code=400, detail="Tipo de atividade inválido ou desativado")
            peso = row_tipo[0]
        natureza = t.natureza if t.natureza in ("programada", "urgente") else "programada"
        cur.execute(
                "INSERT INTO tarefas (usuario_id, descricao, cliente, prioridade, status, segundos, projeto_id, data_prazo, data_agendamento, hora_prazo, tipo_atividade_id, peso, natureza, timer_inicio) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN NOW() END) RETURNING id",
            (uid, t.descricao, t.cliente, t.prioridade, t.status, max(0, min(t.segundos or 0, MAX_SEGUNDOS)), t.projeto_id or None,
             t.data_prazo or None, t.data_agendamento or None, t.hora_prazo or None,
             t.tipo_atividade_id or None, peso, natureza,
             t.status == "em_andamento")   # já nasce em andamento: timer ligado
        )
        new_id = cur.fetchone()[0]
        novos_helpers = _sync_colaboradores(cur, new_id, uid, t.colaboradores or [])
        snap_new = _snapshot_tarefa(cur, new_id)
        registrar_historico(conn, sess, new_id, "criou",
                            [("tarefa", None, t.descricao)], snap_new)
        criar_notificacao(conn, "nova_tarefa", f"🆕 {sess['nome']} criou uma tarefa: {t.descricao[:50]} [{t.cliente}]", sess["id"])
        for hid in novos_helpers:
            criar_notificacao(conn, "ajudante_adicionado",
                f"🤝 {sess['nome']} adicionou você para ajudar em: {t.descricao[:50]} [{t.cliente}]",
                sess["id"], destinatario_id=hid)
        estado = _estado_timer(cur, new_id)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/tarefas/{tid}")
def atualizar_tarefa(tid: int, t: TarefaModel, faiston_token: str = Cookie(None)):
    """Atualiza SÓ os campos que vieram no corpo. Antes o UPDATE gravava todos:
    ações rápidas do quadro (concluir, arrastar, iniciar timer) mandam só
    descrição/cliente/status, e o resto ia como NULL -- a tarefa perdia o
    projeto e o horário do prazo e 'sumia' das visões por projeto."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        enviados = t.model_fields_set
        status_antigo, seg_antigo, rodando, decorrido = _tarefa_do_usuario_para_update(cur, tid, sess["id"])
        snap_old = _snapshot_tarefa(cur, tid)
        status_novo = t.status if "status" in enviados else status_antigo
        # Mesma regra da criação -- o UPDATE abaixo é escopado em usuario_id =
        # sess["id"], então o dono da tarefa é quem está editando.
        if t.projeto_id:
            _exigir_area_com_projeto(cur, sess.get("time", "Projetos"))
        peso_upd = None
        if t.tipo_atividade_id:
            cur.execute("SELECT peso FROM tipos_atividade WHERE id=%s AND ativo=TRUE", (t.tipo_atividade_id,))
            row_tipo = cur.fetchone()
            if not row_tipo:
                raise HTTPException(status_code=400, detail="Tipo de atividade inválido ou desativado")
            peso_upd = row_tipo[0]
        natureza_upd = t.natureza if t.natureza in ("programada", "urgente") else None
        # Só na transição para 'concluido' -- reeditar tarefa já concluída não
        # recalcula, senão o histórico mudaria sozinho. Comparação por DATA:
        # concluir no dia previsto conta como dentro do prazo.
        concluindo = status_novo == "concluido" and status_antigo != "concluido"
        iniciando = status_novo == "em_andamento" and status_antigo != "em_andamento"
        prazo_status = None
        justificativa = (t.justificativa_atraso or "").strip()
        if concluindo:
            prazo_ref = (t.data_prazo if "data_prazo" in enviados else None) or snap_old.get("data_prazo") or ""
            if prazo_ref:
                prazo_d = datetime.strptime(str(prazo_ref)[:10], "%Y-%m-%d").date()
                prazo_status = "dentro" if _hoje_sp() <= prazo_d else "fora"
            else:
                prazo_status = "sem_prazo"
            if prazo_status == "fora" and not justificativa:
                    raise HTTPException(status_code=400, detail="Tarefa concluída fora do prazo: informe a justificativa do atraso")

        sets, params = [], []
        for campo in ("descricao", "cliente", "prioridade", "status"):
            if campo in enviados:
                sets.append(f"{campo}=%s"); params.append(getattr(t, campo))
        for campo in ("projeto_id", "data_prazo", "data_agendamento", "hora_prazo"):
            if campo in enviados:
                sets.append(f"{campo}=%s"); params.append(getattr(t, campo) or None)
        if t.tipo_atividade_id:
            sets.append("tipo_atividade_id=%s"); params.append(t.tipo_atividade_id)
            sets.append("peso=%s"); params.append(peso_upd)
        if natureza_upd:
            sets.append("natureza=%s"); params.append(natureza_upd)

        # Timer: `segundos` só é gravado quando é edição manual de horas. Um
        # cliente que apenas devolve o valor que leu (quadro antigo, tela do
        # N2) não pode zerar o tempo que o timer contou nesse meio-tempo.
        edita_segundos = "segundos" in enviados
        if edita_segundos and rodando and abs((t.segundos or 0) - (seg_antigo + decorrido)) <= _TOLERANCIA_ECO_SEGUNDOS:
            edita_segundos = False
        if edita_segundos:
            sets.append("segundos=%s"); params.append(max(0, min(t.segundos or 0, MAX_SEGUNDOS)))
        if rodando and status_novo != "em_andamento":
            # Saiu de 'em andamento' (concluiu, voltou pra aberto): fecha o timer.
            if not edita_segundos:
                sets.append(f"segundos=LEAST(COALESCE(segundos,0) + {_sql_decorrido()}, {MAX_SEGUNDOS})")
            sets.append("timer_inicio=NULL")
        elif rodando and edita_segundos:
            sets.append("timer_inicio=NOW()")   # horas corrigidas à mão: conta a partir delas
        elif not rodando and iniciando:
            sets.append("timer_inicio=NOW()")   # entrou em 'em andamento': começa a contar
        sets.append("atualizado_em=NOW()")
        cur.execute(f"UPDATE tarefas SET {', '.join(sets)} WHERE id=%s", params + [tid])
        if concluindo:
            cur.execute("UPDATE tarefas SET concluido_em=NOW(), prazo_status=%s, justificativa_atraso=%s WHERE id=%s",
                        (prazo_status, justificativa, tid))
        # Registra no histórico cada campo que mudou (compara antes × depois)
        if snap_old:
            snap_new = _snapshot_tarefa(cur, tid)
            mudancas = []
            for campo, _label in HIST_CAMPOS:
                antes, depois = snap_old.get(campo, ""), snap_new.get(campo, "")
                if str(antes or "") != str(depois or ""):
                    mudancas.append((campo, antes, depois))
            registrar_historico(conn, sess, tid, "editou", mudancas, snap_new)
        # Só o dono atualiza colaboradores, e apenas quando a lista é enviada explicitamente
        novos_helpers = []
        if t.colaboradores is not None:
            novos_helpers = _sync_colaboradores(cur, tid, sess["id"], t.colaboradores)
        if concluindo:
            criar_notificacao(conn, "tarefa_concluida", f"✅ {sess['nome']} concluiu: {t.descricao[:50]} [{t.cliente}]", sess["id"])
        elif iniciando:
            criar_notificacao(conn, "tarefa_iniciada", f"▶️ {sess['nome']} iniciou: {t.descricao[:50]} [{t.cliente}]", sess["id"])
        for hid in novos_helpers:
            criar_notificacao(conn, "ajudante_adicionado",
                f"🤝 {sess['nome']} adicionou você para ajudar em: {t.descricao[:50]} [{t.cliente}]",
                sess["id"], destinatario_id=hid)
        estado = _estado_timer(cur, tid)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tarefas/{tid}/timer/iniciar")
def iniciar_timer_tarefa(tid: int, faiston_token: str = Cookie(None)):
    """Liga o timer desta tarefa sem mexer nos timers das outras (várias
    tarefas podem contar tempo ao mesmo tempo). Idempotente: clicar duas
    vezes, ou repetir a chamada depois de uma falha de rede, não reinicia
    a contagem."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        status_antigo, _seg, _rodando, _dec = _tarefa_do_usuario_para_update(cur, tid, sess["id"])
        if status_antigo == "concluido":
            raise HTTPException(status_code=400, detail="Tarefa já concluída. Reabra a tarefa antes de iniciar o timer.")
        snap_old = _snapshot_tarefa(cur, tid)
        cur.execute("""UPDATE tarefas SET timer_inicio = COALESCE(timer_inicio, NOW()),
                              status='em_andamento', atualizado_em=NOW() WHERE id=%s""", (tid,))
        if status_antigo != "em_andamento" and snap_old:
            snap_new = _snapshot_tarefa(cur, tid)
            registrar_historico(conn, sess, tid, "editou", [("status", status_antigo, "em_andamento")], snap_new)
            criar_notificacao(conn, "tarefa_iniciada",
                f"▶️ {sess['nome']} iniciou: {(snap_old.get('descricao') or '')[:50]} [{snap_old.get('cliente') or ''}]", sess["id"])
        estado = _estado_timer(cur, tid)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tarefas/{tid}/timer/pausar")
def pausar_timer_tarefa(tid: int, faiston_token: str = Cookie(None)):
    """Pausa o timer desta tarefa e soma a sessão em `segundos`. A tarefa
    continua 'em andamento'. Idempotente: pausar o que já está pausado é no-op."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        _tarefa_do_usuario_para_update(cur, tid, sess["id"])
        cur.execute(f"""UPDATE tarefas
                           SET segundos = LEAST(COALESCE(segundos,0) + {_sql_decorrido()}, {MAX_SEGUNDOS}),
                               timer_inicio = NULL, atualizado_em = NOW()
                         WHERE id=%s AND timer_inicio IS NOT NULL""", (tid,))
        estado = _estado_timer(cur, tid)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/tarefas/{tid}/segundos")
def atualizar_segundos(tid: int, body: AtualizarSegundos, faiston_token: str = Cookie(None)):
    """Legado: era o 'checkpoint' do timer que rodava no navegador (a cada 30s
    e ao fechar a aba). O quadro novo não usa mais -- fica pra aba antiga
    ainda aberta. Nunca diminui o tempo já contado, e se o timer está rodando
    no servidor ele continua a partir do valor gravado."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        seg = max(0, min(body.segundos or 0, MAX_SEGUNDOS))
        cur.execute(f"""UPDATE tarefas
                           SET segundos = GREATEST(%s, COALESCE(segundos,0) + {_sql_decorrido()}),
                               timer_inicio = CASE WHEN timer_inicio IS NULL THEN NULL ELSE NOW() END,
                               atualizado_em = NOW()
                         WHERE id=%s AND usuario_id=%s""", (seg, tid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/tarefas/{tid}")
def deletar_tarefa(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        snap = _snapshot_tarefa(cur, tid)
        if sess["perfil"] in ("admin", "gestor", "demo"):
            cur.execute("DELETE FROM tarefas WHERE id=%s", (tid,))
        else:
            cur.execute("DELETE FROM tarefas WHERE id=%s AND usuario_id=%s", (tid, sess["id"]))
        if cur.rowcount and snap:
            registrar_historico(conn, sess, tid, "excluiu",
                                [("tarefa", snap.get("descricao"), None)], snap)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/resumo-diario")
def preview_resumo_diario(enviar: int = 0, dia: str = "", faiston_token: str = Cookie(None)):
    """Pré-visualiza (HTML) ou dispara manualmente o resumo do dia. Admin/gestor.
    ?enviar=1 envia os e-mails · ?dia=YYYY-MM-DD escolhe a data (default hoje)."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor", "demo"):
        raise HTTPException(status_code=403, detail="Sem permissão")
    from datetime import datetime, timedelta
    try:
        d = datetime.strptime(dia, "%Y-%m-%d").date() if dia else _hoje_sp()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida (use YYYY-MM-DD)")
    if enviar == 1:
        if sess["perfil"] not in ("admin", "gestor", "diretor"):
            raise HTTPException(status_code=403, detail="Sem permissão para disparar o envio")
        return enviar_resumo_diario(d)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        inicio = datetime(d.year, d.month, d.day)
        fim = inicio + timedelta(days=1)
        # Gestor vê apenas o próprio time; admin/diretor vê tudo.
        tf = None if sess["perfil"] in ("admin", "diretor") else sess.get("time", "Projetos")
        corpo, total = montar_resumo_diario(cur, inicio, fim, tf)
        cur.close(); conn.close()
        escopo = "todos os times" if tf is None else f"time {tf}"
        html = _shell_email("Resumo do dia",
                            f"{d.strftime('%d/%m/%Y')} · {total} alteração(ões) · {escopo}", corpo)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/alerta-pendencias")
def preview_alerta_pendencias(enviar: int = 0, dia: str = "", usuario_id: int = 0,
                              request: Request = None, faiston_token: str = Cookie(None)):
    """Pré-visualiza ou dispara manualmente o alerta pessoal de fim de expediente.
    ?enviar=1 dispara de verdade pra todo mundo com pendência (só admin, porque
    é um disparo pra empresa toda, não escopado por time) · ?usuario_id=N mostra
    o HTML do e-mail de um funcionário específico · sem parâmetros, lista quem
    receberia e quantas pendências cada um tem hoje."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor", "demo"):
        raise HTTPException(status_code=403, detail="Sem permissão")
    from datetime import datetime
    try:
        d = datetime.strptime(dia, "%Y-%m-%d").date() if dia else _hoje_sp()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida (use YYYY-MM-DD)")
    if enviar == 1:
        if sess["perfil"] != "admin":
            raise HTTPException(status_code=403, detail="Só admin pode disparar o envio pra todo mundo")
        return enviar_alerta_pendencias(d, _resolver_system_url(request))
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if usuario_id:
            cur.execute("SELECT nome, COALESCE(time,'Projetos') FROM usuarios WHERE id=%s AND perfil='funcionario'", (usuario_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Funcionário não encontrado")
            nome, time_u = row
            if sess["perfil"] == "gestor" and time_u != sess.get("time", "Projetos"):
                raise HTTPException(status_code=403, detail="Usuário não pertence ao seu time")
            dados = montar_pendencias_funcionario(cur, usuario_id, d)
            cur.close(); conn.close()
            primeiro_nome = (nome or "").split(" ")[0] or "você"
            corpo = _corpo_email_pendencias(dados, d, primeiro_nome, _resolver_system_url(request))
            html = _shell_email("Antes de fechar o dia",
                                f"{dados['total']} tarefa(s) pendente(s) — {primeiro_nome}", corpo)
            return HTMLResponse(content=html)
        # Sem usuario_id: lista quem receberia e quantas pendências cada um tem.
        tf = None if sess["perfil"] in ("admin", "diretor") else sess.get("time", "Projetos")
        q = "SELECT id, nome FROM usuarios WHERE ativo=TRUE AND perfil='funcionario'"
        params = []
        if tf:
            q += " AND COALESCE(time,'Projetos') = %s"
            params.append(tf)
        cur.execute(q, params)
        resultado = []
        for uid, nome in cur.fetchall():
            dados = montar_pendencias_funcionario(cur, uid, d)
            if dados["total"] > 0:
                resultado.append({"usuario_id": uid, "nome": nome, "total": dados["total"],
                                   "abertas": dados["n_abertas"], "andamento": dados["n_andamento"],
                                   "atrasadas": dados["n_atrasadas"]})
        cur.close(); conn.close()
        return {"dia": str(d), "qtd_receberiam": len(resultado), "detalhe": resultado}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/online")
def usuarios_online(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT nome, perfil, pagina, last_seen
            FROM sessoes
            WHERE last_seen >= NOW() - INTERVAL '5 minutes'
              AND expira_em > NOW()
            ORDER BY last_seen DESC
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        # last_seen é gravado com NOW() do banco, que está em America/Sao_Paulo
        # (ver SET TIME ZONE em get_db). Usar datetime.now() local evita erro de ~3h.
        agora = datetime.now()
        online = []
        for r in rows:
            last = r[3].replace(tzinfo=None) if r[3].tzinfo else r[3]
            minutos = int((agora - last).total_seconds() / 60)
            online.append({
                "nome": r[0], "perfil": r[1],
                "page": r[2] or "—",
                "ultimo_acesso": f"há {minutos} min" if minutos > 0 else "agora"
            })
        return online
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/diagnostico")
def diagnostico_tecnico(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)

    versao = {
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7] or "desconhecido",
        "commit_completo": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "desconhecido"),
        "branch": os.environ.get("RAILWAY_GIT_BRANCH", "desconhecido"),
        "ambiente": os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local"),
        "mensagem_commit": os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE", "")[:200],
    }

    banco = {"ok": False}
    conn = get_db()
    if conn:
        try:
            t0 = datetime.now()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            contagens = {}
            for nome_tabela, sql in [
                ("usuarios", "SELECT COUNT(*) FROM usuarios WHERE ativo=TRUE"),
                ("status_atividades", "SELECT COUNT(*) FROM status_atividades"),
                ("tarefas", "SELECT COUNT(*) FROM tarefas"),
                ("sessoes_ativas", "SELECT COUNT(*) FROM sessoes WHERE expira_em > NOW()"),
            ]:
                try:
                    cur.execute(sql)
                    contagens[nome_tabela] = cur.fetchone()[0]
                except Exception:
                    contagens[nome_tabela] = None
            cur.close(); conn.close()
            banco = {
                "ok": True,
                "latencia_ms": round((datetime.now() - t0).total_seconds() * 1000, 1),
                "contagens": contagens,
            }
        except Exception as e:
            banco = {"ok": False, "erro": str(e)}
    else:
        banco = {"ok": False, "erro": "Não foi possível conectar ao banco"}

    logs_recentes = []
    try:
        log_path = Path("errors.log")
        if log_path.exists():
            linhas = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            logs_recentes = linhas[-200:]
    except Exception as e:
        logs_recentes = [f"Erro ao ler errors.log: {e}"]

    return {"versao": versao, "banco": banco, "logs_recentes": logs_recentes}

@app.get("/api/admin/atividades")
def atividades_recentes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(u.nome, 'Sistema'), COALESCE(u.perfil, '—'), n.tipo, n.mensagem, n.criado_em
            FROM notificacoes n
            LEFT JOIN usuarios u ON n.usuario_id = u.id
            WHERE n.criado_em >= NOW() - INTERVAL '24 hours'
            ORDER BY n.criado_em DESC
            LIMIT 50
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"nome": r[0], "perfil": r[1], "tipo": r[2], "mensagem": r[3],
                 "quando": r[4].strftime("%H:%M")} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/uso")
def uso_sistema(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.nome,
                   COUNT(t.id) as total,
                   SUM(CASE WHEN t.status='aberto' THEN 1 ELSE 0 END) as abertas,
                   SUM(CASE WHEN t.status='em_andamento' THEN 1 ELSE 0 END) as em_andamento,
                   SUM(CASE WHEN t.status='concluido' THEN 1 ELSE 0 END) as concluidas,
                   COALESCE(SUM(t.segundos), 0) as segundos_total
            FROM usuarios u
            LEFT JOIN tarefas t ON t.usuario_id = u.id
            WHERE u.perfil IN ('funcionario','gestor','demo') AND u.ativo = TRUE
            GROUP BY u.id, u.nome ORDER BY total DESC
        """)
        por_usuario = [{"nome": r[0], "total": int(r[1]), "abertas": int(r[2] or 0),
                        "em_andamento": int(r[3] or 0), "concluidas": int(r[4] or 0),
                        "horas": round(int(r[5])/3600, 1)} for r in cur.fetchall()]

        cur.execute("""
            SELECT TO_CHAR(criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM') as dia, COUNT(*) as total
            FROM notificacoes WHERE criado_em >= NOW() - INTERVAL '7 days'
            GROUP BY dia ORDER BY MIN(criado_em)
        """)
        atividade_diaria = [{"dia": r[0], "total": int(r[1])} for r in cur.fetchall()]

        cur.execute("""
            SELECT tipo, COUNT(*) as total FROM notificacoes
            WHERE criado_em >= NOW() - INTERVAL '30 days'
            GROUP BY tipo ORDER BY total DESC
        """)
        tipo_label = {"nova_tarefa": "Nova Tarefa", "tarefa_concluida": "Concluída",
                      "tarefa_iniciada": "Iniciada", "ia_insight": "IA Insight"}
        tipos = [{"tipo": tipo_label.get(r[0], r[0]), "total": int(r[1])} for r in cur.fetchall()]

        cur.execute("""
            SELECT u.nome, COUNT(n.id) as notas
            FROM usuarios u LEFT JOIN notas n ON n.usuario_id = u.id
            WHERE u.perfil IN ('funcionario','gestor','demo') AND u.ativo = TRUE
            GROUP BY u.id, u.nome ORDER BY notas DESC LIMIT 10
        """)
        notas = [{"nome": r[0], "notas": int(r[1])} for r in cur.fetchall()]

        cur.execute("""
            SELECT u.nome, COUNT(c.id) as carimbos
            FROM usuarios u LEFT JOIN carimbos c ON c.criado_por = u.id
            WHERE u.ativo = TRUE GROUP BY u.id, u.nome ORDER BY carimbos DESC LIMIT 10
        """)
        carimbos_uso = [{"nome": r[0], "carimbos": int(r[1])} for r in cur.fetchall()]

        cur.close(); conn.close()
        return {"por_usuario": por_usuario, "atividade_diaria": atividade_diaria,
                "tipos": tipos, "notas": notas, "carimbos": carimbos_uso}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/ping")
def ping_session(page: str = "", faiston_token: str = Cookie(None)):
    get_session(faiston_token, page)
    return {"ok": True}

@app.get("/api/admin/listar-carimbos-times")
def listar_carimbos_times(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.titulo, c.categoria, c.time_usuario, u.nome as criador
            FROM carimbos c LEFT JOIN usuarios u ON u.id = c.criado_por
            ORDER BY c.time_usuario, c.categoria, c.titulo
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "titulo": r[1], "categoria": r[2],
                 "time_usuario": r[3], "criador": r[4]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/admin/carimbo-time/{cid}")
def corrigir_time_carimbo(cid: int, body: dict, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE carimbos SET time_usuario=%s WHERE id=%s", (body.get("time_usuario"), cid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/horas-corrompidas")
def listar_horas_corrompidas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        # Considera corrompido qualquer tarefa com mais de 30 dias contínuos (2592000s)
        cur.execute("""
            SELECT t.id, t.descricao, t.cliente, u.nome, t.segundos
            FROM tarefas t JOIN usuarios u ON u.id = t.usuario_id
            WHERE t.segundos > 2592000
            ORDER BY t.segundos DESC
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "descricao": r[1], "cliente": r[2], "funcionario": r[3],
                 "segundos": r[4], "horas_display": f"{r[4]//3600}h {(r[4]%3600)//60}m"} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/admin/corrigir-horas/{tid}")
def corrigir_horas_tarefa(tid: int, body: AtualizarSegundos, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        # Timer rodando continua contando a partir do valor corrigido.
        cur.execute("""UPDATE tarefas SET segundos=%s,
                              timer_inicio = CASE WHEN timer_inicio IS NULL THEN NULL ELSE NOW() END
                        WHERE id=%s""", (body.segundos, tid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/limpar-tarefas")
def limpar_todas_tarefas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Apenas admin pode limpar tarefas")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tarefas")
        deleted = cur.rowcount
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "removidas": deleted}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# Métricas do dashboard -- em app/metricas/router.py
from app.metricas.router import router as metricas_router
app.include_router(metricas_router)

# ── Carga de trabalho: quem está atolado ────────────────────────────
# O número de tarefas sozinho engana: dez acionamentos (peso 1) não pesam
# como quatro faturamentos (peso 4). Por isso a medida é a soma do peso das
# tarefas que a pessoa tem NA MÃO agora (aberto + em andamento), com as
# atrasadas e as que vencem já contando mais -- é isso que faz a pessoa
# "sentir" a carga. Tarefa concluída sai da conta: mede fila, não produção.
CARGA_PESO_PADRAO = 2          # tarefa sem peso carimbado (anterior à régua)
CARGA_MULT_ATRASADA = 1.5      # já passou do prazo
CARGA_MULT_VENCE_JA = 1.25     # vence hoje ou amanhã
CARGA_JANELA_VENCE_JA = 1      # dias à frente que contam como "vence já"
CARGA_LIMITES_PADRAO = {"moderado": 10, "pesado": 18, "atolado": 28, "atrasadas_atolado": 3}
CARGA_NIVEIS = {
    "tranquilo": {"rotulo": "Tranquilo",  "cor": "#0E9F6E", "icone": "ph-smiley"},
    "moderado":  {"rotulo": "Fluindo",    "cor": "#06D7E6", "icone": "ph-gauge"},
    "pesado":    {"rotulo": "Carga alta", "cor": "#F59E0B", "icone": "ph-warning"},
    "atolado":   {"rotulo": "Atolado",    "cor": "#EF4444", "icone": "ph-fire"},
}
CARGA_NIVEL_ORDEM = ["tranquilo", "moderado", "pesado", "atolado"]


def _carga_limites(cur, time_nome: str) -> dict:
    """Régua de pontos por nível. Cada time calibra a sua (assim como o peso
    das atividades já é por área); sem calibração, vale o padrão."""
    import json as _json
    limites = dict(CARGA_LIMITES_PADRAO)
    try:
        cur.execute("SELECT valor FROM configuracoes WHERE chave=%s",
                    (f"carga_limites_{time_nome or 'Projetos'}",))
        row = cur.fetchone()
        if row:
            salvo = _json.loads(row[0])
            for chave in limites:
                valor = salvo.get(chave)
                if isinstance(valor, (int, float)) and valor > 0:
                    limites[chave] = int(valor)
    except Exception:
        pass
    return limites


def _carga_nivel(pontos: float, atrasadas: int, limites: dict) -> str:
    """Atrasada demais também atola, mesmo que a soma dos pesos não assuste:
    três tarefas vencidas na mão já são um problema pro gestor olhar."""
    if pontos >= limites["atolado"] or atrasadas >= limites["atrasadas_atolado"]:
        return "atolado"
    if pontos >= limites["pesado"]:
        return "pesado"
    if pontos >= limites["moderado"]:
        return "moderado"
    return "tranquilo"


def calcular_carga_equipe(cur, time_filter: str = None, usuario_id: int = None) -> dict:
    """Fila aberta de cada pessoa, já classificada. `time_filter` restringe ao
    time (gestor vê só o seu); `usuario_id` restringe a uma pessoa (funcionário
    consultando a própria carga)."""
    hoje = _hoje_sp()
    limite_vence_ja = hoje + timedelta(days=CARGA_JANELA_VENCE_JA)

    q = """SELECT u.id, u.nome, COALESCE(u.time,'Projetos'), COALESCE(u.cargo,''), u.perfil,
                  t.id, t.status, t.peso, t.data_prazo, t.descricao, t.cliente
           FROM usuarios u
           LEFT JOIN tarefas t ON t.usuario_id = u.id AND t.status IN ('aberto','em_andamento')
           WHERE u.ativo = TRUE AND u.perfil <> 'demo'"""
    params = []
    if time_filter:
        q += " AND COALESCE(u.time,'Projetos') = %s"; params.append(time_filter)
    if usuario_id:
        q += " AND u.id = %s"; params.append(usuario_id)
    cur.execute(q, tuple(params))

    pessoas = {}
    for uid, nome, time_u, cargo, perfil, tid, status, peso, prazo, desc, cliente in cur.fetchall():
        p = pessoas.get(uid)
        if p is None:
            p = pessoas[uid] = {
                "usuario_id": uid, "nome": nome, "time": time_u, "cargo": cargo,
                "perfil": perfil, "tarefas": 0, "em_andamento": 0, "abertas": 0,
                "peso_total": 0, "pontos": 0.0, "atrasadas": 0, "vence_ja": 0,
                "destaques": [],
            }
        if tid is None:
            continue
        peso = int(peso or CARGA_PESO_PADRAO)
        atrasada = bool(prazo and prazo < hoje)
        vence_ja = bool(prazo and not atrasada and prazo <= limite_vence_ja)
        mult = CARGA_MULT_ATRASADA if atrasada else (CARGA_MULT_VENCE_JA if vence_ja else 1.0)
        p["tarefas"] += 1
        p["em_andamento" if status == "em_andamento" else "abertas"] += 1
        p["peso_total"] += peso
        p["pontos"] += peso * mult
        if atrasada: p["atrasadas"] += 1
        if vence_ja: p["vence_ja"] += 1
        p["destaques"].append({
            "id": tid, "descricao": desc, "cliente": cliente, "peso": peso,
            "status": status, "data_prazo": str(prazo) if prazo else None,
            "atrasada": atrasada, "vence_ja": vence_ja,
        })

    limites_por_time = {}
    equipe = []
    for p in pessoas.values():
        # Quem não tem nada na fila só aparece se for funcionário -- é útil pro
        # gestor ver quem está livre pra receber. Gestor/admin sem tarefa
        # nenhuma na mão não é "pessoa da fila", é só quem olha o quadro.
        if p["tarefas"] == 0 and p["perfil"] != "funcionario":
            continue
        limites = limites_por_time.get(p["time"])
        if limites is None:
            limites = limites_por_time[p["time"]] = _carga_limites(cur, p["time"])
        p["pontos"] = round(p["pontos"], 1)
        p["nivel"] = _carga_nivel(p["pontos"], p["atrasadas"], limites)
        p["rotulo"] = CARGA_NIVEIS[p["nivel"]]["rotulo"]
        p["cor"] = CARGA_NIVEIS[p["nivel"]]["cor"]
        p["limite_atolado"] = limites["atolado"]
        p["peso_medio"] = round(p["peso_total"] / p["tarefas"], 1) if p["tarefas"] else 0
        p["indisponivel"] = _bloqueio_ativo(cur, p["usuario_id"], hoje.isoformat())
        # As mais pesadas primeiro (atrasada na frente): é o que o gestor
        # precisa ver pra decidir o que redistribuir.
        p["destaques"] = sorted(
            p["destaques"], key=lambda t: (not t["atrasada"], -t["peso"]))[:5]
        p.pop("perfil", None)
        equipe.append(p)

    equipe.sort(key=lambda p: (-p["pontos"], -p["tarefas"], (p["nome"] or "").lower()))
    sinalizados = [p for p in equipe if p["nivel"] in ("pesado", "atolado")]
    com_fila = [p for p in equipe if p["tarefas"] > 0]
    return {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "limites": _carga_limites(cur, time_filter or "Projetos"),
        "niveis": CARGA_NIVEIS,
        "equipe": equipe,
        "resumo": {
            "pessoas": len(equipe),
            "atolados": sum(1 for p in equipe if p["nivel"] == "atolado"),
            "pesados": sum(1 for p in equipe if p["nivel"] == "pesado"),
            "livres": sum(1 for p in equipe if p["tarefas"] == 0),
            "tarefas_abertas": sum(p["tarefas"] for p in equipe),
            "pontos_medios": round(sum(p["pontos"] for p in com_fila) / len(com_fila), 1) if com_fila else 0,
            "nomes_sinalizados": [p["nome"] for p in sinalizados],
        },
    }


@app.get("/api/carga-equipe")
def carga_equipe(faiston_token: str = Cookie(None)):
    """Sinalização de sobrecarga do kanban: quanto cada pessoa tem na fila e
    quem está atolado. Gestor vê o próprio time, admin/diretor vê tudo,
    funcionário vê só a própria carga."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] in ("admin", "diretor"):
            dados = calcular_carga_equipe(cur)
        elif sess["perfil"] in ("gestor", "demo"):
            dados = calcular_carga_equipe(cur, time_filter=sess.get("time", "Projetos"))
        else:
            dados = calcular_carga_equipe(cur, time_filter=sess.get("time", "Projetos"),
                                          usuario_id=sess["id"])
        cur.close(); conn.close()
        return dados
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


class CargaLimitesModel(BaseModel):
    moderado: int
    pesado: int
    atolado: int
    atrasadas_atolado: int = CARGA_LIMITES_PADRAO["atrasadas_atolado"]


@app.get("/api/config/carga-limites")
def get_carga_limites(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        limites = _carga_limites(cur, sess.get("time", "Projetos"))
        cur.close(); conn.close()
        return {"time": sess.get("time", "Projetos"), "limites": limites,
                "padrao": CARGA_LIMITES_PADRAO}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/config/carga-limites")
def salvar_carga_limites(body: CargaLimitesModel, faiston_token: str = Cookie(None)):
    """Calibra a régua de sobrecarga do time. Cada operação tem um ritmo --
    o que é 'atolado' no Backoffice não é o mesmo do N2."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"):
        raise HTTPException(status_code=403, detail="Sem permissão")
    if not (0 < body.moderado < body.pesado < body.atolado):
        raise HTTPException(status_code=400,
                            detail="Os limites precisam ser crescentes: moderado < pesado < atolado")
    if body.atrasadas_atolado < 1:
        raise HTTPException(status_code=400, detail="Atrasadas para atolado precisa ser pelo menos 1")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        import json as _json
        cur = conn.cursor()
        valor = _json.dumps({"moderado": body.moderado, "pesado": body.pesado,
                             "atolado": body.atolado,
                             "atrasadas_atolado": body.atrasadas_atolado})
        cur.execute("""INSERT INTO configuracoes (chave, valor, atualizado_em) VALUES (%s, %s, NOW())
                       ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor, atualizado_em=NOW()""",
                    (f"carga_limites_{sess.get('time','Projetos')}", valor))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/registrar-acao")
def registrar_acao(acao: AcaoBackoffice, faiston_token: str = Cookie(None)):
    return {"sucesso": True, "mensagem": "Ação registrada"}

@app.get("/api/health")
def health(): return {"status": "ok"}

@app.get("/api/exportar")
def exportar_excel(cliente: str = "", data_inicio: str = "", data_fim: str = "", faiston_token: str = Cookie(None)):
    from fastapi.responses import StreamingResponse
    import io
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        conditions = []
        params = []
        if cliente:
            conditions.append("t.cliente = %s")
            params.append(cliente)
        if data_inicio:
            conditions.append("t.criado_em >= %s")
            params.append(data_inicio + " 00:00:00")
        if data_fim:
            conditions.append("t.criado_em <= %s")
            params.append(data_fim + " 23:59:59")
        filtro = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"""SELECT u.nome, t.descricao, t.cliente, t.prioridade, t.status,
            t.segundos, t.criado_em, t.atualizado_em, COALESCE(t.peso,0),
            t.prazo_status, t.justificativa_atraso
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
            {filtro} ORDER BY t.criado_em DESC""", tuple(params))
        rows = cur.fetchall()
        cur.close(); conn.close()

        # Gerar XLSX com openpyxl
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Tarefas"

        # Estilos
        header_fill = PatternFill("solid", fgColor="4A00E0")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        alt_fill = PatternFill("solid", fgColor="F8FAFC")
        border = Border(bottom=Side(style='thin', color='E2E8F0'))
        center = Alignment(horizontal='center', vertical='center')

        # Cabeçalho -- data e hora de criação/atualização em colunas separadas
        # (em vez de um texto "AAAA-MM-DD HH:MM" só, mais fácil de ler/filtrar
        # fora do Excel), peso da atividade, e o desfecho do prazo (carimbado
        # só na conclusão) com o motivo do atraso quando ficou fora do prazo.
        headers = ["Funcionário", "Tarefa", "Cliente", "Prioridade", "Peso", "Status", "Horas", "Minutos", "Total (h)",
                   "Criado em", "Hora criação", "Atualizado em", "Hora atualização", "Prazo", "Motivo do atraso"]
        col_widths = [25, 40, 20, 12, 8, 15, 8, 8, 10, 14, 12, 14, 12, 16, 40]
        for col, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.row_dimensions[1].height = 30

        # Dados
        status_map = {"concluido": "Concluído", "em_andamento": "Em Andamento", "aberto": "Aberto"}
        prio_colors = {"Alta": "FFE4E6", "Media": "FEF3C7", "Baixa": "D1FAE5"}
        status_colors = {"concluido": "D1FAE5", "em_andamento": "CFFAFE", "aberto": "F1F5F9"}
        prazo_map = {"dentro": "Dentro do prazo", "fora": "Fora do prazo", "sem_prazo": "Sem prazo definido"}

        DATE_COLS = (10, 12)  # Criado em / Atualizado em
        TIME_COLS = (11, 13)  # Hora criação / Hora atualização
        for i, r in enumerate(rows, 2):
            h = r[5] // 3600
            m = (r[5] % 3600) // 60
            total_h = round(r[5] / 3600, 2)
            status_label = status_map.get(r[4], r[4])
            prazo_label = prazo_map.get(r[9], "")
            row_data = [r[0], r[1], r[2], r[3], r[8], status_label, h, m, total_h,
                r[6].date() if r[6] else None, r[6].time() if r[6] else None,
                r[7].date() if r[7] else None, r[7].time() if r[7] else None,
                prazo_label, r[10] or ""]
            fill = PatternFill("solid", fgColor="FFFFFF") if i % 2 == 0 else alt_fill
            for col, val in enumerate(row_data, 1):
                cell = ws.cell(row=i, column=col, value=val)
                cell.border = border
                cell.alignment = Alignment(vertical='center')
                if col in DATE_COLS:
                    cell.number_format = "DD/MM/YYYY"
                elif col in TIME_COLS:
                    cell.number_format = "HH:MM"
                # Cor por prioridade, status e prazo
                if col == 4 and r[3] in prio_colors:
                    cell.fill = PatternFill("solid", fgColor=prio_colors[r[3]])
                elif col == 6 and r[4] in status_colors:
                    cell.fill = PatternFill("solid", fgColor=status_colors[r[4]])
                elif col == 14 and r[9] == "fora":
                    cell.fill = PatternFill("solid", fgColor="FFE4E6")
                    cell.font = Font(color="C02234", bold=True)
                else:
                    cell.fill = fill
            ws.row_dimensions[i].height = 22

        # Totais
        total_row = len(rows) + 2
        ws.cell(row=total_row, column=1, value="TOTAL").font = Font(bold=True)
        ws.cell(row=total_row, column=7, value=sum(r[5]//3600 for r in rows)).font = Font(bold=True)
        ws.cell(row=total_row, column=9, value=round(sum(r[5] for r in rows)/3600, 2)).font = Font(bold=True)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        filename = f"faiston_tarefas{'_'+cliente if cliente else ''}{'_'+data_inicio if data_inicio else ''}.xlsx"
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/limpar-seed")
def limpar_seed(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Apenas admin")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tarefas WHERE usuario_id IN (SELECT id FROM usuarios WHERE usuario IN ('mariana','joao','carlos','fernanda','thiago'))")
        tarefas = cur.rowcount
        cur.execute("DELETE FROM tarefas WHERE descricao LIKE '%[TESTE]%'")
        tarefas += cur.rowcount
        cur.execute("DELETE FROM usuarios WHERE usuario IN ('mariana','joao','carlos','fernanda','thiago')")
        usuarios = cur.rowcount
        # Remove também projetos, clientes e histórico de teste (namespaced [TESTE])
        cur.execute("DELETE FROM tarefa_historico WHERE COALESCE(tarefa_desc,'') LIKE '%[TESTE]%' OR COALESCE(projeto_nome,'') LIKE '%[TESTE]%'")
        cur.execute("DELETE FROM projetos WHERE nome LIKE '%[TESTE]%'")
        cur.execute("DELETE FROM clientes WHERE nome LIKE '%[TESTE]%'")
        conn.commit()
        return {"ok": True, "tarefas_removidas": tarefas, "usuarios_removidos": usuarios}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/seed-dados")
def seed_dados(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Apenas admin")
    import random
    from datetime import datetime, timedelta
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Garante colunas usadas pelo seed (migração lazy normalmente roda no GET /api/tarefas)
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS projeto_id INTEGER REFERENCES projetos(id)")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS data_prazo DATE")
        cur.execute("ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS hora_prazo TIME")
        # Limpa qualquer dado de teste anterior (ordem respeita as FKs)
        cur.execute("DELETE FROM tarefas WHERE descricao LIKE '%[TESTE]%'")
        cur.execute("DELETE FROM usuarios WHERE usuario IN ('mariana','joao','carlos','fernanda','thiago')")
        cur.execute("DELETE FROM tarefa_historico WHERE COALESCE(tarefa_desc,'') LIKE '%[TESTE]%' OR COALESCE(projeto_nome,'') LIKE '%[TESTE]%'")
        cur.execute("DELETE FROM projetos WHERE nome LIKE '%[TESTE]%'")
        cur.execute("DELETE FROM clientes WHERE nome LIKE '%[TESTE]%'")

        funcionarios = [
            ("mariana","faiston123","Mariana Silva","funcionario"),
            ("joao","faiston123","João Henrique","funcionario"),
            ("carlos","faiston123","Carlos Eduardo","funcionario"),
            ("fernanda","faiston123","Fernanda Lima","funcionario"),
            ("thiago","faiston123","Thiago Rocha","funcionario"),
        ]
        ids = {}
        for usuario, senha, nome, perfil in funcionarios:
            cur.execute("""INSERT INTO usuarios (usuario, senha_hash, nome, perfil, primeiro_acesso)
                VALUES (%s,%s,%s,%s,FALSE) ON CONFLICT (usuario) DO UPDATE SET nome=%s RETURNING id""",
                (usuario, hash_senha(senha), nome, perfil, nome))
            ids[nome] = cur.fetchone()[0]

        clientes = ["NTT","Arcos Dourados","Zamp","Telcoweb","VIVO VITA"]
        prioridades_peso = ["Critica","Alta","Alta","Media","Media","Media","Baixa"]

        # Clientes e projetos de teste (namespaced [TESTE] → limpeza segura, não toca dado real)
        clientes_proj = {
            "[TESTE] NTT":            ["[TESTE] NOC 24x7", "[TESTE] Backbone MPLS"],
            "[TESTE] Zamp":           ["[TESTE] Rollout de Lojas"],
            "[TESTE] Arcos Dourados": ["[TESTE] Field Services"],
        }
        projetos_ref = []  # (projeto_id, projeto_nome, cliente_nome)
        for cli_nome, projs in clientes_proj.items():
            cur.execute("INSERT INTO clientes (nome) VALUES (%s) ON CONFLICT (nome) DO UPDATE SET nome=EXCLUDED.nome RETURNING id", (cli_nome,))
            cli_id = cur.fetchone()[0]
            for pnome in projs:
                cur.execute("INSERT INTO projetos (cliente_id, nome) VALUES (%s,%s) RETURNING id", (cli_id, pnome))
                projetos_ref.append((cur.fetchone()[0], pnome, cli_nome))
        descricoes = [
            "[TESTE] Abertura de chamado no NOC",
            "[TESTE] Acompanhamento de incidente crítico",
            "[TESTE] Configuração de switch core",
            "[TESTE] Monitoramento de links MPLS",
            "[TESTE] Troca de equipamento defeituoso",
            "[TESTE] Atualização de firmware",
            "[TESTE] Relatório de disponibilidade mensal",
            "[TESTE] Escalada para fornecedor",
            "[TESTE] Revisão de topologia de rede",
            "[TESTE] Acionamento de parceiro técnico",
            "[TESTE] Documentação de circuito",
            "[TESTE] Teste de failover",
            "[TESTE] Análise de log de erros",
            "[TESTE] Validação de SLA",
            "[TESTE] Suporte remoto ao cliente",
            "[TESTE] Instalação de CPE",
            "[TESTE] Diagnóstico de latência",
            "[TESTE] Follow-up de chamado crítico",
        ]
        now = datetime.now()
        total = 0
        criadas = []  # (tid, desc, cliente, projeto_id, projeto_nome, owner_nome)

        def add_tarefa(owner_nome, desc, status, segundos, prioridade, dias_atras, com_projeto=True):
            # ~75% das tarefas vão para um projeto de teste (pra exercitar o agrupamento
            # por projeto); o restante fica "Sem projeto" (cliente avulso).
            if com_projeto and projetos_ref and random.random() < 0.75:
                pid, pnome, cli = random.choice(projetos_ref)
            else:
                pid, pnome, cli = None, None, random.choice(clientes)
            ts = now - timedelta(days=dias_atras)
            cur.execute("""INSERT INTO tarefas (usuario_id,descricao,cliente,prioridade,status,segundos,projeto_id,criado_em,atualizado_em)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (ids[owner_nome], desc, cli, prioridade, status, segundos, pid, ts, now))
            tid = cur.fetchone()[0]
            criadas.append((tid, desc, cli, pid, pnome, owner_nome))
            return tid

        # Mariana — sobrecarregada, muitos abertos antigos (IA deve alertar)
        for i in range(12):
            add_tarefa("Mariana Silva", random.choice(descricoes), "aberto", 0,
                       random.choice(["Alta","Critica"]), random.randint(8, 20))
            total += 1
        # João — lento, tickets em andamento há muito tempo
        for i in range(8):
            add_tarefa("João Henrique", random.choice(descricoes),
                       random.choice(["em_andamento","em_andamento","aberto"]),
                       random.randint(600, 3600), random.choice(prioridades_peso), random.randint(5, 15))
            total += 1
        # Carlos — equilibrado, maioria concluído
        for i in range(10):
            status = random.choice(["concluido","concluido","concluido","em_andamento","aberto"])
            segundos = random.randint(1800, 7200) if status == "concluido" else random.randint(600, 3600)
            add_tarefa("Carlos Eduardo", random.choice(descricoes), status, segundos,
                       random.choice(prioridades_peso), random.randint(1, 7))
            total += 1
        # Fernanda — poucos tickets, bem resolvidos
        for i in range(5):
            add_tarefa("Fernanda Lima", random.choice(descricoes),
                       random.choice(["concluido","concluido","aberto"]), random.randint(1800, 5400),
                       "Media", random.randint(1, 5))
            total += 1
        # Thiago — vários críticos abertos
        for i in range(7):
            add_tarefa("Thiago Rocha", random.choice(descricoes),
                       random.choice(["aberto","aberto","em_andamento"]), 0,
                       random.choice(["Critica","Alta"]), random.randint(2, 10))
            total += 1

        # ── Histórico de "hoje" (NOW()) para popular o resumo diário ──
        def add_hist(tid, desc, pid, pnome, autor, acao, campo, antigo, novo):
            cur.execute("""INSERT INTO tarefa_historico
                (tarefa_id, tarefa_desc, autor_id, autor_nome, acao, campo, valor_antigo, valor_novo,
                 projeto_id, projeto_nome, time_tarefa, criado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Projetos', NOW())""",
                (tid, desc, ids.get(autor), autor, acao, campo, antigo, novo, pid, pnome))

        com_proj = [c for c in criadas if c[3]]
        random.shuffle(com_proj)
        hist = 0
        for c in com_proj[:6]:
            add_hist(c[0], c[1], c[3], c[4], c[5], "criou", "tarefa", None, c[1]); hist += 1
        for c in com_proj[6:10]:
            add_hist(c[0], c[1], c[3], c[4], c[5], "editou", "status", "aberto", "em_andamento"); hist += 1
            add_hist(c[0], c[1], c[3], c[4], c[5], "editou", "prioridade", "Media", "Alta"); hist += 1
        if com_proj:
            c = com_proj[0]
            add_hist(c[0], c[1], c[3], c[4], c[5], "editou", "data_prazo", None,
                     (now + timedelta(days=1)).strftime("%Y-%m-%d")); hist += 1
        if len(com_proj) > 10:
            c = com_proj[10]
            add_hist(c[0], c[1], c[3], c[4], c[5], "excluiu", "tarefa", c[1], None); hist += 1

        conn.commit(); cur.close(); conn.close()
        return {
            "sucesso": True,
            "tarefas_criadas": total,
            "projetos_criados": len(projetos_ref),
            "historico_criado": hist,
            "usuarios": {u[0]: "senha: faiston123" for u in funcionarios}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def root(): return FileResponse("static/login.html")

@app.get("/faiston-ops-mark.svg")
def faiston_ops_mark(): return FileResponse("static/faiston-ops-mark.svg", media_type="image/svg+xml")

@app.get("/redefinir-senha")
def redefinir_senha_page(): return FileResponse("static/redefinir-senha.html")

@app.get("/dashboard")
def dashboard(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    # Backoffice (cargo dentro de funcionario) entra aqui só pra ver o Kanban
    # de Cronograma do Status Report -- o próprio index.html restringe a
    # visão a essa única seção pra esse cargo (ver init() em index.html).
    eh_backoffice = sess and sess["perfil"] == "funcionario" and sess.get("cargo") == "backoffice"
    # Supervisor do Service Desk também entra, só com a visão do SD (o
    # index.html esconde o resto). get_session já traz o cargo do cadastro,
    # o mesmo que _redirect_login_ou_home usa -- por isso não há loop.
    eh_sup_sd = bool(sess) and sess["perfil"] == "funcionario" and sess.get("cargo") == "sd_supervisor"
    if not sess or (sess["perfil"] not in ("admin", "gestor", "demo", "diretor") and not eh_backoffice and not eh_sup_sd):
        return _redirect_login_ou_home(sess)
    return FileResponse("static/index.html", headers=_HTML_SEM_CACHE)

@app.get("/funcionario")
def funcionario(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    # Operador do Service Desk tem tela própria. Olha o cadastro, não só a
    # sessão: quem foi movido pro SD depois do login também é desviado.
    from app.service_desk.db import get_session as _sd_sessao
    from app.service_desk.router import CARGOS_SD
    sd = _sd_sessao(faiston_token)
    if sd and sd.get("perfil") == "funcionario" and sd.get("cargo") in CARGOS_SD:
        return RedirectResponse("/dashboard" if sd.get("cargo") == "sd_supervisor" else "/service-desk")
    return FileResponse("static/funcionario.html", headers=_HTML_SEM_CACHE)

@app.get("/n2")
def n2_page(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    if sess["perfil"] != "admin" and sess.get("cargo") != "n2":
        return _redirect_login_ou_home(sess)
    # A tela do N2 é o Status Report inteiro -- fora do time de Projetos não há
    # o que mostrar (ver _pode_ver_status_report). Aqui NÃO dá pra usar
    # _redirect_login_ou_home: pra cargo='n2' a home dele é o próprio /n2 e o
    # redirect entraria em loop; cai no board de tarefas.
    if not _pode_ver_status_report(sess):
        return RedirectResponse("/funcionario")
    return FileResponse("static/n2.html", headers=_HTML_SEM_CACHE)

@app.get("/admin")
def admin_page(): return RedirectResponse("/dashboard?go=admin")

@app.get("/devteam")
def devteam_page(): return RedirectResponse("/dashboard?go=areaDev")

app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js", StaticFiles(directory="static/js"), name="js")

# Comentários das tarefas -- em app/comentarios/router.py
from app.comentarios.router import router as comentarios_router
app.include_router(comentarios_router)

# Relatório por cliente -- em app/relatorio_cliente/router.py
from app.relatorio_cliente.router import router as relatorio_cliente_router
app.include_router(relatorio_cliente_router)

# Notificações -- em app/notificacoes/router.py
from app.notificacoes.router import router as notificacoes_router, criar_notificacao
app.include_router(notificacoes_router)

# Avisos globais (banner de novidade e e-mail do Assistente OPS) -- em app/avisos/router.py
from app.avisos.router import router as avisos_router
app.include_router(avisos_router)

# Novidades do sistema -- em app/novidades/router.py
from app.novidades.router import router as novidades_router
app.include_router(novidades_router)

# IA Insights -- em app/ia_insights/router.py
from app.ia_insights.router import router as ia_insights_router
app.include_router(ia_insights_router)

# Histórico completo -- em app/historico/router.py
from app.historico.router import router as historico_router
app.include_router(historico_router)

# Notas pessoais -- em app/notas/router.py
from app.notas.router import router as notas_router
app.include_router(notas_router)

# Carimbos (textos prontos por área) -- em app/carimbos/router.py
from app.carimbos.router import router as carimbos_router
app.include_router(carimbos_router)

# Cadastro de clientes -- em app/clientes/router.py
from app.clientes.router import router as clientes_router
app.include_router(clientes_router)


@app.get("/ajuda")
def ajuda_page(perfil: str = "", faiston_token: str = Cookie(None), request: Request = None):
    # Quem manda é a SESSÃO, não a URL: com cookie válido o ?perfil= é
    # sempre recalculado a partir do perfil+cargo do banco, mesmo que já
    # tenha vindo preenchido. Antes o parâmetro era aceito como veio, então
    # /ajuda?perfil=dev na barra do navegador abria o guia de qualquer
    # função — e o link do login mandava o perfil cru, fazendo um N2 cair
    # na aba de Analista.
    # O ?perfil= sozinho só vale sem sessão, que é o caso do e-mail de
    # primeiro acesso (link aberto antes de logar).
    # Preserva os demais parâmetros (ex.: ?destaque=assistente do aviso de
    # novidade) -- senão o redirect os descarta e o link do aviso perde o
    # scroll/abertura automática do widget.
    sess = get_session(faiston_token)
    if sess and sess.get("perfil"):
        destino = _perfil_guia(sess["perfil"], sess.get("cargo", ""),
                               sess.get("perfil_real", ""))
        if destino and destino != perfil:
            outros = "&".join(
                f"{k}={v}" for k, v in request.query_params.items() if k != "perfil"
            ) if request else ""
            return RedirectResponse(f"/ajuda?perfil={destino}" + (f"&{outros}" if outros else ""))
    return FileResponse("static/ajuda.html")

# Rotas antigas (páginas standalone duplicadas) → redirecionam para a SPA,
# abrindo o módulo correto via ?go=. As páginas antigas foram removidas.
@app.get("/gestao")
def gestao_page(): return RedirectResponse("/dashboard?go=gestao")

@app.get("/clientes")
def clientes_page(): return RedirectResponse("/dashboard?go=clientes")

# Módulo Financeiro saiu do menu (2026-09-28, não é mais usado). Dados e API
# continuam -- o Forecast/P&L da Gestão de Projetos segue funcionando.
@app.get("/financeiro")
def financeiro_geral_page(): return RedirectResponse("/dashboard")

@app.get("/forecast")
def forecast_page(): return RedirectResponse("/dashboard?go=forecast")

# Detalhe financeiro por cliente — página própria (mantida)
@app.get("/financeiro/{cid}")
def financeiro_page(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    return _redirect_login_ou_home(sess)

# Financeiro: projetos, lançamentos e importação de planilhas -- em app/financeiro/router.py
from app.financeiro.router import router as financeiro_router, _ensure_financeiro_tables
app.include_router(financeiro_router)


# Forecast / P&L (adaptação da planilha FORECAST2026) -- em app/forecast/router.py
from app.forecast.router import router as forecast_router, _ensure_forecast_tables
app.include_router(forecast_router)


# Sincronização de planilha online (OneDrive / SharePoint) dos projetos -- em app/planilha_online/router.py
from app.planilha_online.router import router as planilha_online_router
app.include_router(planilha_online_router)


# ─────────────────────────────────────────────
#  RELATÓRIO MENSAL
# ─────────────────────────────────────────────


def _garantir_tabelas_na_subida():
    """Roda os _ensure_* uma vez, na subida, com commit próprio. Só marca como
    garantido depois do commit -- se falhar, os requests continuam criando
    as tabelas sob demanda, como antes."""
    # As flags moram nos módulos que as leem (app/financeiro, app/forecast):
    # liga lá, não numa cópia aqui.
    import app.financeiro.router as _financeiro
    import app.forecast.router as _forecast
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        _ensure_financeiro_tables(cur); conn.commit()
        _financeiro._FINANCEIRO_TABELAS_OK = True
        _ensure_forecast_tables(cur); conn.commit()
        _forecast._FORECAST_TABELAS_OK = True
        cur.close()
    except Exception as e:
        print(f"Erro ao garantir tabelas financeiro/forecast: {e}")
    finally:
        conn.close()

_garantir_tabelas_na_subida()

# Inicia agendador (os jobs que vivem em app/ são importados aqui, antes do uso)
from app.relatorio_mensal.router import _job_relatorio_mensal
from app.loop.router import _loop_sync_job
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")
    _scheduler.add_job(_job_relatorio_mensal, "cron", day=1, hour=8, minute=0)
    # Resumo diário de alterações nas tarefas (seg-sex no fim do expediente).
    # Configurável: RESUMO_DIARIO_ENABLED (default "1"), RESUMO_DIARIO_HORA (default 18).
    if os.environ.get("RESUMO_DIARIO_ENABLED", "1") == "1":
        _resumo_hora = int(os.environ.get("RESUMO_DIARIO_HORA", "18"))
        _scheduler.add_job(enviar_resumo_diario, "cron",
                           day_of_week="mon-fri", hour=_resumo_hora, minute=0,
                           id="resumo_diario", replace_existing=True)
        print(f"APScheduler — resumo diário agendado seg-sex às {_resumo_hora}h")
    # Alerta pessoal de pendências no fim do expediente (e-mail + notificação),
    # uma hora antes do resumo diário por padrão, pra dar tempo de atualizar.
    # Desligado por padrão (feature nova, dispara pra empresa toda): revisar o
    # preview em GET /api/admin/alerta-pendencias antes de ligar em produção
    # via ALERTA_PENDENCIAS_ENABLED=1. ALERTA_PENDENCIAS_HORA define o horário (default 17).
    if os.environ.get("ALERTA_PENDENCIAS_ENABLED", "0") == "1":
        _pendencias_hora = int(os.environ.get("ALERTA_PENDENCIAS_HORA", "17"))
        _scheduler.add_job(enviar_alerta_pendencias, "cron",
                           day_of_week="mon-fri", hour=_pendencias_hora, minute=0,
                           id="alerta_pendencias", replace_existing=True)
        print(f"APScheduler — alerta de pendências agendado seg-sex às {_pendencias_hora}h")
    # Sync do Microsoft Loop -- desativado por padrão (LOOP_SYNC_ENABLED="0")
    # até o Entra ID App Registration existir de verdade. Ver bloco acima.
    if os.environ.get("LOOP_SYNC_ENABLED", "0") == "1":
        _loop_minutos = int(os.environ.get("LOOP_SYNC_MINUTOS", "20"))
        _scheduler.add_job(_loop_sync_job, "interval", minutes=_loop_minutos,
                           id="loop_sync", replace_existing=True)
        print(f"APScheduler — sync do Microsoft Loop a cada {_loop_minutos}min")
    # Assistente OPS — capacidade D (observar), job de madrugada: detecta
    # padrão de repetição/retrabalho/pendência parada, redige e grava
    # sinalização. Desativado por padrão (ASSISTENTE_OBSERVAR_ENABLED="0")
    # -- feature nova, precisa de combinado com a liderança antes de ligar
    # em produção (regra 8 do CLAUDE.md do assistente: a sinalização é da
    # pessoa, nunca sobe pro gestor -- mas alguém tem que saber que a
    # capacidade existe antes da equipe ver o primeiro aviso).
    if os.environ.get("ASSISTENTE_OBSERVAR_ENABLED", "0") == "1":
        from app.assistente.capacidade_observar.job import rodar_sync as _observar_job
        _observar_hora = int(os.environ.get("ASSISTENTE_OBSERVAR_HORA", "3"))
        _scheduler.add_job(_observar_job, "cron", hour=_observar_hora, minute=0,
                           id="assistente_observar", replace_existing=True)
        print(f"APScheduler — assistente (capacidade D) agendado às {_observar_hora}h")
    # Timer das tarefas: passa o tempo corrido pra `segundos` a cada minuto
    # (ver consolidar_timers).
    _scheduler.add_job(consolidar_timers, "interval", minutes=1,
                       id="consolidar_timers", replace_existing=True,
                       max_instances=1, coalesce=True)
    # Service Desk: fecha status (pausa/online) esquecido aberto depois do
    # fim do turno -- sem isso o painel chegava a mostrar 96h de pausa.
    _scheduler.add_job(limpar_sessoes_expiradas, "interval", minutes=10,
                       id="limpar_sessoes", replace_existing=True,
                       max_instances=1, coalesce=True)
    from app.service_desk.router import job_encerrar_status_esquecidos
    _scheduler.add_job(job_encerrar_status_esquecidos, "interval", minutes=5,
                       id="sd_encerrar_status", replace_existing=True,
                       max_instances=1, coalesce=True)
    _scheduler.start()
    print("APScheduler iniciado — relatório agendado para dia 1 de cada mês às 08h")
except ImportError:
    print("APScheduler não instalado — relatórios automáticos desativados. Instale com: pip install apscheduler")


# Relatório mensal (montagem, envio por e-mail e endpoints) -- em app/relatorio_mensal/router.py
from app.relatorio_mensal.router import router as relatorio_mensal_router
app.include_router(relatorio_mensal_router)

# ─────────────────────────────────────────────────────────────


# Gestão de projetos (PMO): projetos, comentários, meus projetos e contratos -- em app/gestao_projetos/router.py
from app.gestao_projetos.router import router as gestao_projetos_router
app.include_router(gestao_projetos_router)


# Status de Campo (despachos técnicos por site/cliente) -- em app/status_campo/router.py
from app.status_campo.router import router as status_campo_router
app.include_router(status_campo_router)


# Painel de Controle do N2 -- em app/painel_n2/router.py
from app.painel_n2.router import router as painel_n2_router
app.include_router(painel_n2_router)


# Importação de planilha de cronograma/atividades para o Status de Campo -- em app/importacao_cronograma/router.py
from app.importacao_cronograma.router import router as importacao_cronograma_router
app.include_router(importacao_cronograma_router)


# Importação da planilha de cronograma para a Escala N2 -- em app/importacao_escala_n2/router.py
from app.importacao_escala_n2.router import router as importacao_escala_n2_router
app.include_router(importacao_escala_n2_router)


# EQUIPE DEV (kanban, comentários, checklist e diário) -- em app/equipe_dev/router.py
from app.equipe_dev.router import router as equipe_dev_router
app.include_router(equipe_dev_router)

# Integração com o Microsoft Loop (esqueleto) -- em app/loop/router.py
from app.loop.router import router as loop_router
app.include_router(loop_router)

# Suporte: solicitações dos usuários e respostas da equipe dev -- em app/suporte/router.py
from app.suporte.router import router as suporte_router
app.include_router(suporte_router)
