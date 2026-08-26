# Especificação de implementação — Assistente OPS (Dashboard-Faiston)

Adaptação da spec genérica ao schema real deste repositório. Leia junto
com `docs/assistente-ops.md`. Implemente fase por fase, na ordem; não
avance sem cumprir o critério de aceite da fase anterior.

---

## 0. Variáveis de ambiente

```bash
LLM_BASE_URL=https://api.groq.com/openai/v1     # default já embutido em llm.py
LLM_API_KEY=gsk_...                              # se ausente, cai para GROQ_API_KEY (já usada por /api/ia/insights)
LLM_MODEL=openai/gpt-oss-20b                     # default já embutido em llm.py
LLM_TIMEOUT_S=30
EMBEDDING_MODEL=intfloat/multilingual-e5-small   # default já embutido em embeddings.py (Fase 3)
EMBEDDING_DIM=384                                # tem que bater com VECTOR(N) no schema — não troca sem reindexar tudo
```

Nenhuma dessas é obrigatória pra o app subir — `llm.py` só falha (com
`event: erro` no stream, nunca derrubando o resto do OPS) na hora de
efetivamente chamar o modelo sem chave configurada.

**Atenção pro deploy (Railway):** `embeddings.py` baixa o modelo
`intfloat/multilingual-e5-small` do Hugging Face na primeira vez que é
usado (a primeira ingestão, ou a primeira pergunta depois de haver
documento indexado) — precisa de saída de rede liberada pra
`huggingface.co`/`hf.co`. Esse download **não foi validado neste
ambiente de desenvolvimento** (o proxy daqui bloqueia huggingface.co por
política); confirmar que funciona de fato no Railway antes de contar com
a capacidade B em produção.

---

## 1. Schema

Implementado (Fase 1 + Fase 3 + Fase 5):

```sql
CREATE TABLE IF NOT EXISTS assistente_log (
    id              BIGSERIAL PRIMARY KEY,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario_id      INTEGER     NOT NULL REFERENCES usuarios(id),
    pergunta        TEXT        NOT NULL,
    contexto_tela   TEXT,
    capacidade      TEXT,                       -- 'achar' | 'explicar' | 'resumir' | 'fora_escopo' | NULL (genérica)
    resposta        TEXT,
    fontes          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    respondida      BOOLEAN     NOT NULL DEFAULT false,
    motivo_falha    TEXT,                       -- 'sem_documento' | 'erro_modelo' | 'chave_invalida' | 'modelo_invalido' | 'limite_taxa' | 'timeout' | 'sem_rede'
    tokens_entrada  INTEGER,
    tokens_saida    INTEGER,
    latencia_ms     INTEGER,
    feedback        SMALLINT                    -- NULL | 1 útil | -1 não útil
);

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS documento (
    id            BIGSERIAL PRIMARY KEY,
    titulo        TEXT NOT NULL UNIQUE,
    origem        TEXT,
    versao        TEXT,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    ativo         BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS documento_chunk (
    id           BIGSERIAL PRIMARY KEY,
    documento_id BIGINT NOT NULL REFERENCES documento(id) ON DELETE CASCADE,
    ordem        INTEGER NOT NULL,
    texto        TEXT NOT NULL,
    embedding    VECTOR(384) NOT NULL,           -- dimensão vem de EMBEDDING_DIM
    tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('portuguese', texto)) STORED
);

CREATE TABLE IF NOT EXISTS sinalizacao (
    id           BIGSERIAL PRIMARY KEY,
    usuario_id   INTEGER NOT NULL REFERENCES usuarios(id),
    detector     TEXT NOT NULL,                   -- 'repeticao_identica' | 'retrabalho' | 'pendencia_parada'
    assinatura   TEXT NOT NULL,                   -- o que define "o mesmo achado de novo", por detector
    evidencia    JSONB NOT NULL,                  -- o que foi detectado, vira input da redação e fica auditável
    texto        TEXT NOT NULL,                   -- redigido pelo modelo em cima da evidência
    criado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
    vista_em     TIMESTAMPTZ,
    feedback     SMALLINT                         -- NULL | 1 útil | -1 não útil | -2 nunca mais este detector
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sinalizacao_unica
    ON sinalizacao (usuario_id, detector, assinatura, (criado_em::date));
CREATE INDEX IF NOT EXISTS idx_sinalizacao_usuario
    ON sinalizacao (usuario_id, vista_em, criado_em DESC);

ALTER TABLE documento ADD COLUMN IF NOT EXISTS ordem_onboarding INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS idx_documento_ordem_onboarding
    ON documento (ordem_onboarding) WHERE ordem_onboarding IS NOT NULL;

CREATE TABLE IF NOT EXISTS onboarding_progresso (
    usuario_id    INTEGER PRIMARY KEY REFERENCES usuarios(id),
    etapa_atual   INTEGER NOT NULL DEFAULT 1,
    iniciado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    concluido_em  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS checkin_diario (
    id               BIGSERIAL PRIMARY KEY,
    usuario_id       INTEGER NOT NULL REFERENCES usuarios(id),
    data             DATE NOT NULL,
    resumo_enviado   TEXT,                          -- o que o assistente mandou (resumo de ontem)
    resposta_usuario TEXT,                          -- o plano que a pessoa disse pra hoje
    criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
    respondido_em    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_checkin_diario_unico
    ON checkin_diario (usuario_id, data);
```

