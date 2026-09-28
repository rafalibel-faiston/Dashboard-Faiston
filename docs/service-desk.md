# Service Desk (operação SGB)

Módulo isolado dentro do Faiston OPS para a operação de Service Desk:
jornada do operador, registro rápido de atendimento, histórico e escalas.

- Código: `app/service_desk/` (router, schema, regras de turno)
- Tela: `static/service-desk.html` → rota `/service-desk`
- Tabelas: só `sd_*` (não toca em tabela de outra operação)
- Testes: `tests/test_service_desk.py`

## Acesso

| Quem | Como cadastrar | O que vê |
|---|---|---|
| Operador | Gerenciar Usuários → perfil Funcionário, cargo **Service Desk — Operador**, time **Service Desk** | Painel do operador (`/service-desk`): jornada, os próprios atendimentos, escalas |
| Supervisor | cargo **Service Desk — Supervisor** (ou gestor do time Service Desk) | Entra no `/dashboard` só com o menu do Service Desk (visão gerencial) + Equipe em tempo real, atendimentos de todos, edita escala, cadastra status |
| admin / diretor / dev | — | tudo |

A área **Service Desk** é criada sozinha no cadastro de Áreas (Admin → Áreas)
no boot do módulo, aceitando só os cargos do SD e sem trabalhar por projeto.
Os cargos `sd_operador`/`sd_supervisor` não entram nas outras áreas; o admin
pode ligar se precisar, na própria tela de Áreas.

Quem é de outro time recebe 403 na API e é redirecionado fora da tela.
O login do operador cai direto em `/service-desk`.

Matrícula, nível (N1/N2/N3), site, jornada padrão e meta por turno são
configurados pelo supervisor na aba **Equipe** (ícone de engrenagem).

## Visão gerencial (`/dashboard` → Service Desk)

Supervisor, gestor da área e admin/diretor. Dados de `GET /api/sd/dashboard?periodo=hoje|7d|30d|90d`:

- KPIs: atendimentos (e média/dia), resolução no 1º contato, % redirecionados,
  em aberto, quem está online/em pausa agora, tempo total em pausa.
- Atendimentos por dia, volume por hora do dia (dimensionar escala),
  principais categorias e filas de entrada.
- Produção por operador: atendimentos, média por dia trabalhado, 1º contato,
  redirecionados e pausa no período, com o status de agora.

Mesmo formato do Visão Geral: KPIs com selo e abas **Métricas | Kanban**.

**Kanban** (`GET /api/sd/kanban?periodo=...`): colunas Em aberto (tudo que não
finalizou, de qualquer data) · Redirecionado · Concluído (no período),
agrupadas por operador ou por fila. Por operador, quem está mais carregado
sobe. Arrastar o card troca só o status (`PATCH /api/sd/atendimentos/{id}/status`).

**Carga da equipe** (mesmo painel do Kanban de tarefas): pontos = atendimentos
em aberto, parado há mais de 4h conta 1,5x. Régua: Fluindo a partir de 2,
Carga alta a partir de 5, Atolado a partir de 8 (`CARGA_SD_LIMITES` no router).

## Registro rápido (premissa: mais rápido que bloco de notas)

- **Ctrl+Enter** salva; o formulário limpa mantendo canal e fila, com o foco de volta no nome.
- **Alt+1..9** troca o canal; **Ctrl+K** busca; **Esc** limpa.
- Solicitante tem autocompletar (nome ou login) e preenche o login sozinho.
- **Modelos rápidos**: as combinações problema + tratativa que o operador mais
  repete (≥ 2× em 60 dias) viram botões — sem cadastro, sai do histórico.
- Fila, canal e categoria novos digitados são criados na hora.
- Preencher "redirecionado para" já marca o status **Redirecionado**.
- Canal e fila abrem com o último usado; listas ordenadas pelas mais usadas.

## Regras de turno (`app/service_desk/turno.py`)

- Turno noturno (19h–07h) atravessa a meia-noite: às 02h o turno é o que começou ontem.
- A contagem "do turno" (KPI e lista) usa janelas contíguas: de 2h antes do
  início até 2h antes do próximo turno — hora extra e chegada antecipada não somem.
- Escala cadastrada no dia (ou na véspera, para plantão noturno) tem prioridade
  sobre a jornada padrão do operador.
- Status (Online / Pausa com motivo / Indisponível) é um log de eventos. Status
  esquecido aberto depois do fim do turno é fechado automaticamente (job a cada
  5 min + na abertura do painel) — o sistema antigo chegava a mostrar 96h de pausa.

## KPIs

- **Produtividade**: atendimentos no turno ÷ meta do operador.
- **Resolução no 1º contato (FCR)**: concluídos sem redirecionamento ÷ atendimentos com status que finaliza.
- **Fila com maior demanda**: fila de entrada mais frequente no turno.

## Pendências (levantar com a operação)

- Campos obrigatórios reais, categorias e filas oficiais.
- Tipos de pausa e limites de tempo (NR-17).
- Meta por turno/nível e SLAs cobrados pelo cliente.
- Integração com o ServiceNow (hoje o nº do chamado é digitado).
- Retenção dos dados do solicitante (nome e login de rede são dados pessoais — LGPD).
