"""Ponto de entrada do Faiston OPS: cria o app, middlewares e tratamento de
erro, sobe o schema do banco, registra os routers de app/ e o agendador.

As rotas vivem em app/<domínio>/router.py e o que é compartilhado entre elas
(banco, sessão, acesso, e-mail, agenda) em app/core/ -- ver refatoração de
2026-10-08.
"""
import logging
import os
import traceback
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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


# Banco e sessão vêm de app/core. get_session, hash_senha e _pool_db ficam
# acessíveis como main.X porque a suíte de testes usa.
from app.core.auth import get_session, hash_senha, limpar_sessoes_expiradas  # noqa: F401
from app.core.db import _pool_db, _request_conns, get_db  # noqa: F401
from app.core.schema import _migrar_timer_tarefas, setup_banco

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

setup_banco()
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
