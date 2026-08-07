# Módulo de MC (Margem de Contribuição) — Faiston Ops

Ingestão e visualização das planilhas de MC que chegam junto com o e-mail de
KICK-OFF e ficam na pasta de MC's do OneDrive.

O Ops lê a planilha `.xlsx`, confere os totais contra o que ela declara,
persiste e expõe na tela.

## Dois caminhos de ingestão

**Preferido — o Ops lê a planilha:**

```
planilha .xlsx no OneDrive
        ↓  (só o link)
POST /api/mc/importar-planilha   ou   tool MCP mc_importar_planilha
        ↓  o Ops baixa, extrai e lê os totais declarados
mc_contratos + mc_equipe + mc_investimentos + mc_ingestoes_log
        ↓
módulo MC (index.html)
```

**Alternativo — números já em mão:**

```
JSON  →  POST /api/mc/importar  (ou tool mc_importar)
```

O caminho da planilha existe porque o primeiro desenho — o Claude ler o
`.xlsb` e mandar JSON — não se sustentou: leitura variável, e o modelo não
mandava os totais declarados, então a conferência do Ops ficava desligada e
número errado entrava sem ninguém ver. Com o Ops lendo o arquivo, a extração é
determinística e os totais saem da própria planilha, então a conferência sempre
roda.

O caminho do JSON continua útil para registrar o que veio no **corpo do e-mail
de kick-off** antes de a planilha existir (status `RECEBIDA`), e reimportar
pela planilha depois substitui esse registro.

> **`.xlsb` não serve.** Nem o `openpyxl` do Ops lê, nem o conector do
> Microsoft 365 aceita o MIME. O arquivo precisa ser salvo como `.xlsx` — é o
> único passo manual que sobra no fluxo.

## Por que tabelas novas

Nenhuma estrutura existente do Ops comporta MC:

| Estrutura existente | Por que não serve |
| --- | --- |
| `forecast_projetos` | 1 linha por projeto com o consolidado de margem **já calculado**. Não guarda as linhas de custo que produzem esse número. |
| `forecast_mensal` | P&L por período (realizado/projetado por mês), não o planejado detalhado do contrato. |
| `lancamentos` | Realizado por projeto (`projeto_id` + valor + data). MC é planejado por contrato/ano. |
| `contratos_gestao` | Só cadastro do contrato (nome, SDM, status, datas). |

MC é o **planejado detalhado por contrato/ano**: N linhas de equipe e N linhas
de investimento. Daí a estrutura própria.

## Tabelas

Criadas sob demanda por `_ensure_mc_tables(cur)` — o mesmo padrão do Forecast
(`_ensure_forecast_tables`). Não existe migration em arquivo `.sql` neste repo:
todo o schema vive em código (`setup_banco()` e os `_ensure_*`), então subir o
app já aplica.

- **`mc_contratos`** — cabeçalho de cada MC. Chave única `(contrato_codigo, ano)`.
  Guarda os totais somados das linhas, os totais declarados na planilha, o
  status, o motivo da revisão e o payload original inteiro em `payload` (JSONB).
- **`mc_equipe`** — linhas de custo de equipe. Cada linha guarda também o JSON
  bruto em `bruto`, então coluna nova na planilha não se perde.
- **`mc_investimentos`** — linhas de investimento, mesma ideia.
- **`mc_ingestoes_log`** — histórico de **toda** tentativa de ingestão, inclusive
  as que falharam. `mc_id` é `ON DELETE SET NULL`: apagar a MC não apaga a
  auditoria.

## Status

| Status | Significado |
| --- | --- |
| `RECEBIDA` | Só o cabeçalho chegou (cliente/projeto do corpo do e-mail de kick-off), a planilha ainda não foi extraída. Reimportar com as linhas substitui. |
| `PROCESSADA` | Os totais declarados na planilha fecham com a soma das linhas **e** o contrato foi vinculado. |
| `REVISAO_MANUAL` | Divergência de total e/ou contrato não encontrado no Ops. O motivo fica em `motivo_revisao`. |
| `ERRO` | Falha de verdade na ingestão (exceção gravada no log). |

