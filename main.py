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


# Resumo diário de alterações nas tarefas e alerta pessoal de pendências (e-mails) -- em app/resumo_diario/router.py
from app.resumo_diario.router import router as resumo_diario_router, enviar_alerta_pendencias, enviar_resumo_diario
app.include_router(resumo_diario_router)


# Login, logout, troca e redefinição de senha, /api/me e tutoriais -- em app/autenticacao/router.py
from app.autenticacao.router import router as autenticacao_router
app.include_router(autenticacao_router)


# Usuários e bloqueios de agenda -- em app/usuarios/router.py
from app.usuarios.router import router as usuarios_router
app.include_router(usuarios_router)


# Áreas, frentes e catálogo de pesos das atividades -- em app/areas/router.py
from app.areas.router import router as areas_router
app.include_router(areas_router)


# Tarefas: CRUD, timer no servidor e histórico de alterações -- em app/tarefas/router.py
from app.tarefas.router import router as tarefas_router, consolidar_timers
app.include_router(tarefas_router)


# Ferramentas do admin: quem está online, diagnóstico, uso do sistema e correções de dados -- em app/admin/router.py
from app.admin.router import router as admin_router
app.include_router(admin_router)


# Métricas do dashboard -- em app/metricas/router.py
from app.metricas.router import router as metricas_router
app.include_router(metricas_router)

# Carga de trabalho da equipe (quem está atolado) -- em app/carga/router.py
from app.carga.router import router as carga_router
app.include_router(carga_router)


# Exportação para Excel, seed de demonstração, health e registro de ação do Backoffice -- em app/dados/router.py
from app.dados.router import router as dados_router
app.include_router(dados_router)


# Rotas que servem as páginas HTML -- em app/paginas/router.py
from app.paginas.router import router as paginas_router
app.include_router(paginas_router)


app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js", StaticFiles(directory="static/js"), name="js")

# Comentários das tarefas -- em app/comentarios/router.py
from app.comentarios.router import router as comentarios_router
app.include_router(comentarios_router)

# Relatório por cliente -- em app/relatorio_cliente/router.py
from app.relatorio_cliente.router import router as relatorio_cliente_router
app.include_router(relatorio_cliente_router)

# Notificações -- em app/notificacoes/router.py
from app.notificacoes.router import router as notificacoes_router
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
