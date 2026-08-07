# Conector MCP do Faiston Ops — MC

Permite que o Claude grave MC no Ops. Junto com o conector do Outlook, fecha o
loop: o Claude acha o e-mail de KICK-OFF com a planilha nova, extrai a MC e
grava aqui — sem passo manual.

```
Outlook (conector pronto)          Faiston Ops (este conector)
  e-mail de KICK-OFF        →          mc_importar
  + planilha .xlsb                     mc_listar / mc_detalhe
                                       contratos_buscar
```

## Como conectar no Claude

1. **Settings → Connectors → Add custom connector**
2. URL: `https://dashboard-faiston-teste.up.railway.app/mcp`
   (em produção, o domínio de produção + `/mcp`)
3. O Claude descobre o resto sozinho (metadata, registro de cliente).
4. Vai aparecer a tela de login do Faiston Ops. **Entre com o seu login normal
   do Ops** — o do próprio sistema, o mesmo que você usa no dashboard.
5. Pronto. As tools aparecem na lista do Claude.

O token fica amarrado ao usuário que fez login: as permissões são as do perfil
dessa pessoa, e o histórico de ingestões (`mc_ingestoes_log`) grava o nome dela.
Perfil `demo` consegue ler mas não importar, igual na tela.

Token de acesso vale 30 dias e é renovado automaticamente por refresh token
(180 dias). Depois disso, é refazer o login.

## As tools

| Tool | O que faz | Precisa de |
| --- | --- | --- |
| `mc_importar` | Grava uma MC. Idempotente por contrato+ano. | admin, gestor, diretor |
| `mc_listar` | Lista MC's com status e totais. Filtra por status/contrato. | + demo |
| `mc_detalhe` | Uma MC com todas as linhas, totais declarados × somados e histórico. | + demo |
| `contratos_buscar` | Procura contrato por parte do nome/código, em `contratos_gestao` e `forecast_projetos`. | + demo |

As descrições que o Claude lê dizem explicitamente para **copiar os números da
planilha, não recalcular**, e para **não inventar contrato nem linha de custo**.
`mc_importar` pede os totais que a planilha declara justamente para o Ops
conferir contra a soma das linhas.

Quando uma MC entra mas não fecha (divergência de total, contrato não
encontrado), a tool **não** retorna erro: retorna sucesso com um campo
`atencao` explicando o que precisa de conferência humana. Erro faria o Claude
tentar de novo; o que se quer é que ele avise a pessoa.

## Autenticação

### OAuth 2.1 (o caminho do Claude)

O fluxo de conector personalizado do Claude assume OAuth 2.1 — faz descoberta,
registro dinâmico de cliente e o code flow com PKCE. Não existe opção "sem
autenticação" na interface dele, e o suporte a header fixo ainda é beta
restrito. Por isso o Ops se comporta como authorization server:

```
GET  /.well-known/oauth-protected-resource     descoberta do resource server (RFC 9728)
GET  /.well-known/oauth-authorization-server   descoberta do AS (RFC 8414)
POST /mcp/oauth/register                       registro dinâmico (RFC 7591)
GET  /mcp/oauth/authorize                      tela de login do Ops
POST /mcp/oauth/authorize                      valida e devolve o code
POST /mcp/oauth/token                          code/refresh → access token
POST /mcp                                      o MCP (JSON-RPC 2.0)
```

O que é aplicado:

- **PKCE S256 obrigatório.** `plain` não é oferecido nem aceito — sem isso, um
  code interceptado no redirect seria trocável por token.
- **`redirect_uri` só https**, exceto `localhost` (desenvolvimento). Um redirect
  http exposto entrega o code para quem estiver no caminho.
- **`redirect_uri` tem que estar registrada** naquele `client_id`.
- **Code de uso único**, 120 s de validade, marcado como usado antes de emitir
  o token (replay não gera um segundo token).
