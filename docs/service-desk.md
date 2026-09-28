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
| Operador | Gerenciar Usuários → perfil Funcionário, cargo **Service Desk — Operador**, time **Service Desk** | Minha jornada, os próprios atendimentos, escalas |
| Supervisor | cargo **Service Desk — Supervisor** (ou gestor do time Service Desk) | + Equipe em tempo real, atendimentos de todos, edita escala, cadastra status |
| admin / diretor / dev | — | tudo |

Quem é de outro time recebe 403 na API e é redirecionado fora da tela.
O login do operador cai direto em `/service-desk`.

Matrícula, nível (N1/N2/N3), site, jornada padrão e meta por turno são
configurados pelo supervisor na aba **Equipe** (ícone de engrenagem).

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
