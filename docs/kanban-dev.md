# Kanban da Área de Dev (Planejamento)

Quadro de tarefas da equipe dev (perfil `dev`) dentro do Faiston OPS: **Área de Dev → Planejamento**.
Usado também pra demandas que não são do OPS — o "projeto" de cada tarefa é a primeira tag.

## Onde está o código

| Parte | Arquivo |
|---|---|
| UI do quadro, foco, drawer, paleta | `static/js/dev-kanban.js` + `static/css/dev-kanban.css` |
| UI da IA | `static/js/dev-kanban-ia.js` |
| UI das conexões | `static/js/dev-kanban-conexoes.js` |
| CRUD das tarefas (legado, `/api/dev-tarefas`) | `main.py` |
| Ranking "Foco do dia" (regras) | `app/devkanban/foco.py` |
| IA (captura, briefing, standup, comando) | `app/devkanban/ia.py` + `router.py` (`/api/dev-ia/*`) |
| Conexões e webhooks | `app/devkanban/conexoes.py`, `eventos.py`, `github.py`, `graph.py` |
| Tabelas/colunas extras | `app/devkanban/db.py` |
| Testes | `tests/test_dev_*.py` |

## Uso

- **Ctrl/⌘ + K**: busca tarefas, filtra por projeto, cria, alterna a visão e **pergunta à IA**. Atalho **n** = nova tarefa.
- **Foco de hoje**: top 5 por regras (atrasada, vence hoje/amanhã, urgência, parada há dias, quase pronta). Sem IA.
- **Quadro / Projetos**: duas visões; arrastar respeita a posição dentro da coluna.
- **Concluído** mostra só os últimos 14 dias (há "mostrar mais").

## IA — "a IA propõe, o backend valida, você confirma"

Usa o cliente de `app/assistente/llm.py` (`LLM_BASE_URL`, `LLM_API_KEY`/`GROQ_API_KEY`, `LLM_MODEL`).

- **Capturar**: texto livre → rascunhos editáveis → criar (marcadas `origem=ia`).
- **Briefing / Standup**: o ranking vem das regras; o modelo só escreve o texto. Sem chave ou com falha do provedor, cai em texto por regras (com aviso).
- **Comando (Ctrl+K → "Perguntar à IA")**: vira ações de uma whitelist — `filtrar`, `mover`, `prioridade`, `adicionar_tag`, `remover_tag`, `renomear_tag`, `atribuir`, `prazo`, `criar`. **Nunca exclui.** Ids, enums e pessoas são validados; nada muda até você confirmar, e a aplicação revalida tudo no servidor.
- Limite de 20 chamadas de IA por minuto por usuário.
- **LGPD**: o texto colado é enviado ao provedor de IA configurado; a tela avisa para não colar dados pessoais sensíveis.

## Conexões (botão **Conexões**)

Variáveis de ambiente (todas opcionais; sem elas o recurso fica desligado e a tela explica):

| Variável | Para quê |
|---|---|
| `GITHUB_TOKEN` | Estado de PR/issue (fine-grained, **somente leitura**: Pull requests, Issues, Checks, Metadata). Sem token: só repositórios públicos. |
| `GITHUB_API_URL` | GitHub Enterprise (padrão `https://api.github.com`). |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | Outlook (cai nas `LOOP_*` se não definidas). Permissões de **aplicativo**: `Calendars.ReadWrite` e `Mail.Read`, com consentimento do admin. **Restrinja as caixas com uma Application Access Policy** — sem isso a permissão vale para todas as caixas do tenant. |
| `GRAPH_API_URL`, `GRAPH_LOGIN_URL` | Nuvens soberanas do Microsoft (opcional). |
| `DEV_HOOKS_PERMITIR_HTTP`, `DEV_HOOKS_PERMITIR_REDE_PRIVADA` | **Só para teste local.** Em produção deixe desligadas (webhook de saída só `https` e só IP público). |

### Webhooks de entrada — `POST /api/dev-hooks/in/{token}`

Público (sem cookie/CSRF); a autenticação é o **token secreto na URL** — trate como senha. Criação no painel (presets: n8n, TiFlux, Faiston Hub, Teams via Power Automate, GitHub).

**Genérico (JSON)** — `acao`: `upsert` (padrão; mesmo `origem_ref` atualiza), `mover` (+`status`), `concluir`, `comentar` (+`comentario`).
Campos: `titulo`, `descricao`, `prioridade` (`urgente|alta|media|baixa`), `prazo` (`AAAA-MM-DD`), `tags[]`, `status`, `link`, `checklist[]`, `origem_ref`.

```bash
curl -X POST 'https://SEU-HOST/api/dev-hooks/in/TOKEN' -H 'Content-Type: application/json' \
  -d '{"titulo":"Chamado #123: VPN caindo","origem_ref":"TKT-123","prioridade":"alta"}'
```

**TiFlux e Faiston Hub** entram por aqui, via n8n (use o nº do chamado/tarefa como `origem_ref`). **Teams**: fluxo do Power Automate/n8n → HTTP POST para uma URL de entrada.

**GitHub** — no repositório: *Settings → Webhooks*: URL do hook, `application/json`, o *Secret* do painel, eventos *Pull requests* e *Issues*. A assinatura `X-Hub-Signature-256` é obrigatória. Regras (a tarefa precisa ter o link do PR/issue no campo **Link**): PR mergeado → Concluído · PR aberto/pronto pra review → Em progresso (se estava em Backlog/A fazer) · PR fechado sem merge → só comentário · issue fechada → Concluído · issue reaberta → A fazer.

### Webhooks de saída

O quadro faz `POST` JSON `{evento, enviado_em, tarefa}` quando uma tarefa é `tarefa.criada`, `.atualizada`, `.movida`, `.concluida` ou `.excluida` (filtro por evento no cadastro).
Cada envio vem assinado: `X-Faiston-Signature: sha256=` + HMAC-SHA256 do corpo com o *secret* do webhook (também `X-Faiston-Event` e `X-Faiston-Delivery`). 1 retentativa em falha; o último status aparece no painel. Proteção SSRF: só `https`, só IP público, sem redirect.
Webhooks de entrada **genéricos não disparam saída** (evita laço saída→n8n→entrada); o do GitHub dispara.

### Outlook

- **Prazo → calendário**: no drawer da tarefa, "Adicionar prazo ao calendário" cria um evento de dia inteiro na caixa do responsável (ou de quem pediu). O evento acompanha mudança de prazo/título e some ao concluir ou excluir.
- **E-mail sinalizado → tarefa**: *Conexões → Outlook* lista os e-mails com bandeirinha; "Criar tarefa" lê o e-mail com a IA e sugere as tarefas (dedupe por id da mensagem).

## Segurança — resumo

- Tudo em `/api/dev-ia/*` e `/api/dev-integracoes/*` exige perfil `dev` + CSRF; o webhook de entrada é a única rota pública (token na URL, limite de 120 chamadas/min por token, corpo ≤ 256 KB).
- Segredos de webhook ficam em texto no banco (precisam ser usados para assinar/validar) e só são exibidos a perfil `dev`.
- A IA não executa nada sozinha; o que ela devolve é sanitizado antes de ser exibido.