- **Refresh com rotação**: usar o refresh revoga o anterior.
- **Tokens no banco como SHA-256.** Se o banco vazar, não sai credencial usável.
- **401 com `WWW-Authenticate`** apontando o resource metadata — é assim que o
  cliente sabe onde começar o OAuth em vez de só falhar.

### Bearer fixo (fora do Claude)

Para chamar de um script ou de outra automação:

```bash
MCP_API_TOKEN=<segredo longo>        # gere com: openssl rand -base64 48
MCP_API_TOKEN_USUARIO=bruna.silva    # login do Ops que essa automação representa
```

Depois: `Authorization: Bearer <MCP_API_TOKEN>`. Sem `MCP_API_TOKEN` definida,
esse caminho fica desligado — nenhum token estático é aceito por padrão. Se
`MCP_API_TOKEN_USUARIO` não for definida, cai no primeiro admin ativo, o que
funciona mas piora a auditoria: prefira nomear o usuário.

## Implementação

`mcp_faiston.py`, montado pelo `main.py` no fim do arquivo. O protocolo é
implementado à mão (JSON-RPC 2.0 sobre um POST) e **não há dependência nova**.

Isso foi decisão consciente: o SDK oficial (`mcp` 2.0) exige
`pydantic>=2.12`, e o Ops roda `pydantic==2.7.1` com `fastapi==0.111.0`. Subir
o pydantic por causa de um módulo acessório arriscaria o app inteiro. O
subconjunto que um conector precisa é pequeno — `initialize`, `tools/list`,
`tools/call`, `ping` e as notificações — então sai mais seguro implementar aqui.

Versões de protocolo: ecoa a que o cliente pedir se for conhecida
(`2025-06-18`, `2025-03-26`, `2024-11-05`), senão responde `2025-06-18`.

As tools chamam **as mesmas funções** que os endpoints `/api/mc/*`
(`mc_processar_importacao`, `mc_consultar_lista`, `mc_consultar_detalhe`), então
os dois caminhos de ingestão não podem divergir na conferência de totais nem no
status.

A montagem está dentro de um `try/except`: se o MCP falhar ao subir, o Ops sobe
de todo jeito. Um módulo acessório não pode derrubar o deploy.

Rotas fora de `/api/*` de propósito: o `CSRFMiddleware` não se aplica (um
cliente MCP não tem cookie para montar o header) e a separação fica visível.

### Tabelas

`mcp_oauth_clients`, `mcp_oauth_codes`, `mcp_oauth_tokens` — criadas sob demanda
por `_ensure_mcp_tables()`, mesmo padrão do resto do sistema.

## Testes

`tests/test_mcp.py` — 45 testes, simulando o que o Claude faz: descoberta,
registro, code flow com PKCE, troca por token, e as chamadas JSON-RPC. Cobre
também o que **não** pode passar: token forjado, code reutilizado,
`code_verifier` errado, `redirect_uri` não registrada, `redirect_uri` http,
senha errada, perfil sem acesso, e `demo` tentando importar.

```bash
TEST_DATABASE_URL="postgresql://.../ops_teste" pytest tests/test_mcp.py -q
```

Além dos testes, o servidor foi validado contra o **cliente MCP oficial**
(SDK `mcp` 2.0, num venv separado do projeto): negociação de protocolo,
`tools/list` e `tools/call` com `structuredContent` — implementação
independente conversando com a nossa.

## Antes de ligar em produção

- Só ligar depois de validar em teste, como o resto do módulo.
- O `authorize` aceita qualquer login do Ops com perfil de leitura de MC. Quem
  conectar o conector com o próprio login age com o próprio perfil — é o
  desenho, mas vale saber que qualquer gestor pode conectar.
- Não há tela de gestão de tokens ainda. Para revogar: `UPDATE mcp_oauth_tokens
  SET revogado=TRUE WHERE usuario_id=<id>`. Se isso virar rotina, vale uma tela.
- `HTTPS` é obrigatório na prática: o metadata é montado a partir de
  `X-Forwarded-Proto`, então atrás do proxy do Railway sai `https` correto.
