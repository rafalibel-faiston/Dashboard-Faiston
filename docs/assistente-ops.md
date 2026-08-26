# Assistente OPS — Faiston OPS (Dashboard de Apontamento)

Contexto permanente do assistente embutido neste sistema. Leia antes de
qualquer alteração em `app/assistente/`, `static/assistente/` ou nas
rotas `/assistente/*` de `main.py`.

Este documento é a adaptação, ao schema real deste repositório, de uma
especificação genérica escrita originalmente para outro sistema Faiston
(logística/estoque). Os exemplos de domínio abaixo (tarefas, projetos,
clientes, despacho técnico) são os de verdade — não invente estoque,
lote, remessa ou CFOP, esse vocabulário não existe aqui.

## O que é

Assistente de IA embutido no **Faiston OPS** (este dashboard de
apontamento de projetos e despacho técnico). Usuário: equipe interna —
backoffice de projetos, N2 de campo, gestão, diretoria.

Seis capacidades, quatro reativas, uma proativa e uma guiada:

| Capacidade | O que resolve | Como funciona |
|---|---|---|
| **A · Achar** | Procurar informação no sistema (status de tarefa, cliente, atividade de campo, carimbo de atendimento) | Modelo escolhe uma função de um catálogo fixo; o backend executa a consulta |
| **B · Explicar** | Dúvida de procedimento e fluxo (como abrir um chamado, como fechar uma atividade N2) | Busca híbrida nos documentos internos + resposta citando a fonte |
| **C · Resumir** | Montar relatório semanal (o que hoje já existe parcialmente em `/api/ia/insights`) | Modelo redige em cima de agregado já calculado pelo backend |
| **D · Observar** | Repetição, retrabalho e pendência esquecida em tarefas/atividades | Job diário detecta o padrão em SQL; o modelo só escreve o aviso |
| **E · Ensinar** | Onboarding de quem é novo no time — trilha guiada, uma etapa de cada vez | Modelo ensina em cima do conteúdo de um documento já indexado; progresso guardado por pessoa |
| **F · Checkin diário** | Resumir o dia anterior e perguntar o plano de hoje, na 1ª vez que a pessoa loga | Assistente abre sozinho (uma vez por dia), resume ontem em SQL, guarda a resposta como memória pro checkin de amanhã |

**Todas as fases implementadas** (log + genérico, resumo semanal,
explicar via documento indexado, achar por catálogo fixo — incluindo
busca de carimbo —, observar desligado por padrão, ensinar). Ver
`docs/assistente-spec.md` pros critérios de aceite de cada uma,
incluindo o que falta validar (números do resumo, conteúdo real pra
testar a capacidade B, alinhamento com a liderança antes de ligar a
capacidade D).

## Stack real deste repositório

- **Backend:** FastAPI (o mesmo `main.py` monolítico já existente), Python 3.11+
- **Banco:** PostgreSQL (Neon), acesso via `psycopg2` **síncrono** — o
  resto do app inteiro usa esse padrão (uma conexão por chamada, sem
  pool), e o módulo do assistente reaproveita o mesmo padrão em vez de
  introduzir `asyncpg` isolado. Só a chamada ao modelo (`llm.py`) é
  assíncrona de verdade; as funções de banco síncronas do assistente são
  chamadas de dentro do endpoint async via `run_in_threadpool`.
- **Modelo:** compatível com OpenAI via Groq. Hoje usa a mesma
  `GROQ_API_KEY` que `/api/ia/insights` já usa em `main.py` (fallback se
  `LLM_API_KEY` não estiver setada) — ver `app/assistente/llm.py`.
- **Front:** JS puro, widget flutuante incluído via `<script>` nas telas
  autenticadas (mesmo padrão de `faiston-erros.js`), streaming via SSE.
- **Hospedagem:** Railway (mesma do resto do app, ver `Procfile`).

## Regras invioláveis

1. **O assistente só lê.** Nenhuma capacidade cria registro, altera
   status ou dispara fluxo em tabela de negócio (`tarefas`, `projetos`,
   `clientes`, `status_atividades`, etc.). A única escrita permitida é em
   `assistente_log` e nas tabelas de documento (`documento`/
   `documento_chunk`, via `POST /assistente/documentos`, admin-only).
