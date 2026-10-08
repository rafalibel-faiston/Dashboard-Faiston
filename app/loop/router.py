"""Integração com o Microsoft Loop (esqueleto).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
import os

from fastapi import APIRouter, Cookie, HTTPException

from app.core.auth import get_session
from app.core.db import get_db

# --- corpo ---
router = APIRouter()


# ─── Integração Microsoft Loop (esqueleto, 2026-08-04) ───────────────────────
# Snapshot periódico de página(s) do Loop via Microsoft Graph API + SharePoint
# Embedded (não é o Graph API de conteúdo do Loop propriamente dito, que segue
# limitado -- é o caminho documentado por baixo, via os arquivos .loop/.fluid
# guardados em SharePoint Embedded). Deliberadamente só-leitura: não existe
# hoje um caminho maduro pra escrever de volta no Loop a partir daqui.
#
# Tudo abaixo é inerte sem configuração -- só ativa quando as env vars
# existirem (mesmo padrão defensivo de _brevo_send/RESUMO_DIARIO_ENABLED).
# Pendente antes de funcionar de verdade (ver wiki
# entities/dashboard-faiston-integracao-microsoft-loop.md):
#   - Entra ID App Registration (Files.Read.All + Sites.Read.All +
#     FileStorageContainer.Selected, consentimento admin) + o passo único
#     `Set-SPOApplicationPermission` via Global Admin.
#   - Descobrir manualmente (uma vez, por página) o drive_id/item_id de cada
#     página-fonte do Loop -- não é redescoberto a cada sync.
#
# Variáveis de ambiente esperadas:
#   LOOP_TENANT_ID, LOOP_CLIENT_ID, LOOP_CLIENT_SECRET  — credenciais do app
#   LOOP_DRIVE_ID                                        — container SPE do Loop
#   LOOP_ITEM_ID_AREA_DEV, LOOP_ITEM_ID_GESTAO_PROJETOS  — item_id por página
#   LOOP_SYNC_ENABLED (default "0"), LOOP_SYNC_MINUTOS (default "20")
LOOP_CHAVES_VALIDAS = ("area_dev", "gestao_projetos")


def _loop_token() -> str:
    """Client-credentials OAuth2 contra o Entra ID -- app-only, não depende
    de login individual de ninguém. Deixa a exceção subir; quem chama
    (_loop_sync_job) já trata e loga."""
    import urllib.request, urllib.parse, json as _json
    tenant_id = os.environ["LOOP_TENANT_ID"]
    body = urllib.parse.urlencode({
        "client_id": os.environ["LOOP_CLIENT_ID"],
        "client_secret": os.environ["LOOP_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return _json.loads(resp.read())["access_token"]


def _loop_exportar_item(token: str, drive_id: str, item_id: str) -> str:
    """Baixa o HTML já convertido pelo próprio Graph (?format=html) -- não
    precisamos parsear o formato .loop/.fluid na mão. drive_id contém '!'
    que precisa ir URL-encoded como %21."""
    import urllib.request
    drive_id_enc = drive_id.replace("!", "%21")
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id_enc}/items/{item_id}/content?format=html"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _loop_sync_job():
    """Job agendado -- busca o snapshot de cada página configurada e grava
    em loop_snapshots. Cada chave é independente: uma falhar não impede as
    outras (commit/rollback por item, não por job inteiro)."""
    if not (os.environ.get("LOOP_TENANT_ID") and os.environ.get("LOOP_CLIENT_ID")
            and os.environ.get("LOOP_CLIENT_SECRET")):
        return  # credenciais ainda não existem -- inerte de propósito
    drive_id = os.environ.get("LOOP_DRIVE_ID", "")
    itens = {
        "area_dev": os.environ.get("LOOP_ITEM_ID_AREA_DEV", ""),
        "gestao_projetos": os.environ.get("LOOP_ITEM_ID_GESTAO_PROJETOS", ""),
    }
    itens = {k: v for k, v in itens.items() if v}
    if not drive_id or not itens:
        print("[loop] LOOP_DRIVE_ID/LOOP_ITEM_ID_* não configurados -- nada para sincronizar")
        return
    try:
        token = _loop_token()
    except Exception as e:
        print(f"[loop] Falha ao autenticar no Graph: {e}")
        return
    conn = get_db()
    if not conn:
        print("[loop] Banco offline -- sync adiado pro próximo ciclo")
        return
    try:
        cur = conn.cursor()
        for chave, item_id in itens.items():
            try:
                html = _loop_exportar_item(token, drive_id, item_id)
                cur.execute("""
                    INSERT INTO loop_snapshots (chave, html_conteudo, atualizado_em, erro)
                    VALUES (%s, %s, NOW(), NULL)
                    ON CONFLICT (chave) DO UPDATE
                    SET html_conteudo = EXCLUDED.html_conteudo,
                        atualizado_em = EXCLUDED.atualizado_em, erro = NULL
                """, (chave, html))
                conn.commit()
                print(f"[loop] Snapshot atualizado: {chave}")
            except Exception as e:
                conn.rollback()
                cur.execute("""
                    INSERT INTO loop_snapshots (chave, atualizado_em, erro)
                    VALUES (%s, NOW(), %s)
                    ON CONFLICT (chave) DO UPDATE SET erro = EXCLUDED.erro, atualizado_em = EXCLUDED.atualizado_em
                """, (chave, str(e)))
                conn.commit()
                print(f"[loop] Falha ao exportar '{chave}': {e}")
        cur.close(); conn.close()
    except Exception as e:
        print(f"[loop] Erro no job de sync: {e}")
        conn.close()


# --- INTEGRAÇÃO MICROSOFT LOOP (esqueleto, 2026-08-04 — ver _loop_sync_job) ---
# Leitura liberada a qualquer sessão válida, sem restrição de perfil --
# decisão do usuário ("a ideia é todo mundo ver"). Pendente: decidir em qual
# tela/aba exata cada chave aparece pra quem não é admin/dev (Área de Dev
# hoje só é alcançável por esse perfil -- ver nota na wiki).
@router.get("/api/loop-snapshot/{chave}")
def loop_obter_snapshot(chave: str, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if chave not in LOOP_CHAVES_VALIDAS:
        raise HTTPException(status_code=404, detail="Chave desconhecida")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT html_conteudo, atualizado_em, erro FROM loop_snapshots WHERE chave = %s", (chave,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return {"configurado": False, "html": "", "atualizado_em": None, "erro": None}
        html, atualizado_em, erro = row
        return {
            "configurado": True,
            "html": html or "",
            "atualizado_em": atualizado_em.strftime("%d/%m/%Y %H:%M") if atualizado_em else None,
            "erro": erro,
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════
# SUPORTE: qualquer usuário logado abre uma solicitação (bug, dúvida,
# pedido de melhoria); só a equipe dev vê/gerencia. É uma thread de
# mensagens (suporte_mensagens) -- dev responde, quem abriu recebe um
# e-mail avisando e responde de volta pelo sistema (2026-09-09; antes
# era um campo de resposta único, sem volta pro usuário).
# ══════════════════════════════════════════════════════════════════