Cada uma dessas tabelas (Fase 3, 5, 6, 7) é criada num bloco isolado
dentro de `setup_schema()`, com commit e tratamento de erro próprios —
uma fase falhando (ex.: extensão vector indisponível) não desfaz o que
as outras já conseguiram criar. Isso corrigiu um bug real: até essa
mudança, tudo dividia um único commit no final, e uma falha na Fase 6
chegou a apagar silenciosamente a criação da Fase 5 (`sinalizacao`) que
já tinha rodado antes na mesma transação.

`usuario_id` referencia `usuarios(id)`, a tabela real de usuário deste
sistema (`perfil` + `cargo` + `time` definem permissão — ver
`app/assistente/db.py:get_session`). `documento.titulo` é `UNIQUE`:
reingerir um título existente atualiza o mesmo documento em vez de
duplicar (apaga os chunks antigos, insere os novos — ver seção 6).

Se `CREATE EXTENSION vector`/`pg_trgm` falhar (plano do Neon sem
permissão, por exemplo), `setup_schema()` faz rollback só dessa parte e
segue — `assistente_log` continua funcionando (fases 1/2 não dependem
disso), só a capacidade B fica indisponível até habilitar a extensão.

Fase 5: nenhuma tabela nova de atividade pra capacidade D foi criada —
a atividade já existe em `tarefas`/`tarefa_historico` (backoffice/projeto)
e `status_atividades` (despacho de campo). Os detectores mapeiam as duas
primeiras em vez de duplicar — ver seção 8.

---

## 2. Contratos de API (implementados)

### `GET /assistente/elegivel`

Sem autenticação obrigatória (nunca 401/403 — só diz sim/não, é o que o
widget consulta pra decidir se aparece):

```json
{ "elegivel": true }
```

### `POST /assistente/pergunta`

Requer sessão válida (`faiston_token`) — qualquer perfil serve; sem
sessão, 401. Requer também `X-CSRF-Token` batendo com o cookie `csrf_token`
(mesmo middleware de `/api/*`, estendido pra cobrir `/assistente/*`).

Entrada:

```json
{ "pergunta": "qual o fluxo de abertura de chamado N2?", "contexto_tela": "funcionario" }
```

Saída: stream SSE, `media_type="text/event-stream"`.

```
event: inicio
data: {"log_id": 1841, "capacidade": "explicar"}

event: texto
data: {"delta": "O fluxo de RMA "}

event: texto
data: {"delta": "começa com a abertura do chamado."}

event: fontes
data: {"fontes": [{"documento_id": 12, "titulo": "POP RMA Vita", "trecho": "..."}]}

event: fim
data: {"tokens_entrada": 420, "tokens_saida": 60, "latencia_ms": 900}
```