A MC é **sempre gravada**, mesmo com problema — perder o arquivo é pior do que
vincular/conferir depois. `PATCH /api/mc/contratos/{id}/status` resolve na mão.

## Endpoints

| Método | Rota | Perfis |
| --- | --- | --- |
| `POST` | `/api/mc/importar-planilha` (arquivo ou url) | admin, gestor, diretor |
| `POST` | `/api/mc/importar` (JSON) | admin, gestor, diretor |
| `GET` | `/api/mc/contratos` (`?status=&contrato=`) | admin, gestor, diretor, demo |
| `GET` | `/api/mc/contratos/{id}` | admin, gestor, diretor, demo |
| `PATCH` | `/api/mc/contratos/{id}/status` | admin, gestor, diretor |
| `DELETE` | `/api/mc/contratos/{id}` | admin, gestor, diretor |
| `GET` | `/api/mc/ingestoes` (`?limite=`) | admin, gestor, diretor, demo |

Leitura acompanha o Forecast (inclui `demo`). Escrita **exclui `demo`** de
propósito, diferente do Forecast: MC alimenta número financeiro de contrato e
este é o mesmo endpoint que a automação vai chamar mais pra frente.

Como toda rota `/api/*` que muda estado, `POST/PATCH/DELETE` passam pelo
`CSRFMiddleware` e exigem o header `X-CSRF-Token` batendo com o cookie
`csrf_token`. No navegador o patch global de `fetch` (topo do `index.html`) já
cuida disso; num cliente fora do navegador é preciso mandar o header à mão.

## Importar pela planilha

`POST /api/mc/importar-planilha` aceita as duas formas:

- **multipart** com o campo `file` (o `.xlsx` em si) — é o que a tela usa;
- **JSON** com `url` — link do OneDrive/SharePoint, baixado por
  `_download_planilha()`, o mesmo utilitário que a sincronização de planilha de
  projeto já usa.

Nos dois casos dá para mandar `contrato`, `ano`, `cliente`, `cliente_final`,
`projeto` e `tcv` junto: **o que é informado vence** o que foi inferido do nome
do arquivo. Isso importa porque o nome do arquivo nem sempre traz o código de
faturamento certo — aconteceu de o assunto do e-mail dizer `F260301` quando o
correto era `F260534`.

A extração vive em `mc_planilha.py`, separada de propósito: é a peça que precisa
de calibragem quando aparecer um layout diferente.

- **Nada de posição fixa de célula.** O template tem várias abas e versões desde
  2021. O parser acha a *linha de cabeçalho* por nome de coluna (mesma
  estratégia do `_parse_forecast_workbook` no Forecast) e lê dali para baixo.
- **Vocabulário de colunas** tolerante: `Função`/`Cargo`/`Recurso`,
  `Qtde`/`Quantidade`/`HC`, `Custo Total (R$)`/`Total`/`Custo Anual`, etc.
- **Desempate equipe × investimento**: `salário`, `encargos` e `meses` só
  existem em equipe; o nome da aba (`2.1 Equipe`, `Investimentos`) resolve o
  resto.
- **Linha de `TOTAL` encerra o bloco** em vez de entrar como item — somá-la
  junto dobraria o valor.
- **Totais declarados**: procura células tipo `TOTAL EQUIPE` / `TOTAL
  INVESTIMENTOS` e pega o número à direita. É o dado mais importante do
  arquivo: é contra ele que a conferência acontece.
- **Diagnóstico sempre**: a resposta traz as abas vistas e os cabeçalhos
  reconhecidos. Quando não reconhece nada, o erro (422) lista as abas — calibrar
  fica uma iteração em vez de adivinhação.
- Nome de arquivo vindo de URL é **desescapado** antes de procurar o contrato:
  `MC%20F260015-7%20-%20ANO%2001.xlsx` esconderia o código, porque o `0` do
  `%20` cola no `F` e mata o limite de palavra do regex.

## Contrato do JSON de `POST /api/mc/importar`

Único campo obrigatório: o código do contrato.

