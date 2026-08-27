# Sinalização de carga — "quem está atolado"

Objetivo: o gestor bater o olho no Kanban e ver **quem está sobrecarregado**,
sem precisar contar card. A pessoa vê a mesma medida no próprio quadro.

## Como a conta é feita

A carga mede **fila**, não produção: só entram tarefas com status `aberto` e
`em_andamento` do **dono** da tarefa (colaborador não conta, igual aos
contadores do quadro pessoal). Tarefa concluída sai da conta.

```
pontos = Σ peso(tarefa) × multiplicador
```

| Item | Valor |
| --- | --- |
| `peso` | 1 a 4, carimbado na tarefa a partir do tipo de atividade (`tipos_atividade.peso`) |
| tarefa sem peso (anterior à régua) | conta como 2 (`CARGA_PESO_PADRAO`) |
| tarefa atrasada (`data_prazo < hoje`) | multiplicador 1,5 |
| vence hoje ou amanhã | multiplicador 1,25 |
| demais | multiplicador 1,0 |

Contar só o número de tarefas engana — dez acionamentos (peso 1) não pesam
como quatro faturamentos (peso 4). Por isso a base é o peso, que já existia
na régua de complexidade da operação.

## Níveis

| Nível | Rótulo na tela | Faixa padrão (pontos) |
| --- | --- | --- |
| `tranquilo` | Tranquilo | < 10 |
| `moderado` | Fluindo | 10 a 17 |
| `pesado` | Carga alta | 18 a 27 |
| `atolado` | Atolado | ≥ 28 |

Regra extra: **3 ou mais tarefas atrasadas marcam "atolado"** mesmo com poucos
pontos — fila vencida é problema independente do tamanho dela.

## Calibragem por time

A régua fica em `configuracoes`, chave `carga_limites_{time}` (JSON com
`moderado`, `pesado`, `atolado`, `atrasadas_atolado`). Sem registro, vale
`CARGA_LIMITES_PADRAO`. Admin/gestor/diretor calibra pelo botão **Calibrar**
no painel do Kanban (`PUT /api/config/carga-limites`, sempre no time da
sessão). O que atola o Backoffice não é o mesmo que atola o N2 — por isso a
régua é por time, assim como o catálogo de pesos já é por área.

## API

- `GET /api/carga-equipe` — admin/diretor vê todos os times, gestor/demo vê o
  próprio time, funcionário vê só a si mesmo. Devolve `equipe` (uma linha por
  pessoa, ordenada por pontos), `resumo`, `limites` e `niveis` (rótulo, cor e
  ícone usados pela tela).
- `GET|PUT /api/config/carga-limites` — lê/calibra a régua do time da sessão.

Cada linha de `equipe` traz `pontos`, `tarefas`, `peso_total`, `peso_medio`,
`atrasadas`, `vence_ja`, `nivel`, `limite_atolado`, `indisponivel` (férias ou
afastamento ativo hoje) e `destaques` (até 5 tarefas, atrasadas e mais pesadas
primeiro). Pessoas sem nenhuma tarefa na fila aparecem quando são
`perfil='funcionario'` — é o "quem está livre pra receber".

## Onde aparece

- **Kanban do gestor** (`static/index.html`): painel *Carga da equipe* acima
  das colunas (cards por pessoa, barra de pontos, chips de alerta) e um selo
  de nível no cabeçalho de cada pessoa nas colunas de fila. Clicar num card
  filtra o quadro por aquela pessoa. O selo não aparece em "Concluído" nem no
  agrupamento por projeto.
- **Quadro do funcionário** (`static/funcionario.html`): faixa *Sua carga
  agora* entre os KPIs e as colunas.

O painel considera **todas** as tarefas abertas da pessoa, independente dos
filtros de cliente/projeto da tela — é a carga real dela, não a fatia filtrada.