`capacidade` é `"resumir"` (pedido de resumo semanal detectado por
palavra-chave), `"explicar"` (achou trecho de documento indexado) ou
`null` (conversa genérica — Fase 1, ou nenhum documento indexado ainda).
`event: fontes` só existe quando `capacidade == "explicar"` **e** a
resposta não foi a recusa fixa ("Não encontrei isso na base de
procedimentos.") — sai depois do texto, antes do `fim`.

Uma recusa (regra 2: sem trecho relevante, sem invenção) ainda é
`respondida: true` no log — é uma resposta honesta, não uma falha — mas
grava `motivo_falha = 'sem_documento'`, que é o que alimenta
`/assistente/lacunas` (ainda não implementado, Fase 4).

Erro do modelo vira `event: erro` com mensagem legível, nunca stack
trace, e grava `motivo_falha` no log (classificado a partir do tipo de
exceção da SDK — ver `llm.py:_classificar_erro`).

### `POST /assistente/feedback`

```json
{ "log_id": 1841, "util": true }
```

Só atualiza o log se `usuario_id` do log bater com quem está logado
(`log.py:registrar_feedback`) — 404 caso contrário, nunca deixa um
usuário avaliar a pergunta de outro.

### `GET /assistente/sinalizacoes` (Fase 5)

```json
{
  "sinalizacoes": [
    {
      "id": 1, "detector": "repeticao_identica",
      "texto": "Você fez o mesmo tipo de acionamento pro Arcos Dourados 14 vezes essa semana...",
      "criado_em": "2026-08-20T13:00:00Z", "vista_em": null, "feedback": null
    }
  ]
}
```

Sempre escopado por `sess["id"]` — não existe parâmetro pra pedir
sinalização de outra pessoa.

### `GET /assistente/sinalizacoes/contagem` (Fase 5)

`{ "nao_vistas": 2 }` — nunca 401/403 (mesma regra de `/elegivel`, é o
que alimenta o badge do widget); sem sessão elegível devolve `0`.

### `POST /assistente/sinalizacoes/{id}/visualizar` (Fase 5)

Marca `vista_em`. `403` se o id não existe ou não é da pessoa logada
(mesma resposta pros dois casos, pra não confirmar existência de id de
outra pessoa).

### `POST /assistente/sinalizacoes/{id}/feedback` (Fase 5)

```json
{ "feedback": 1 }
```

`1` útil, `-1` não útil, `-2` desliga aquele detector pra essa pessoa
permanentemente. `403` nas mesmas condições do endpoint acima.

---

## 3. Cliente do modelo — `app/assistente/llm.py`

Implementado conforme a spec original: `AsyncOpenAI`, `completar_stream`
com `stream_options={"include_usage": True}` pra contagem de tokens
chegar no fim do stream. Diferença da spec genérica: `LLM_API_KEY` cai
para `GROQ_API_KEY` se não setada (reaproveita a chave que
`/api/ia/insights` já usa em `main.py`), e a construção do client nunca
lança exceção na importação (evita derrubar `main.py` inteiro por falta
de env var — só falha na hora de chamar, dentro de `ModeloIndisponivel`).

---

## 4. Fase 1 — Log e caixa de perguntas · **feito**

Implementado:

- `app/assistente/db.py:setup_schema()` — cria `assistente_log`, chamada
  em `main.py` logo após `setup_banco()`.
- `POST /assistente/pergunta` grava no log (`log.py:criar_pergunta`,
  **antes** de qualquer chamada ao modelo) e devolve, via stream, uma
  resposta genérica com `prompts/sistema_generico.md`.
- `POST /assistente/feedback`.
- Widget (`static/assistente/widget.js` + `.css` + `avatar.svg`),
  incluído em `index.html`, `funcionario.html`, `n2.html`,
  `financeiro.html`, `historico.html` — não em `relatorio.html`
  (visualização de impressão/exportação) nem nas páginas pré-login.
  Botão flutuante com avatar, atalho `Alt+A`, streaming palavra a
  palavra, estado de erro distinto de carregamento, polegar
  útil/não útil, sem `localStorage` de histórico.

`prompts/sistema_generico.md` (adaptado ao domínio real — tarefas,
projetos, clientes, despacho técnico — em vez dos exemplos de
logística/estoque da spec original).

**Critério de aceite** (validar com a equipe antes da Fase 2):

- [ ] Uma pergunta feita no widget aparece em `assistente_log` com
      `usuario_id`, `latencia_ms` e `tokens_entrada`/`tokens_saida`
      preenchidos.
- [ ] A resposta aparece palavra a palavra, primeira palavra em menos de
      2 segundos.
- [ ] Perguntar algo que depende de dado interno (ex.: "quantas tarefas
      o João tem em aberto?") produz uma recusa honesta, não uma
      invenção.
- [ ] Rodar sem `LLM_API_KEY`/`GROQ_API_KEY` configurada produz
      `event: erro` legível no widget, sem quebrar a tela do OPS.

**Pare aqui.** Deixe rodando com a equipe (hoje só perfil `admin`) antes
de seguir pra Fase 2 — o log da primeira semana é o que deve orientar o
que vem depois, não suposição.

---

## 5. Fase 2 — Capacidade C, resumir · **feito**

`app/assistente/capacidade_resumir.py:montar_agregado_semana()` — agregado
inteiramente em SQL (mesma base do `/api/ia/insights` já existente em
`main.py`, com recorte por semana adicionado): tarefas concluídas e horas
por funcionário, tarefas por cliente, tickets acima do limite de atraso
(`limite_dias_atraso` vai explícito no JSON, não só embutido no nome do
campo — evita que o modelo escreva um número que pareça inventado) e
atendimentos de campo concluídos na semana (`status_atividades`).

Ainda sem classificação de intenção de verdade (isso é Fase 4,
`intencao.py`): `router.py:_eh_pedido_de_resumo()` é um gatilho por
palavra-chave (`"resumo"` + `"semana"`/`"semanal"`, sem acento) que decide,
dentro do mesmo `POST /assistente/pergunta`, entre chamar
`montar_agregado_semana()` + `prompts/sistema_resumir.md` ou seguir pelo
fluxo genérico da Fase 1. `capacidade` vai como `"resumir"` no evento
`inicio` e no log, em vez de `null`. O widget ganhou um chip de sugestão
("📊 Resumo da semana") no estado vazio do painel pra não depender só do
usuário adivinhar a frase certa.

Regras da spec original sem alteração: números exatamente do JSON, nunca
calculados pelo modelo; máximo cinco parágrafos.

**Critério de aceite:**

- [x] Todo número do texto gerado aparece no JSON de entrada —
      `tests/test_assistente_resumir_numeros.py` (não depende de banco
      nem de chamada ao modelo, roda sempre; testa a função
      `todos_numeros_batem()` que o critério pede).
- [ ] Rodar duas vezes com o mesmo JSON produz textos equivalentes em
      conteúdo — depende de chamada real ao modelo, precisa validação
      manual em produção/teste, não dá pra automatizar sem `LLM_API_KEY`.
- [ ] Validar com a equipe se os números escolhidos (tarefas concluídas e
      horas por funcionário/cliente, atrasados, atendimentos de campo)
      são de fato os que interessam no resumo semanal — ainda não
      confirmado com o Rafa (pergunta em aberto original).

---

## 6. Fase 3 — Capacidade B, explicar · **feito**

Implementado conforme a spec original, sem mudança de desenho:

- `app/assistente/ingestao.py` — `quebrar_em_blocos()` (500–700 palavras,
  80 de sobreposição, nunca corta no meio de frase; prioriza fechar o
  bloco numa borda de parágrafo assim que atinge o mínimo — se um
  parágrafo sozinho estoura o máximo, quebra por sentença dentro dele).
  `extrair_texto()` lê `.md`, `.docx` (`python-docx`) e `.pdf` (`pypdf`).
  `ingerir_documento()` prefixa o título do documento antes do embedding
  (melhora a recuperação de blocos do meio, que sozinhos perdem
  contexto), apaga os chunks antigos antes de inserir os novos quando o
  título já existe (`documento.titulo UNIQUE`).
- `app/assistente/embeddings.py` — `sentence-transformers`, modelo e
  dimensão configuráveis (`EMBEDDING_MODEL`/`EMBEDDING_DIM`), carregado
  só na primeira chamada (import pesado isolado, não atrasa a subida do
  resto do assistente). Prefixo `"query: "`/`"passage: "` embutido nas
  funções — nenhum outro módulo monta esse prefixo à mão.
- `app/assistente/capacidade_explicar.py` — `buscar_hibrido()` roda as
  duas buscas (vetorial via `pgvector`, textual via `tsvector`/
  `plainto_tsquery`) e funde com Reciprocal Rank Fusion
  (`fundir_rrf()`, extraída à parte, pura, testável sem banco). Devolve
  `None` em erro (o router avisa e não inventa) e `[]` quando não há
  documento indexado ainda (cai no genérico da Fase 1).
- `POST /assistente/pergunta` decide o roteamento: pedido de resumo →
  Fase 2; senão, roda a busca híbrida — achou trecho → capacidade
  `explicar` com `prompts/sistema_explicar.md`, resposta em texto
  corrido e natural (sem citar número de trecho tipo "[2]" no meio da
  frase — as fontes já vão à parte no evento `fontes`) e recusa
  exatamente "Não encontrei isso na base de procedimentos." sem trecho
  que sustente; sem nenhum documento indexado → genérico da Fase 1
  (comportamento inalterado até a primeira ingestão).
- `POST /assistente/documentos` — endpoint de ingestão (upload
  multipart: `arquivo` + `titulo` + `origem`/`versao` opcionais), restrito
  a `perfil == 'admin'` sempre (o assistente é aberto a todos os perfis,
  mas gestão de conteúdo é mais sensível que só perguntar).
- `GET /assistente/documentos` — tela de admin
  (`static/assistente/documentos.html`): arrastar/escolher arquivo,
  título/origem/versão, lista dos documentos já indexados com contagem
  de blocos e botão de remover (`DELETE /assistente/documentos/{id}`,
  `GET /assistente/documentos/lista` pro JSON). Mesmo gate server-side
  das outras páginas do OPS — sem sessão redireciona pro login, sem ser
  admin redireciona pro dashboard. Atalho (ícone ⚙) no cabeçalho do
  widget, visível só pra quem `GET /assistente/elegivel` devolve
  `admin: true`.

**Ainda falta:** nenhum POP real foi ingerido — o pipeline está pronto e
testado (chunking, RRF, roteamento, recusa, tela de upload) mas sem
conteúdo de verdade ainda não dá pra rodar o critério de aceite abaixo.

**Critério de aceite** (rodar depois de ingerir POPs reais pela tela em
`/assistente/documentos`):

- [ ] Conjunto de 20 perguntas com resposta conhecida na base + 10 cuja
      resposta não está na base.
- [ ] Nas 10 sem resposta, recusa honesta nas 10 — uma resposta
      inventada aqui reprova a fase.
- [ ] Nas 20 com resposta, pelo menos 16 corretas e todas com trecho
      citado (evento `fontes` presente).
- [x] Toda recusa grava `motivo_falha = 'sem_documento'` — já
      implementado e verificado com resposta simulada (`router.py`);
      falta só confirmar com conteúdo real.
- [x] `documento.titulo` repetido reingere em vez de duplicar (apaga
      chunks antigos, insere os novos) — implementado em
      `ingerir_documento()`.

---

## 7. Fase 4 — Capacidade A, achar · **feito**

Catálogo fixo (`app/assistente/catalogo.py`), sem SQL gerado pelo
modelo. Consultas implementadas — escolhidas pelo domínio, **não**
confirmadas contra o log real da Fase 1 (este ambiente de
desenvolvimento não tem acesso ao `assistente_log` de produção); ajustar
depois de olhar o log de verdade é esperado:

```python
def minhas_tarefas(sess: dict, status: str | None = None) -> dict:
    """Quantas tarefas a pessoa que perguntou tem, por status. Sempre
    sobre quem pergunta -- nunca recebe usuario_id de fora."""

def tarefas_por_cliente(sess: dict, cliente: str) -> dict:
    """Tarefas em aberto/andamento de um cliente específico."""

def escala_n2_do_dia(sess: dict, data: str) -> dict:
    """Quem está de plantão N2 numa data (YYYY-MM-DD)."""

def atividades_campo_pendentes(sess: dict, dias: int = 7) -> dict:
    """status_atividades não concluída/cancelada há mais de N dias."""

def buscar_carimbo(sess: dict, termo: str) -> dict:
    """Busca em `carimbos` (textos prontos de atendimento/acionamento
    que a própria equipe cadastra em /api/carimbos, main.py) por título
    ou categoria. Mesma regra de visibilidade do CRUD original: admin vê
    todos, os demais só os do próprio time (sess['time'])."""
```

`minhas_tarefas` cumpre a regra 5 (roda com a permissão de quem
perguntou) por desenho: não existe parâmetro pra consultar outra
pessoa. `tarefas_por_cliente`/`escala_n2_do_dia`/`atividades_campo_pendentes`
são visão de equipe (cliente, escala, despacho), sem recorte por
`time`/`cargo` ainda — aceitável no piloto atual (só `admin`), mas
listado como limitação conhecida pra quando o piloto abrir pra mais
perfis. `buscar_carimbo` já nasce escopada por time (reaproveitando a
regra que a tabela `carimbos` já tinha antes do assistente existir).

`buscar_carimbo` é a única função do catálogo cujo resultado é
reproduzido ao pé da letra na resposta (`capacidade_achar.py:
formatar_resposta`) — um carimbo é texto pronto pra colar num
atendimento, então o modelo nunca reformula o conteúdo, só ajuda a
achar o carimbo certo. Zero resultado → recusa honesta citando o termo
buscado; mais de um resultado → lista os títulos e pede um termo mais
específico (sem follow-up de conversa — cada pergunta é isolada, então
"qual desses" tem que ser respondido numa nova pergunta com nome mais
preciso, não um "sim"/"o segundo").

**Fluxo** (`app/assistente/capacidade_achar.py`):

1. `POST /assistente/pergunta` tenta achar **antes** de explicar/
   genérico, pra qualquer pergunta que não seja pedido de resumo. O
   modelo recebe a pergunta e o catálogo como `tools`
   (`tool_choice="auto"`, `llm.py:completar_com_ferramentas`, chamada
   sem streaming) e decide sozinho: chama uma função, ou não chama
   nenhuma (aí o router segue pra explicar/genérico normalmente — é
   assim que "achar" decide se a pergunta é dele, sem heurística de
   palavra-chave).
2. Se o modelo chamou uma função: `catalogo.executar()` é o único ponto
   de execução — valida que a função existe no catálogo e que os
   argumentos batem (tipo, faixa, formato de data), roda a query fixa
   parametrizada. Argumento ou função inválida vira `ValueError`/
   `TypeError`, nunca erro 500.
3. O texto final é montado por **código**, não pelo modelo
   (`capacidade_achar.py:formatar_resposta`) — regra 3, o modelo nunca
   recalcula/reafirma número. Sem streaming de verdade aqui (a resposta
   já está pronta), mandada como um único evento `texto`.
4. Função inexistente ou argumento inválido → "Não consegui entender
   qual informação você precisa." (nunca tenta adivinhar), grava
   `motivo_falha = 'funcao_nao_identificada'`, `respondida = false`.

**Critério de aceite:**

- [x] Nenhuma string SQL é construída a partir de saída do modelo —
      `tests/test_assistente_achar.py::test_nenhuma_sql_e_montada_dinamicamente_no_catalogo`
      percorre a AST de `catalogo.py` e garante que todo `cur.execute()`
      usa string literal fixa, nunca f-string/concatenação com
      argumento do modelo.
- [x] Perguntar por algo que não bate com nenhuma função devolve
      resposta honesta, não erro 500 — testado com mock do modelo
      (`resultado_achar = None` cai pro fluxo normal; `_invalido` vira
      recusa).
- [ ] Usuário sem permissão sobre uma base não recebe o dado dela — não
      testável hoje porque o piloto é só `admin`; falta desenhar quando
      o piloto abrir pra `cargo`/`time` diferentes.

---

## 8. Fase 5 — Capacidade D, observar · **feito, desligada por padrão**

**Sem tabela `atividade` nova.** A spec original propõe uma tabela
genérica de atividade; neste sistema ela já existe, espalhada — os três
detectores implementados usam o que já existia, sem exigir desenho de
captura adicional:

- `repeticao_identica` (`app/assistente/capacidade_observar/detectores.py`) —
  `tarefas` agrupado por `(usuario_id, tipo_atividade_id, cliente)`, 5+
  vezes numa janela de 5 dias. Assinatura: `tipo::cliente`. Peso:
  `tempo_total_s` somado (`tarefas.segundos`).
- `retrabalho` — `tarefa_historico` (`acao = 'editou'`) agrupado por
  `(autor_id, tarefa_id)`, 4+ edições em 7 dias. Assinatura:
  `tarefa::{tarefa_id}`. Peso: proxy de 30min por edição (não há duração
  registrada por edição).
- `pendencia_parada` — CTE com `percentile_cont(0.9)` da duração de
  tarefas concluídas por `tipo_atividade_id` (mínimo 5 amostras em 90
  dias), comparado contra tarefas abertas/em andamento mais velhas que
  esse p90. Assinatura: `tipo::{tipo_atividade}`. Peso: segundos de
  atraso sobre o p90. Usa percentil, não média — duração de tarefa tem
  cauda longa.

`duracao_anomala` (sessão de trabalho isolada) e `padrao_de_calendario`
da spec original não foram implementados: não existe início/pausa/fim
registrado por sessão neste sistema, só o acumulado em
`tarefas.segundos` — ficam pra quando/se existir esse desenho de captura.

Regra 9 do CLAUDE.md do assistente (detecção nunca passa pelo modelo)
garantida por teste estático: `detectores.py` não importa nada de
`llm.py` e todo `cur.execute()` usa string SQL literal fixa
(`tests/test_assistente_observar.py`).

**Orçamento e cooldown** (`capacidade_observar/job.py`), aplicados antes
de gastar uma chamada de modelo:

- no máximo 2 sinalizações por pessoa por semana — excedente descarta,
  não vira fila;
- cooldown de 30 dias por `(usuario_id, detector, assinatura)`;
- `feedback = -2` desliga aquele detector pra essa pessoa
  permanentemente.

**Redação** (`capacidade_observar/redacao.py`) é o único arquivo do
pacote que fala com o modelo — recebe a evidência já detectada, escreve
até duas frases sem julgar desempenho, ou responde `DESCARTAR` se não
tiver nada de fato acionável (vira `None`, não grava sinalização).

**Job diário** (`capacidade_observar/job.py:rodar`) roda fora do caminho
de `/pergunta`, agendado via APScheduler em `main.py`
(`ASSISTENTE_OBSERVAR_ENABLED=1`, `ASSISTENTE_OBSERVAR_HORA`, default
3h) — **desligado por padrão**, precisa de alinhamento com a liderança
antes de ligar (ver `assistente-ops.md`).

`sinalizacao` (schema na seção 1) é sempre lida/atualizada escopada por
`usuario_id` (`capacidade_observar/sinalizacoes.py`, endpoints na seção
2) — regra 8 do CLAUDE.md: a sinalização é da pessoa, nunca sobre a
pessoa; não existe (e não pode existir) uma consulta que devolva
sinalização de mais de uma pessoa, nem pra admin.

**Critério de aceite:**

- [x] Nenhuma query de detector chama o modelo — teste de AST
      (`test_detectores_nao_importa_nada_de_llm`,
      `test_detectores_so_usa_sql_literal_fixa`).
- [x] Orçamento de 2/semana respeitado e prioriza o achado de maior peso
      quando há mais candidatos que vaga
      (`test_orcamento_limita_a_dois_por_semana_e_prioriza_maior_peso`).
- [x] Cooldown de 30 dias bloqueia a mesma assinatura
      (`test_cooldown_de_30_dias_bloqueia_a_mesma_assinatura`).
- [x] `feedback = -2` desliga o detector permanentemente pra aquela
      pessoa (`test_feedback_menos_dois_desliga_o_detector_permanentemente`).
- [x] Teste que tenta acessar/alterar sinalização de outra pessoa e
      espera 403 — em duas camadas, função (`sinalizacoes.py`) e
      endpoint de verdade via `TestClient`
      (`tests/test_assistente_sinalizacoes.py`).
- [ ] Rodar o job de verdade contra dados reais e calibrar limiares
      (`minimo`/`dias` de cada detector) com o que a equipe realmente
      pergunta/reclama — não testável neste ambiente sem banco real.

`POST /assistente/observar/rodar-agora` (admin, `_exigir_admin`) roda o
job (`job.rodar()`) na hora em vez de esperar o agendamento diário —
existe só pra testar/depurar, nunca chamado pelo fluxo normal. Botão
correspondente na tela `/assistente/documentos`.

---

## 9. Fase 6 — Capacidade E, ensinar (modo professor) · **feito**

Onboarding guiado: leva quem é novo no time pelos documentos marcados
como trilha, um de cada vez, num tom didático. Reaproveita a base de
procedimentos que já existe (Fase 3) como conteúdo — nenhum sistema de
conteúdo novo, só uma posição opcional em cada documento
(`documento.ordem_onboarding`, índice único parcial — nunca duas etapas
na mesma posição). O que não tinha equivalente é o progresso por
pessoa (schema na seção 1, `onboarding_progresso`).

**Módulo** (`app/assistente/capacidade_ensinar.py`) — todas as queries
em SQL literal fixo, garantido por teste de AST:

```python
def total_etapas() -> int: ...
def etapa_por_ordem(ordem: int) -> dict | None:
    """Documento daquela posição -- concatena os blocos (Fase 3) na
    ordem original, porque não existe texto bruto salvo."""
def progresso_atual(usuario_id: int) -> dict | None:
    """None = nunca começou. {"etapa_atual": N, "concluido": bool} caso
    contrário."""
def iniciar_ou_retomar(usuario_id: int) -> dict:
    """Cria na etapa 1 se nunca começou; se já tem progresso (mesmo
    concluído), retoma dali -- nunca reinicia por cima."""
def avancar(usuario_id: int) -> dict:
    """+1 etapa; sem próxima na trilha, marca concluído em vez de
    apontar pra uma etapa que não existe."""
```

**Gatilho** (`app/assistente/router.py`), checado com a mesma
prioridade do resumo — antes de achar/explicar/genérico:

- Começar/retomar: `_eh_pedido_de_ensinar_iniciar` — palavra-chave
  ("onboarding", "modo professor") ou "sou novo(a)" combinado com
  "time"/"equipe"/"aqui" (sozinho é frase comum demais pra virar
  gatilho).
- Continuar: `_eh_pedido_de_ensinar_continuar` — só a frase "próxima
  aula"/"continuar aula"/"próxima etapa" (distinta o bastante pra não
  colidir com "qual o próximo passo do chamado?" ou conversa normal), e
  só é checada quando a pessoa já tem uma trilha em andamento
  (`progresso_atual` não-`None` e não concluída). Depois de concluído o
  onboarding, "próxima aula" deixa de ser gatilho especial e vira
  pergunta normal (achar → explicar → genérico), sem tratamento
  diferente.

Sem etapa cadastrada (`total_etapas() == 0`) ou trilha já concluída:
resposta fixa (sem chamar o modelo), mesmo princípio da regra 3 —
código decide quando não há o que ensinar, o modelo só escreve quando
há conteúdo de verdade pra explicar. Havendo etapa: streaming normal via
`completar_stream`, com `prompts/sistema_professor.md` como prompt de
sistema e o conteúdo da etapa (título + blocos concatenados) como
mensagem de usuário — o modelo nunca recebe a pergunta original da
pessoa nessa capacidade, só o material a ensinar.

**Admin** define a trilha na tela `/assistente/documentos` — campo
numérico "posição no onboarding" por documento
(`PATCH /assistente/documentos/{id}/onboarding`,
`{"ordem_onboarding": int | null}`), `null` remove da trilha. Posição
duplicada devolve `400` (checado antes do `UPDATE`, mesmo padrão de
`main.py:criar_carimbo` pro nome duplicado — nunca deixa o índice único
do banco estourar como exceção genérica).

**Critério de aceite:**

- [x] `capacidade_ensinar.py` só usa SQL literal fixo — teste de AST
      (`test_capacidade_ensinar_so_usa_sql_literal_fixa`).
- [x] "Próxima aula" só dispara com trilha em andamento, nunca em
      conversa normal ou depois de concluído — coberto com mock de
      `TestClient` simulando os três casos (iniciar, continuar, sem
      trilha/concluído).
- [x] Progresso nunca reinicia sozinho por cima de uma trilha em
      andamento ou já concluída (`test_iniciar_ou_retomar_ja_tinha_progresso_nao_reinicia`).
- [ ] Rodar com uma trilha real (POPs marcados de verdade) e validar
      com alguém realmente novo no time se o tom didático funciona —
      não testável neste ambiente sem pessoa de verdade nem conteúdo
      real.

---

## 10. Fase 7 — Capacidade F, checkin diário · **feito**

Na primeira vez que a pessoa loga no dia, o widget consulta
`GET /assistente/checkin/pendente` — se `true`, abre sozinho e chama
`POST /assistente/checkin/iniciar`. O servidor monta o contexto (dado
real de ontem, SQL — regra 3, nunca o modelo calcula) e manda o modelo
redigir um resumo curto + a pergunta "o que você pretende fazer hoje?".
A resposta a essa pergunta é a próxima mensagem que a pessoa mandar,
seja lá qual for o texto — capturada com prioridade máxima em
`POST /assistente/pergunta`, antes até do gatilho de resumo, então
nenhuma outra capacidade tenta "entender" essa mensagem. Confirmação é
um texto fixo ("Anotado! Bom trabalho hoje. 🙂"), sem chamar o modelo
de novo — mesmo princípio de `capacidade_achar.py`, código decide o
texto final quando não há nada de fato pra gerar.

**Contratos:**

```
GET /assistente/checkin/pendente
→ { "pendente": true }
```
Nunca 401/403 (mesmo espírito de `/elegivel`) — sem sessão elegível,
`pendente` é sempre `false`.

```
POST /assistente/checkin/iniciar
```
Mesmo formato de stream SSE de `/pergunta` (`inicio`/`texto`/`fim`),
`capacidade: "checkin"`. Marca `checkin_diario` como enviado ao
terminar — por isso só dispara uma vez por dia, mesmo que a pessoa
recarregue a página várias vezes.

**Memória fechando o ciclo:** o contexto de amanhã inclui o que a
pessoa concluiu ontem (`tarefas`, SQL) **e** o que ela tinha dito, no
checkin de ontem, que pretendia fazer (`checkin_diario.resposta_usuario`
de dois dias atrás relativo a hoje) — o modelo compara os dois num tom
de espelho gentil, nunca de cobrança.

Regra 8 do CLAUDE.md do assistente (mesma da capacidade D): a memória é
da pessoa, nunca sobre a pessoa. Não existe (e não pode existir) uma
consulta que devolva o plano de mais de uma pessoa — nem pra admin, nem
agregado.

**Critério de aceite:**

- [x] `capacidade_checkin.py` só usa SQL literal fixo e nunca importa
      `llm.py` — testes de AST
      (`test_capacidade_checkin_so_usa_sql_literal_fixa`,
      `test_capacidade_checkin_nao_importa_nada_de_llm`).
- [x] A próxima mensagem, qualquer que seja o texto, é capturada como
      resposta do checkin quando há um resumo enviado sem resposta
      ainda — testado via `TestClient` na rota de verdade
      (`test_router_intercepta_resposta_quando_aguardando_checkin`).
- [x] Um registro por `(usuario_id, data)` (índice único) garante que
      só dispara uma vez por dia.
- [ ] Rodar com gente de verdade por alguns dias e ver se o tom do
      resumo soa natural (não robótico, não de cobrança) — não
      testável neste ambiente sem uso real acumulado.

---

## 11. Widget

- Botão flutuante com avatar (`static/assistente/avatar.svg`, rosto
  simples em gradiente na paleta da marca — `#5B2EE0` → `#B826C9` →
  `#EC4899`), atalho `Alt+A`.
- Streaming palavra a palavra via `fetch` + leitura manual do stream SSE
  (não usa `EventSource`, porque o endpoint é POST).
- CSRF tratado no próprio widget (lê `csrf_token` do cookie e ecoa em
  `X-CSRF-Token`), não depende do patch de `window.fetch` que só existe
  em algumas telas.
- Elegibilidade checada via `GET /assistente/elegivel` — hoje só
  `perfil = 'admin'` vê o botão.
- Estado de erro visualmente distinto (fundo vermelho claro) do estado de
  carregamento (cursor piscando).
- Sem `localStorage` de histórico — cada abertura do widget começa vazia,
  o histórico de verdade é `assistente_log` no servidor.
- Chip de sugestão "Resumo da semana" no estado vazio do painel.
- **Fase 3:** fontes da capacidade B viram chips clicáveis abaixo da
  resposta (`.ops-fonte-chip`) — clicar expande uma prévia curta do
  trecho (`.ops-fonte-previa`), sem navegar pra lugar nenhum. O texto
  da resposta em si não cita número de trecho (removido a pedido — lia
  mal, tipo "às terças e quintas [2] [4]"); quem quer conferir a fonte
  clica no chip. Testado visualmente num harness local com stream
  simulado (screenshot do fluxo completo: pergunta → resposta → chips →
  prévia expandida).
- **Fase 5:** sino no cabeçalho (`#ops-sino`) com badge de não vistas
  (também espelhado no ícone flutuante, `#ops-badge`), poll a cada 2min
  via `GET /assistente/sinalizacoes/contagem`. Clicar abre a lista
  (`GET /assistente/sinalizacoes`) no lugar do chat; abrir marca as
  sinalizações como vistas (`POST .../visualizar`). Cada item tem três
  botões de feedback (👍 útil, 👎 não útil, 🔕 não me avise mais assim —
  `POST .../feedback` com `1`/`-1`/`-2`); depois de responder, os botões
  desabilitam e mostram "Obrigado!", igual ao feedback de resposta do
  chat. Testado visualmente com stream/lista simulados (screenshot do
  badge, da lista e do feedback já dado).
- **Fase 6:** chip "🎓 Começar onboarding" no estado vazio do painel,
  ao lado do "📊 Resumo da semana" — manda a frase-gatilho de
  `_eh_pedido_de_ensinar_iniciar`, sem UI dedicada além disso (a aula
  aparece como mensagem normal do assistente, "próxima aula" é só
  digitar como qualquer pergunta).
- **Fase 7:** ao carregar, consulta `GET /assistente/checkin/pendente`
  — se `true`, abre o painel sozinho (sem clique), remove os chips de
  sugestão e chama `POST /assistente/checkin/iniciar`
  (`iniciarCheckinDiario`), que reaproveita o mesmo parser de stream SSE
  de `enviarPergunta` (extraído em `consumirStreamSSE`). Falha nesse
  fetch some em silêncio (sem bolha de erro) — é proativo, a pessoa não
  pediu nada, não faz sentido incomodar com erro de algo que ela nem
  sabia que ia acontecer.