```json
{
  "contrato": "F260015",
  "ano": "01",
  "cliente": "NTT Data",
  "cliente_final": "McDonald's",
  "projeto": "Sustentação de campo — SP",
  "tcv": 1250000.00,
  "arquivo": "MC F260015 - ANO 01.xlsb",
  "equipe": [
    {"funcao": "Coordenador de Projeto", "quantidade": 1, "salario": 9500.00,
     "encargos": 7600.00, "beneficios": 1200.00, "custo_mensal": 18300.00,
     "meses": 12, "custo_total": 219600.00}
  ],
  "investimentos": [
    {"item": "Notebook Dell Latitude 3450", "quantidade": 4,
     "valor_unitario": 5600.00, "valor_total": 22400.00}
  ],
  "totais": {"equipe": 310926.63, "investimentos": 28400.00}
}
```

O parser é **deliberadamente tolerante**, porque o formato exato que o
`mc_extractor.py` emite ainda pode mudar e não vale reprovar uma planilha
inteira por nome de coluna:

- **Apelidos de campo**, comparados sem acento/caixa/pontuação:
  `contrato` = `contrato_codigo` = `codigo`;
  `equipe` = `custos_equipe` = `mao_de_obra`;
  `investimentos` = `investimento` = `capex`;
  `funcao` = `cargo` = `descricao` = `item`;
  `custo_total` = `total` = `valor_total`;
  `valor_unitario` = `unitario` = `preco_unitario`; etc.
- **Números em texto pt-BR**: `"R$ 310.926,63"`, `"310.926,63"` e `310926.63`
  chegam ao mesmo lugar. Parênteses viram negativo.
- **Agrupamento por aba**: se `equipe` vier como dict (`{"Equipe A": [...]}`),
  cada chave entra como `categoria` das linhas de dentro.
- **Total ausente**: se a linha não traz `custo_total`, ele é derivado de
  `custo_mensal × meses × quantidade` — mas **nunca** sobrescreve o total que a
  planilha trouxe, que é a fonte da verdade da conferência.
- **Campo desconhecido não quebra e não se perde**: cada linha vai inteira para
  `bruto` (JSONB) e o envelope inteiro para `mc_contratos.payload`.

### Conferência

Se `totais.equipe` / `totais.investimentos` vierem no payload, são comparados
com a soma das linhas com tolerância de **R$ 0,01** (cobre ruído de float no
caminho `.xlsb` → JSON). Divergência acima disso → `REVISAO_MANUAL` com os dois
números no `motivo_revisao`. Sem `totais` no payload, não há o que conferir e a
soma das linhas é aceita.

### Idempotência

O `ano` é normalizado para dois dígitos (`"1"`, `"01"` e `"ANO 01"` viram `01`).
Sem isso a chave tratava a mesma MC escrita de outro jeito como uma segunda MC
— apareceu de verdade: a extração mandou `"1"` e o exemplo mandou `"01"`.

Linhas em branco da planilha (sem descrição e sem nenhum valor) são descartadas:
a extração real trouxe linhas de separação junto, que poluíam o detalhe sem
somar nada. Linha com descrição e valor zero **fica** — `Despesa Operação` a
zero é informação da planilha, não lixo.

Reimportar o mesmo `(contrato, ano)` **substitui**: as linhas antigas são
apagadas, o `mc_contratos.id` é mantido (`ON CONFLICT ... DO UPDATE`) e uma nova
entrada aparece no `mc_ingestoes_log`. Planilha corrigida e reenviada não gera
MC duplicada. Anos diferentes do mesmo contrato coexistem.

## Vínculo com o contrato

Ordem tentada por `_mc_resolver_contrato()`, hoje **casamento exato**:

1. `forecast_projetos.codigo` — é lá que o código do contrato (`F260015`) vive
   no Ops hoje. Preenche cliente/cliente final/projeto quando o payload não traz.
   (`forecast_projetos` é criada sob demanda, então a checagem passa por
   `to_regclass` — num banco onde ninguém abriu o Forecast a tabela pode não
   existir e isso não pode derrubar a MC.)
2. `contratos_gestao.nome` — preenche `mc_contratos.contrato_id` (FK real).
3. Nada casou → grava com `match_origem = 'nenhum'` e joga em `REVISAO_MANUAL`.