2. **Resposta de procedimento sem fonte é bug.** Capacidade B sempre
   carrega os documentos de origem. Sem trecho relevante, a resposta é
   "não encontrei isso na base" — nunca conhecimento geral do modelo.
3. **O modelo nunca calcula número.** Todo valor numérico vem do
   PostgreSQL já calculado (`SUM`, `COUNT`, `percentile_cont`, etc.). O
   modelo só redige em cima do que recebeu.
4. **Nada de provedor no código.** URL, chave e modelo vêm de
   `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_TIMEOUT_S`. O
   único arquivo que conhece o formato da API é `app/assistente/llm.py`.
5. **A consulta roda com a permissão de quem perguntou.** A sessão vem do
   mesmo cookie `faiston_token` e da mesma tabela `sessoes` que autentica
   o resto do OPS (`app/assistente/db.py`) — nunca um usuário técnico
   privilegiado.
6. **Toda pergunta vai para o log**, antes de responder, sempre —
   `app/assistente/log.py:criar_pergunta`, chamado antes de qualquer
   chamada ao modelo.
7. **Testes contra banco de teste, nunca produção.** Vale a mesma regra
   do resto da suíte (`tests/conftest.py`, `TEST_DATABASE_URL`).
8. **A capacidade D é assistente, não vigilância.** A sinalização vai
   para a própria pessoa que gerou a atividade, e para mais ninguém. Não
   existe endpoint, tela ou relatório que mostre a um gestor o que cada
   pessoa repetiu, quanto tempo levou ou quantas vezes errou. Combinar
   isso com a liderança **antes** de ligar a capacidade D é pré-requisito
   (pergunta ainda em aberto — ver seção "Perguntas em aberto" abaixo).
9. **A detecção de padrão nunca passa pelo modelo.** Contagem, mediana,
   percentil e comparação são SQL puro. O modelo só redige o aviso.

## Convenções de código

- **Sem LangChain, sem LlamaIndex.** SDK oficial da OpenAI e SQL direto.
- Módulo do assistente é async onde precisa ser (streaming, chamada ao
  modelo); banco continua síncrono, decisão deliberada pra não duplicar
  infraestrutura de conexão só pra este módulo.
- Type hints em toda função pública. Pydantic para os contratos
  (`app/assistente/schemas.py`).
- Prompts de sistema em `.md` separado, versionado, nunca embutidos em
  string no meio da lógica (`app/assistente/prompts/`).
- Nomes de domínio em português (`assistente_log`, `documento_chunk`),
  estrutura de código em inglês.
- Toda chamada ao modelo tem timeout (`LLM_TIMEOUT_S`) e tratamento de
  falha — o assistente cair não pode derrubar a tela do OPS. `llm.py`
  nunca lança `KeyError` na importação por falta de env var; só na hora
  de usar.
- Rotas do assistente (`/assistente/*`) passam pelo mesmo
  `CSRFMiddleware` que protege `/api/*` em `main.py` — qualquer novo
  endpoint de escrita precisa continuar dentro desse prefixo pra herdar a
  proteção.

## Estrutura real

```
app/
  assistente/
    __init__.py
    router.py          # endpoints FastAPI + rotas estáticas do widget
    schemas.py          # Pydantic (PerguntaRequest, FeedbackRequest)
    db.py                # conexão + sessão (reaproveita tabela `sessoes`)
    llm.py               # cliente do modelo, único ponto que fala com a API
    log.py               # gravação em assistente_log
    capacidade_resumir.py # agregado semanal em SQL (Fase 2)
    capacidade_explicar.py # busca híbrida + RRF (Fase 3)
    embeddings.py         # sentence-transformers, local em CPU (Fase 3)
    ingestao.py            # quebra em blocos + parsers .md/.docx/.pdf (Fase 3)
    catalogo.py             # funções de consulta fixas, expostas como tools (Fase 4)
    capacidade_achar.py     # function calling + formatação de resposta (Fase 4)
    capacidade_observar/    # padrão detectado em SQL + redação pelo modelo (Fase 5)
      __init__.py
      detectores.py         # SQL puro, nunca importa llm.py
      redacao.py             # único arquivo do pacote que fala com o modelo
      job.py                  # orçamento (2/semana), cooldown (30 dias), grava sinalizacao
      sinalizacoes.py          # leitura/atualização escopada por usuario_id
    capacidade_ensinar.py   # trilha de onboarding: progresso por pessoa (Fase 6)
    prompts/
      sistema_generico.md
      sistema_resumir.md
      sistema_explicar.md
      sistema_achar.md
      sistema_observar.md
      sistema_professor.md
static/
  assistente/
    widget.js
    widget.css
    avatar.svg
    documentos.html        # tela admin de gestão da base (Fase 3)
docs/
  assistente-ops.md      # este arquivo
  assistente-spec.md      # fases e critério de aceite
```

