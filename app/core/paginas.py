"""Helpers das rotas que servem páginas HTML.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi.responses import RedirectResponse

# --- corpo ---
# --- PÁGINAS ---
# Servir o HTML sem checar sessão aqui dependia só do JS de cada página
# (fetch /api/me + redirect) pra afastar quem não devia ver aquela tela --
# esconder o link não é proteção, a rota em si precisa recusar (2026-07-30,
# a pedido do usuário). O dado sensível de verdade já vem só via /api/* (que
# já checa sessão/perfil), mas a página em si -- estrutura, JS, lógica de
# negócio nos comentários -- não deveria ser servida pra quem não tem sessão
# válida, e telas de nível admin não deveriam nem carregar pra funcionário/N2.
def _redirect_login_ou_home(sess):
    """Sem sessão manda pro login; com sessão mas perfil sem acesso a essa
    página manda pra home de quem já está logado, em vez de forçar relogin
    -- mesmo mapeamento perfil/cargo → home usado no pós-login (login.html)."""
    if not sess:
        return RedirectResponse("/")
    if sess["perfil"] in ("admin", "gestor", "demo", "diretor"):
        return RedirectResponse("/dashboard")
    if sess.get("cargo") == "n2":
        return RedirectResponse("/n2")
    if sess.get("cargo") == "sd_supervisor":
        return RedirectResponse("/dashboard")
    if sess.get("cargo") == "sd_operador":
        return RedirectResponse("/service-desk")
    return RedirectResponse("/funcionario")


# Telas autenticadas que embarcam o widget do assistente. `no-cache` obriga
# o navegador a revalidar o HTML, o que garante que uma troca de versão no
# <script src="...widget.js?v=N"> chegue de fato em quem já tinha a página
# aberta antes -- sem isso, o navegador podia seguir servindo o HTML antigo
# (e portanto o widget antigo) por horas. Não é "não cacheia": o ETag do
# FileResponse continua valendo, então página sem mudança responde 304.
_HTML_SEM_CACHE = {"Cache-Control": "no-cache"}
