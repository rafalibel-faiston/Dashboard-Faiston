# Arquitetura do Faiston OPS

Backend FastAPI + PostgreSQL (psycopg2, sem ORM). Front em HTML/JS puro em `static/`.

## Como o código está organizado

- **`main.py`** — ponto de entrada: cria o `app`, middlewares (CSRF, fechamento de conexões), tratamento de erro, sobe o schema, registra os routers e o agendador (APScheduler). Não tem rota de negócio.
- **`app/core/`** — o que é compartilhado entre as rotas. Não importa nada de fora de `app/core` (exceto a constante de área do Service Desk).
- **`app/<domínio>/router.py`** — as rotas de cada domínio, num `APIRouter` incluído pelo `main.py` na mesma ordem de antes.

Regra de dependência: router pode importar de `app/core` e de outro router; `app/core` nunca importa router; ninguém importa o `main`.

## app/core

| Módulo | Para que serve | Linhas |
|---|---|---|
| `app/core/acesso.py` | Áreas, cargos, perfis e regras de acesso compartilhadas pelas rotas | 139 |
| `app/core/agenda.py` | Data de hoje no fuso de Brasília e bloqueios de agenda (férias, afastamento, recorrência) | 88 |
| `app/core/auth.py` | Senha (hash bcrypt, política) e sessão do usuário | 114 |
| `app/core/db.py` | Conexão com o Postgres: pool dos requests e conexão avulsa dos jobs | 174 |
| `app/core/email.py` | Envio de e-mail (Brevo) e o layout padrão dos e-mails do Faiston OPS | 267 |
| `app/core/paginas.py` | Helpers das rotas que servem páginas HTML | 39 |
| `app/core/schema.py` | Criação e migração do schema do banco na subida do app (CREATE/ALTER idempotentes) | 758 |

## Routers

| Módulo | Domínio | Rotas | Linhas |
|---|---|---|---|
| `app/admin/router.py` | Ferramentas do admin: quem está online, diagnóstico, uso do sistema e correções de dados | 10 | 274 |
| `app/areas/router.py` | Áreas, frentes e catálogo de pesos das atividades | 11 | 353 |
| `app/assistente/router.py` | Endpoints do assistente OPS | 23 | 793 |
| `app/autenticacao/router.py` | Login, logout, troca e redefinição de senha, /api/me e tutoriais | 10 | 312 |
| `app/avisos/router.py` | Avisos globais (banner de novidade e e-mail do Assistente OPS) | 3 | 116 |
| `app/carga/router.py` | Carga de trabalho da equipe (quem está atolado) | 3 | 232 |
| `app/carimbos/router.py` | Carimbos (textos prontos por área) | 4 | 114 |
| `app/clientes/router.py` | Cadastro de clientes | 4 | 102 |
| `app/comentarios/router.py` | Comentários das tarefas | 3 | 64 |
| `app/dados/router.py` | Exportação para Excel, seed de demonstração, health e registro de ação do Backoffice | 5 | 324 |
| `app/equipe_dev/router.py` | Equipe Dev: kanban interno, comentários, checklist e diário (restrito a perfil 'dev') | 15 | 344 |
| `app/financeiro/router.py` | Financeiro: projetos, lançamentos e importação de planilhas | 20 | 801 |
| `app/forecast/router.py` | Forecast / P&L (adaptação da planilha FORECAST2026) | 7 | 418 |
| `app/gestao_projetos/router.py` | Gestão de projetos (PMO): projetos, comentários, meus projetos e contratos | 12 | 357 |
| `app/historico/router.py` | Histórico completo | 2 | 114 |
| `app/ia_insights/router.py` | IA Insights | 1 | 141 |
| `app/importacao_cronograma/router.py` | Importação de planilha de cronograma/atividades para o Status de Campo | 1 | 250 |
| `app/importacao_escala_n2/router.py` | Importação da planilha de cronograma para a Escala N2 | 2 | 273 |
| `app/loop/router.py` | Integração com o Microsoft Loop (esqueleto) | 1 | 162 |
| `app/metricas/router.py` | Métricas do dashboard | 2 | 363 |
| `app/notas/router.py` | Notas pessoais | 5 | 150 |
| `app/notificacoes/router.py` | Notificações | 8 | 145 |
| `app/novidades/router.py` | Novidades do sistema | 6 | 249 |
| `app/paginas/router.py` | Rotas que servem as páginas HTML | 14 | 118 |
| `app/painel_n2/router.py` | Painel de Controle do N2 | 5 | 169 |
| `app/planilha_online/router.py` | Sincronização de planilha online (OneDrive / SharePoint) dos projetos | 3 | 162 |
| `app/relatorio_cliente/router.py` | Relatório por cliente | 4 | 90 |
| `app/relatorio_mensal/router.py` | Relatório mensal (montagem, envio por e-mail e endpoints) | 2 | 307 |
| `app/resumo_diario/router.py` | Resumo diário de alterações nas tarefas e alerta pessoal de pendências (e-mails) | 2 | 546 |
| `app/service_desk/router.py` | Endpoints do Service Desk (/service-desk e /api/sd/*) | 23 | 1095 |
| `app/status_campo/router.py` | Status de Campo (despachos técnicos por site/cliente) | 13 | 818 |
| `app/suporte/router.py` | Suporte: solicitações dos usuários e respostas da equipe dev | 8 | 334 |
| `app/tarefas/router.py` | Tarefas: CRUD, timer no servidor e histórico de alterações | 7 | 527 |
| `app/usuarios/router.py` | Usuários e bloqueios de agenda | 10 | 348 |

## Como adicionar uma rota nova

1. Ache o domínio em `app/<domínio>/router.py` (ou crie a pasta com `__init__.py` e `router = APIRouter()`).
2. Use `get_db()` de `app.core.db` e `get_session()` de `app.core.auth`; regras de acesso em `app.core.acesso`.
3. Router novo: `from app.<domínio>.router import router as <domínio>_router` + `app.include_router(...)` no `main.py`.
4. Teste em `tests/` — o CI (`.github/workflows/testes.yml`) roda a suíte a cada push e PR.