Sem pasta `migrations/` separada: o schema do assistente é criado por
`app/assistente/db.py:setup_schema()`, chamada em `main.py` logo depois
de `setup_banco()` — o mesmo padrão de `CREATE TABLE IF NOT EXISTS` que o
resto do app já usa, em vez de introduzir um runner de migração novo só
pra este módulo.

**Renomeação (25/08):** o produto sempre se chamou OPS pro usuário final
(título já mostrava "OPS" no cabeçalho do widget), mas o namespace
interno de CSS/JS ainda usava o prefixo `nexo-` (nome de trabalho da
fase inicial). `static/assistente/widget.css` e `widget.js` foram
migrados por completo pra `#ops-*`/`.ops-*` (ids, classes, comentários) —
puramente de nomenclatura, nenhum comportamento mudou. Este arquivo
também foi renomeado de `assistente-nexo.md` pra `assistente-ops.md`.

## Piloto atual

O widget só aparece (client-side, `GET /assistente/elegivel`) **e** só
responde (server-side, checado em toda rota) para os perfis listados na env
var `ASSISTENTE_PERFIS_PILOTO` — lista separada por vírgula (ex.:
`admin,gestor`). Ampliar ou fechar o piloto é mexer só nessa variável, sem
alterar código.

`ASSISTENTE_PERFIS_PILOTO=*` libera para **todos** os perfis (`funcionario`,
`gestor`, `diretor`, `demo`, `admin`, `dev`) — é o valor de quando o piloto
acaba e o assistente vira feature de todo mundo. Sem o `*`, ampliar exigiria
listar cada perfil e lembrar de atualizar a variável a cada perfil novo.

Se a variável não existir, o default é `admin` — some a config, o assistente
fecha em vez de abrir pra geral.

O `*` amplia quem **pergunta**, nunca quem **gerencia a base**: ingestão e
remoção de documento passam por `_exigir_admin`, que exige `perfil='admin'`
de verdade em qualquer configuração de piloto.

## Ordem de implementação

Não pule fases. Cada uma tem critério de aceite em `assistente-spec.md`.

1. **Log e caixa de perguntas — feito.** Resposta genérica em streaming,
   sem acesso a dado do sistema nem a documentos.
2. **Capacidade C — resumir — feito.** Gatilho por palavra-chave (não é
   classificação de intenção de verdade ainda), agregado semanal em SQL,
   texto redigido em cima do JSON. Falta validar com a equipe se os
   números escolhidos são os certos.
   **Correção de segurança (25/08):** a versão original devolvia o
   agregado da equipe inteira (nome e número de todo mundo) pra quem
   perguntasse — passou despercebido enquanto o piloto era só `admin`,
   mas violava a regra 5 (a consulta roda com a permissão de quem
   perguntou) assim que um perfil `funcionario` ganhou acesso ao
   assistente. `montar_agregado_semana()` agora exige `usuario_id`/`nome`
   e escopa toda query por quem perguntou — testado com AST
   (`test_capacidade_resumir_toda_query_de_tarefas_escopada_por_usuario`)
   pra não voltar a acontecer sem que o teste quebre.
3. **Capacidade B — explicar — pipeline pronto, sem conteúdo real ainda.**
   Ingestão (`.md`/`.docx`/`.pdf`), embedding local, busca híbrida com
   RRF e recusa honesta sem fonte, tudo implementado e testado com
   documento sintético. Falta ingerir POPs de verdade
   (`POST /assistente/documentos`, admin) e rodar o critério de aceite
   (20 perguntas com resposta + 10 sem) — ver `assistente-spec.md`.
