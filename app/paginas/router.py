"""Rotas que servem as páginas HTML.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi import APIRouter, Cookie, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.core.acesso import _perfil_guia, _pode_ver_status_report
from app.core.auth import get_session
from app.core.paginas import _HTML_SEM_CACHE, _redirect_login_ou_home

# --- corpo ---
router = APIRouter()


@router.get("/")
def root(): return FileResponse("static/login.html")

@router.get("/faiston-ops-mark.svg")
def faiston_ops_mark(): return FileResponse("static/faiston-ops-mark.svg", media_type="image/svg+xml")

@router.get("/redefinir-senha")
def redefinir_senha_page(): return FileResponse("static/redefinir-senha.html")

@router.get("/dashboard")
def dashboard(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    # Backoffice (cargo dentro de funcionario) entra aqui só pra ver o Kanban
    # de Cronograma do Status Report -- o próprio index.html restringe a
    # visão a essa única seção pra esse cargo (ver init() em index.html).
    eh_backoffice = sess and sess["perfil"] == "funcionario" and sess.get("cargo") == "backoffice"
    # Supervisor do Service Desk também entra, só com a visão do SD (o
    # index.html esconde o resto). get_session já traz o cargo do cadastro,
    # o mesmo que _redirect_login_ou_home usa -- por isso não há loop.
    eh_sup_sd = bool(sess) and sess["perfil"] == "funcionario" and sess.get("cargo") == "sd_supervisor"
    if not sess or (sess["perfil"] not in ("admin", "gestor", "demo", "diretor") and not eh_backoffice and not eh_sup_sd):
        return _redirect_login_ou_home(sess)
    return FileResponse("static/index.html", headers=_HTML_SEM_CACHE)

@router.get("/funcionario")
def funcionario(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    # Operador do Service Desk tem tela própria. Olha o cadastro, não só a
    # sessão: quem foi movido pro SD depois do login também é desviado.
    from app.service_desk.db import get_session as _sd_sessao
    from app.service_desk.router import CARGOS_SD
    sd = _sd_sessao(faiston_token)
    if sd and sd.get("perfil") == "funcionario" and sd.get("cargo") in CARGOS_SD:
        return RedirectResponse("/dashboard" if sd.get("cargo") == "sd_supervisor" else "/service-desk")
    return FileResponse("static/funcionario.html", headers=_HTML_SEM_CACHE)

@router.get("/n2")
def n2_page(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    if sess["perfil"] != "admin" and sess.get("cargo") != "n2":
        return _redirect_login_ou_home(sess)
    # A tela do N2 é o Status Report inteiro -- fora do time de Projetos não há
    # o que mostrar (ver _pode_ver_status_report). Aqui NÃO dá pra usar
    # _redirect_login_ou_home: pra cargo='n2' a home dele é o próprio /n2 e o
    # redirect entraria em loop; cai no board de tarefas.
    if not _pode_ver_status_report(sess):
        return RedirectResponse("/funcionario")
    return FileResponse("static/n2.html", headers=_HTML_SEM_CACHE)

@router.get("/admin")
def admin_page(): return RedirectResponse("/dashboard?go=admin")

@router.get("/devteam")
def devteam_page(): return RedirectResponse("/dashboard?go=areaDev")


@router.get("/ajuda")
def ajuda_page(perfil: str = "", faiston_token: str = Cookie(None), request: Request = None):
    # Quem manda é a SESSÃO, não a URL: com cookie válido o ?perfil= é
    # sempre recalculado a partir do perfil+cargo do banco, mesmo que já
    # tenha vindo preenchido. Antes o parâmetro era aceito como veio, então
    # /ajuda?perfil=dev na barra do navegador abria o guia de qualquer
    # função — e o link do login mandava o perfil cru, fazendo um N2 cair
    # na aba de Analista.
    # O ?perfil= sozinho só vale sem sessão, que é o caso do e-mail de
    # primeiro acesso (link aberto antes de logar).
    # Preserva os demais parâmetros (ex.: ?destaque=assistente do aviso de
    # novidade) -- senão o redirect os descarta e o link do aviso perde o
    # scroll/abertura automática do widget.
    sess = get_session(faiston_token)
    if sess and sess.get("perfil"):
        destino = _perfil_guia(sess["perfil"], sess.get("cargo", ""),
                               sess.get("perfil_real", ""))
        if destino and destino != perfil:
            outros = "&".join(
                f"{k}={v}" for k, v in request.query_params.items() if k != "perfil"
            ) if request else ""
            return RedirectResponse(f"/ajuda?perfil={destino}" + (f"&{outros}" if outros else ""))
    return FileResponse("static/ajuda.html")

# Rotas antigas (páginas standalone duplicadas) → redirecionam para a SPA,
# abrindo o módulo correto via ?go=. As páginas antigas foram removidas.
@router.get("/gestao")
def gestao_page(): return RedirectResponse("/dashboard?go=gestao")

@router.get("/clientes")
def clientes_page(): return RedirectResponse("/dashboard?go=clientes")

# Módulo Financeiro saiu do menu (2026-09-28, não é mais usado). Dados e API
# continuam -- o Forecast/P&L da Gestão de Projetos segue funcionando.
@router.get("/financeiro")
def financeiro_geral_page(): return RedirectResponse("/dashboard")

@router.get("/forecast")
def forecast_page(): return RedirectResponse("/dashboard?go=forecast")

# Detalhe financeiro por cliente — página própria (mantida)
@router.get("/financeiro/{cid}")
def financeiro_page(cid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    return _redirect_login_ou_home(sess)
