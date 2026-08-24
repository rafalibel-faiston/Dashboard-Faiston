# Especificação de implementação — Assistente NEXO (Dashboard-Faiston)

Adaptação da spec genérica ao schema real deste repositório. Leia junto
com `docs/assistente-nexo.md`. Implemente fase por fase, na ordem; não
avance sem cumprir o critério de aceite da fase anterior.

---

## 0. Variáveis de ambiente

```bash
LLM_BASE_URL=https://api.groq.com/openai/v1     # default já embutido em llm.py
LLM_API_KEY=gsk_...                              # se ausente, cai para GROQ_API_KEY (já usada por /api/ia/insights)
LLM_MODEL=llama-3.1-8b-instant                   # default já embutido em llm.py
LLM_TIMEOUT_S=30
ASSISTENTE_PERFIS_PILOTO=admin                   # lista separada por vírgula; controla quem vê e quem pode usar
```

Nenhuma dessas é obrigatória pra o app subir — `llm.py` só falha (com
`event: erro` no stream, nunca derrubando o resto do OPS) na hora de
efetivamente chamar o modelo sem chave configurada.

---

## 1. Schema

Implementado (Fase 1):

```sql
CREATE TABLE IF NOT EXISTS assistente_log (
    id              BIGSERIAL PRIMARY KEY,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario_id      INTEGER     NOT NULL REFERENCES usuarios(id),
    pergunta        TEXT        NOT NULL,
    contexto_tela   TEXT,
    capacidade      TEXT,                       -- 'achar' | 'explicar' | 'resumir' | 'fora_escopo' | NULL (fase 1)
    resposta        TEXT,
    fontes          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    respondida      BOOLEAN     NOT NULL DEFAULT false,
    motivo_falha    TEXT,                       -- 'sem_documento' | 'erro_modelo' | 'timeout' | ...
    tokens_entrada  INTEGER,
    tokens_saida    INTEGER,
    latencia_ms     INTEGER,
    feedback        SMALLINT                    -- NULL | 1 útil | -1 não útil
);
```

`usuario_id` referencia `usuarios(id)`, a tabela real de usuário deste
sistema (`perfil` + `cargo` + `time` definem permissão — ver
`app/assistente/db.py:get_session`).

Ainda **não** criado (chega na fase correspondente):

- `documento` / `documento_chunk` (+ extensões `vector` e `pg_trgm`) — Fase 3, capacidade B.
- Nenhuma tabela nova de atividade pra capacidade D: a atividade já existe
  em `tarefas` (com `segundos` acumulado) e `status_atividades` (despacho
  de campo). Fase 5 mapeia essas duas em vez de duplicar — ver seção 8.

---

## 2. Contratos de API (implementados)

### `GET /assistente/elegivel`

Sem autenticação obrigatória (nunca 401/403 — só diz sim/não, é o que o
widget consulta pra decidir se aparece):

```json
{ "elegivel": true }
```

### `POST /assistente/pergunta`

Requer sessão válida (`faiston_token`) com `perfil` em
`ASSISTENTE_PERFIS_PILOTO`; senão 401 (sem sessão) ou 403 (perfil fora do
piloto). Requer também `X-CSRF-Token` batendo com o cookie `csrf_token`
(mesmo middleware de `/api/*`, estendido pra cobrir `/assistente/*`).

Entrada:

```json
{ "pergunta": "qual o fluxo de abertura de chamado N2?", "contexto_tela": "funcionario" }
```

Saída: stream SSE, `media_type="text/event-stream"`.

```
event: inicio
data: {"log_id": 1841, "capacidade": null}

event: texto
data: {"delta": "Ainda estou "}

event: texto
data: {"delta": "em construção..."}

event: fim
data: {"tokens_entrada": 120, "tokens_saida": 38, "latencia_ms": 640}
```

`capacidade` fica `null` na Fase 1 (não existe classificação de intenção
ainda). `event: fontes` só passa a existir na Fase 3 (capacidade B).
Erro do modelo vira `event: erro` com mensagem legível, nunca stack
trace, e grava `motivo_falha` no log.

### `POST /assistente/feedback`

```json
{ "log_id": 1841, "util": true }
```

Só atualiza o log se `usuario_id` do log bater com quem está logado
(`log.py:registrar_feedback`) — 404 caso contrário, nunca deixa um
usuário avaliar a pergunta de outro.

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

## 5. Fase 2 — Capacidade C, resumir (não implementada)