4. **Capacidade A — achar — feito.** Catálogo fixo (5 funções:
   `minhas_tarefas`, `tarefas_por_cliente`, `escala_n2_do_dia`,
   `atividades_campo_pendentes`, `buscar_carimbo`), function calling
   decide se a pergunta é do tipo achar, texto final montado por código.
   **Correção de segurança (25/08):** `tarefas_por_cliente` e
   `atividades_campo_pendentes` liam a base inteira, sem escopo por
   pessoa — mesma causa raiz da correção da capacidade C, e mesma
   janela (invisível enquanto o piloto era só `admin`, vazamento real
   assim que `funcionario` entrou). Agora não-admin vê só o que é dele
   (tarefa por `usuario_id`; atividade de campo por `tecnico`/
   `n2_responsavel`, que são texto livre — `status_atividades` não tem
   FK de usuário), e admin mantém a visão de gestão que já tem nas
   telas normais. `escala_n2_do_dia` fica de fora de propósito: escala
   de plantão é informação compartilhada por natureza. Coberto por
   `test_catalogo_nao_expoe_tarefa_de_outra_pessoa_pra_nao_admin`.
   `buscar_carimbo` busca na tabela `carimbos` que já existia no OPS
   (textos prontos de atendimento/acionamento cadastrados pelos próprios
   funcionários, `/api/carimbos` em `main.py`) — mesma regra de
   visibilidade por time que o CRUD original, conteúdo devolvido ao pé
   da letra (o modelo nunca reescreve um carimbo). As outras quatro
   foram escolhidas pelo domínio, não pelo log real (sem acesso a ele
   neste ambiente) — ajustar depois de ver o que a equipe pergunta de
   verdade.
   **Melhoria de UX (25/08):** resposta de `buscar_carimbo` (um único
   resultado) ganhou botão "📋 Copiar carimbo" no widget, logo abaixo da
   bolha — copia só o conteúdo do carimbo pra área de transferência, sem
   o cabeçalho `Carimbo "TÍTULO" (CATEGORIA):` que é metadado nosso, não
   texto pra colar no atendimento. Detecção 100% client-side
   (`static/assistente/widget.js:extrairConteudoCarimbo`, regex sobre o
   texto já formatado por `formatar_resposta()`) — nenhuma mudança de
   contrato entre backend e front, nenhum campo novo na resposta.
5. **Capacidade D — observar — implementada, desligada por padrão.** Três
   detectores em SQL puro (`repeticao_identica`, `retrabalho`,
   `pendencia_parada`), rodando de `tarefas`/`tarefa_historico` (não
   precisou de tabela nova). Orçamento de no máximo 2 sinalizações por
   pessoa por semana, cooldown de 30 dias por (pessoa, detector,
   assinatura), feedback `-2` desliga o detector pra aquela pessoa pra
   sempre — tudo isso decidido em SQL/Python, nunca pelo modelo; o
   modelo só redige o texto em cima do achado já detectado (ou descarta,
   se não tiver nada de fato acionável pra dizer). Job diário agendado
   via APScheduler (`ASSISTENTE_OBSERVAR_ENABLED=1`,
   `ASSISTENTE_OBSERVAR_HORA`, default 3h), **desligado por padrão** —
   ver "Perguntas em aberto" abaixo antes de ligar em produção. Widget
   tem sino no cabeçalho com contador de não vistas + lista com feedback
   (útil / não útil / não me avise mais assim). Sinalização é sempre da
   pessoa, nunca sobre a pessoa: todo endpoint e toda função de leitura
   são escopados por `usuario_id`, sem view agregada nem por gestor.
   `POST /assistente/observar/rodar-agora` (admin, botão na tela
   `/assistente/documentos`) dispara o job na hora, só pra testar sem
   esperar o agendamento diário. `POST /assistente/observar/popular-dados-teste`
   / `.../limpar-dados-teste` (mesma tela, mesmo gate) geram/removem
   tarefas sintéticas pra quem clicou, marcadas com
   `cliente = 'Cliente Teste Observar'` — única exceção deliberada à
   regra 1 (só lê) neste módulo, isolada em
   `capacidade_observar/dados_teste.py`, nunca chamada por nenhuma
   capacidade conversacional.
   `POST /assistente/demo/popular` / `.../demo/limpar` (mesma tela,
   mesmo gate) são mais amplos: criam até 4 funcionários fictícios
   (`app/assistente/dados_demo.py`, login `demo.*`) e ~7 tarefas
   variadas cada (feita/em andamento/aberta, tipos e clientes
   misturados) — pra ver o sistema com dado de verdade, não só pra
   testar a capacidade D. `limpar` remove as tarefas geradas e desativa
   (não apaga) os usuários demo.