Quando nada casa, o motivo da revisão traz **sugestões** de códigos parecidos
(prefixo antes do sufixo) — sem vincular. Isso saiu do uso real: o extrator tira
o código do nome do arquivo, que vem com sufixo de versão (`F260015-7`),
enquanto o Ops guarda o código base (`F260015`). Vincular por prefixo
automaticamente seria arriscado (o `-7` pode ser outro item, não só revisão da
planilha), então a pista vai para quem decide.

> **Pendência (Rafael/Bruna):** a regra definitiva de match. Enquanto não
> define, nada se perde: cai em revisão manual, com a sugestão do parecido, e a
> tela permite vincular.

## Tela

**Módulo próprio na sidebar: MC** (`/dashboard?go=mc`), no `static/index.html`.

Não é sub-aba da Gestão de propósito. A Gestão é cadastro
(cliente/projeto/contrato) e o Forecast é consolidado de margem; MC é ingestão
de planilha de kick-off, com ciclo de vida próprio (recebida → conferida →
processada) e um estado que exige ação humana. Misturar com cadastro
esconderia esse ciclo dentro de uma tela que não é sobre isso.

Estrutura: `<div id="mod-mc">`, registrado na lista de `mods` do `setModule()`,
com link `#link-mc` na navegação principal (entre Financeiro e Histórico).
Não precisa de gating extra por perfil: `/dashboard` já só admite
admin/gestor/diretor/demo — `funcionario` é redirecionado para `/funcionario` —
e esse é exatamente o conjunto de `MC_PERFIS_LEITURA`.

Conteúdo:

- KPIs por status, clicáveis (filtram a lista; clicar de novo limpa).
- Badge no link MC da sidebar com o total que **exige ação humana**
  (`REVISAO_MANUAL` + `ERRO`), oculto quando é zero.
- Lista com busca por contrato/cliente/projeto e filtro de status.
- Modal de detalhe: cabeçalho, linhas de equipe e de investimento, marcação de
  "confere com a planilha" por bloco, histórico de ingestões daquela MC e botão
  *Marcar como processada*.
- Modal separado com o histórico geral de ingestões (`mc_ingestoes_log`).
- Botão **Importar MC**: aceita o JSON do extrator colado ou como arquivo
  `.json`, uma MC ou uma lista. Valida no navegador antes de enviar (JSON
  malformado aponta a posição; MC sem `contrato` é barrada), e mostra o
  resultado por MC — status, totais e motivo da revisão. Só aparece para
  admin/gestor/diretor, espelhando `MC_PERFIS_ESCRITA`; `demo` vê a lista mas
  não o botão.

  O fluxo desenhado é o extrator chamar `POST /api/mc/importar` direto — este
  modal existe pra dar um caminho pela tela, sem depender de ferramenta de API.

> **Pendência (Rafael/Bruna):** quem vê essa tela. Hoje é todo mundo que entra
> no dashboard — admin/gestor/diretor/demo. Pra restringir a admin: mudar
> `MC_PERFIS_LEITURA` no `main.py` **e** esconder o `#link-mc` no `init()` do
> `index.html`, do mesmo jeito que o `#link-admin` já é escondido.

## Testes

`tests/test_mc.py` — 25 testes, cobrindo acesso por perfil, conferência de
totais, tolerância de R$ 0,01, números em pt-BR, derivação de total,
idempotência, coexistência de anos, coluna desconhecida, filtros, log e ajuste
manual de status.

O caso-base usa **F260015 ano 01**, cujos totais foram conferidos contra a
planilha original: equipe **R$ 310.926,63**, investimentos **R$ 28.400,00**.

```bash
TEST_DATABASE_URL="postgresql://.../ops_teste" pytest tests/test_mc.py -q
```

## Pendências fora deste módulo

- Trocar o login de teste (`rafa.dev`) pela credencial de produção da Bruna na
  tarefa agendada `detectar-kickoff-mc-ops` — só depois de validar este módulo.
- Avaliar se o Cowork chama `/api/mc/importar` automaticamente ao terminar o
  `mc_extractor.py`, fechando o loop sem passo manual. Iteração futura, depois
  de rodar o fluxo manual algumas vezes.
- A tarefa agendada que cria o placeholder em `POST /api/forecast/projeto` a
  partir do corpo do e-mail de kick-off é **independente** deste módulo e
  continua funcionando como está.