Ponto de partida real: `/api/ia/insights` em `main.py` (linha ~4059) já
calcula, inteiramente em SQL, tickets por funcionário/cliente/prioridade
e tickets atrasados — e já manda isso pro modelo virar texto (hoje 3
insights curtos, via `http.client` direto em vez de `AsyncOpenAI`). A
Fase 2 do assistente deveria:

- Migrar essa lógica pra `app/assistente/capacidade_resumir.py`, reusando
  `llm.py` em vez da chamada HTTP crua duplicada.
- Confirmar com o Rafa se os números de `/api/ia/insights` são de fato o
  que entra no resumo semanal, ou se falta algo (ver pergunta em aberto
  em `assistente-nexo.md`).

Regras da spec original continuam valendo sem alteração: números
exatamente do JSON, nunca calculados pelo modelo; máximo cinco
parágrafos; teste que extrai números do texto gerado e confirma que
todos vêm do JSON de entrada.

---

## 6. Fase 3 — Capacidade B, explicar (não implementada)

Sem mudança de desenho em relação à spec original (ingestão de
`.md`/`.docx`/`.pdf`, blocos de 500–700 palavras com 80 de sobreposição,
`documento`/`documento_chunk` com `pgvector` + `pg_trgm`, busca híbrida
com Reciprocal Rank Fusion, prompt que recusa sem trecho de origem). O
que falta antes de começar: **conteúdo real** — nenhum POP foi indicado
ainda como primeiro documento de teste.

---

## 7. Fase 4 — Capacidade A, achar (não implementada)

Catálogo fixo, sem SQL gerado pelo modelo — mesma regra da spec
original. Consultas candidatas neste schema (a confirmar contra o log
real da Fase 1, não escolher no escuro):

```python
async def status_tarefas_usuario(usuario_id: int) -> dict:
    """Quantas tarefas abertas/em andamento/concluídas a pessoa tem."""

async def tarefas_por_cliente(cliente: str) -> list[dict]:
    """Tarefas em aberto de um cliente específico."""

async def atividades_campo_pendentes(dias: int = 7) -> list[dict]:
    """status_atividades com status != 'concluido' há mais de N dias."""

async def escala_n2_do_dia(data: str) -> list[dict]:
    """Quem está de plantão N2 numa data."""
```

Todas rodam com a permissão de quem perguntou — se o resto do OPS já
restringe visão por `time`/`cargo` em alguma dessas tabelas, a função
aqui replica a mesma restrição, nunca um acesso mais amplo.

---

## 8. Fase 5 — Capacidade D, observar (não implementada)

**Sem tabela `atividade` nova.** A spec original propõe uma tabela
genérica de atividade; neste sistema ela já existe, espalhada em duas:

- `tarefas` — id, `usuario_id`, `cliente`, `status`, `segundos`
  (acumulado, sem início/pausa/fim registrado), `criado_em`,
  `concluido_em`.
- `status_atividades` — despacho técnico de campo, com `tecnico_id`,
  `cliente_id`, `data`, `hora_chegada`/`hora_termino`, `status`.

Isso limita o detector `duracao_anomala` da spec original: não existe
duração "de execução real" isolada por sessão de trabalho, só o
acumulado em `tarefas.segundos` (que pode incluir pausas). Detectores que
não dependem disso (`repeticao_identica` sobre `tarefas.cliente` +
`tipo_atividade_id`, `pendencia_parada` sobre `status_atividades` parado
numa etapa, `padrao_de_calendario`) seguem viáveis sem mudança de
captura. Antes de implementar, decidir com o Rafa se vale desenhar
captura adicional (início/fim por sessão) ou aceitar a limitação.

`sinalizacao` segue como na spec original (cooldown de 30 dias, teto de 2
por semana, `feedback = -2` desliga o detector permanentemente pra
aquela pessoa). Regra 8 do CLAUDE.md (sinalização é da pessoa, nunca
sobre a pessoa pro gestor) não muda — e precisa estar combinada com a
liderança antes de qualquer sinalização real chegar em alguém.

---

## 9. Widget — implementado na Fase 1

- Botão flutuante com avatar (`static/assistente/avatar.svg`, gradiente
  na paleta da marca — `#5B2EE0` → `#B826C9` → `#EC4899`), atalho `Alt+A`.
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
- Fontes da capacidade B (chips clicáveis) — placeholder no CSS
  (`.nexo-feedback`/estrutura de bolha já suporta anexar conteúdo extra),
  implementação real fica pra Fase 3.