6. **Capacidade E — ensinar (modo professor) — feito.** Trilha de
   onboarding guiada: reaproveita documento já indexado (Fase 3) como
   conteúdo de cada etapa — admin marca a posição de um documento na
   trilha (`documento.ordem_onboarding`, campo "posição no onboarding"
   na tela `/assistente/documentos`) em vez de um sistema de conteúdo
   novo. Progresso por pessoa em `onboarding_progresso`, sempre retoma
   de onde parou. Gatilho por palavra-chave: "onboarding"/"modo
   professor"/"sou novo(a) no time" começa ou retoma a trilha; "próxima
   aula" (frase distinta o bastante pra não colidir com conversa normal)
   avança — só conta como gatilho quando a pessoa já tem uma trilha em
   andamento. O modelo ensina em cima do conteúdo da etapa recebida (tom
   didático, `prompts/sistema_professor.md`), nunca inventa procedimento
   fora do que foi passado. Chip "🎓 Começar onboarding" no estado vazio
   do widget.
7. **Capacidade F — checkin diário — feito.** Na 1ª vez que a pessoa
   loga no dia (`GET /assistente/checkin/pendente`, checado uma vez ao
   carregar o widget), o painel abre sozinho e o assistente resume o
   que ela concluiu ontem (SQL, `capacidade_checkin.py:contexto_ontem`)
   junto do que ela tinha dito que ia fazer (a resposta do checkin
   anterior, guardada em `checkin_diario`), e pergunta o plano de hoje.
   A próxima mensagem que a pessoa mandar — não importa o texto — vira
   automaticamente a resposta do checkin (interceptada com prioridade
   máxima em `POST /assistente/pergunta`, antes de resumo/ensinar/achar/
   explicar), confirmada com um "Anotado!" fixo, sem gastar chamada de
   modelo. Mesma regra 8 da capacidade D: a memória é só da pessoa, sem
   view por gestor. Um registro por (usuario_id, data) garante que só
   dispara uma vez por dia mesmo recarregando a página várias vezes.

## Perguntas em aberto (não inventar, confirmar antes de avançar)

Herdadas da spec original, ainda sem resposta — travam as fases 2 a 5,
não a Fase 1:

- Quais números entram no resumo semanal hoje (Fase 2)? `/api/ia/insights`
  já calcula tickets por funcionário/cliente/prioridade — é essa a base,
  ou tem outra planilha/reunião que compila algo diferente?
- Existe POP/procedimento já escrito pra usar como primeiro documento de
  teste da capacidade B? (a tela de upload já está pronta em
  `/assistente/documentos` — atalho ⚙ no cabeçalho do widget — só falta
  o conteúdo)
- Quais tipos de tarefa mais se repetem, na percepção da equipe? Calibra
  o limiar inicial dos detectores da capacidade D (hoje: 5+ vezes em 5
  dias pra `repeticao_identica`, 4+ edições em 7 dias pra `retrabalho`).
- A capacidade D foi combinada com a liderança? A regra de que a
  sinalização não sobe pro gestor precisa estar acordada antes de ligar
  `ASSISTENTE_OBSERVAR_ENABLED=1` de verdade — está implementada e
  testada, mas desligada até esse alinhamento acontecer.
- Ampliar o piloto (hoje só `admin`) pra quais perfis/cargos, e quando?
- Quais documentos (e em que ordem) formam a trilha de onboarding da
  capacidade E? Hoje nenhum documento está marcado — a trilha fica vazia
  até o admin escolher pela tela `/assistente/documentos`.
