"""Cliente mínimo do Microsoft Graph (permissão de APLICATIVO, sem login do usuário):
eventos de calendário pro prazo das tarefas e leitura de e-mails sinalizados.

Variáveis de ambiente (cai nas do Loop, que usam o mesmo app do Azure):
    GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET
    (opcionais: GRAPH_API_URL e GRAPH_LOGIN_URL, pra nuvens soberanas como Graph US Gov)
Permissões de aplicativo necessárias no Azure (com consentimento do admin):
    Calendars.ReadWrite  (eventos)    Mail.Read  (e-mails sinalizados)
Recomenda-se restringir as caixas acessíveis com uma Application Access Policy
(New-ApplicationAccessPolicy) -- sem isso a permissão vale pra TODAS as caixas do tenant.
"""
import html
import os
import re
import time
from urllib.parse import quote

import requests

BASE = os.environ.get("GRAPH_API_URL", "https://graph.microsoft.com/v1.0").rstrip("/")
LOGIN = os.environ.get("GRAPH_LOGIN_URL", "https://login.microsoftonline.com").rstrip("/")
FUSO = "E. South America Standard Time"     # nome Windows de America/Sao_Paulo (o que o Graph espera)
_tok = {"v": None, "exp": 0.0}


class GraphErro(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _cfg():
    g = lambda a, b: os.environ.get(a) or os.environ.get(b, "")
    return g("GRAPH_TENANT_ID", "LOOP_TENANT_ID"), g("GRAPH_CLIENT_ID", "LOOP_CLIENT_ID"), g("GRAPH_CLIENT_SECRET", "LOOP_CLIENT_SECRET")


def configurado() -> bool:
    return all(_cfg())


def _token() -> str:
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    tenant, cid, secret = _cfg()
    r = requests.post(f"{LOGIN}/{tenant}/oauth2/v2.0/token", timeout=10, data={
        "client_id": cid, "client_secret": secret, "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default"})
    if r.status_code != 200:
        raise GraphErro(r.status_code, "Não consegui autenticar no Microsoft Graph (confira tenant, client id e secret)")
    d = r.json()
    _tok["v"], _tok["exp"] = d["access_token"], time.time() + int(d.get("expires_in", 3600))
    return _tok["v"]


def _req(metodo: str, caminho: str, **kw):
    """Única função que fala HTTP com o Graph (os testes trocam esta)."""
    if not configurado():
        raise GraphErro(503, "Microsoft Graph não configurado neste ambiente")
    r = requests.request(metodo, BASE + caminho, timeout=12, headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"}, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise GraphErro(r.status_code, msg or f"Erro {r.status_code} do Microsoft Graph")
    return r.json() if r.content else {}


def _corpo_evento(titulo: str, dia_iso: str, link: str = "") -> dict:
    from datetime import date, timedelta
    d = date.fromisoformat(dia_iso)
    corpo = f'<p>Prazo da tarefa no Faiston OPS.</p>' + (f'<p><a href="{html.escape(link)}">{html.escape(link)}</a></p>' if link.startswith("http") else "")
    return {
        "subject": f"[Prazo] {titulo}"[:240], "isAllDay": True, "showAs": "free", "categories": ["Faiston OPS"],
        "start": {"dateTime": f"{d.isoformat()}T00:00:00", "timeZone": FUSO},
        "end": {"dateTime": f"{(d + timedelta(days=1)).isoformat()}T00:00:00", "timeZone": FUSO},
        "body": {"contentType": "HTML", "content": corpo},
    }


def criar_evento(mailbox: str, titulo: str, dia_iso: str, link: str = "") -> str:
    return _req("POST", f"/users/{quote(mailbox)}/events", json=_corpo_evento(titulo, dia_iso, link))["id"]


def atualizar_evento(mailbox: str, evento_id: str, titulo: str, dia_iso: str, link: str = "") -> None:
    _req("PATCH", f"/users/{quote(mailbox)}/events/{quote(evento_id, safe='')}", json=_corpo_evento(titulo, dia_iso, link))


def remover_evento(mailbox: str, evento_id: str) -> None:
    try:
        _req("DELETE", f"/users/{quote(mailbox)}/events/{quote(evento_id, safe='')}")
    except GraphErro as e:
        if e.status != 404:          # já não existe: ok
            raise


def emails_sinalizados(mailbox: str, limite: int = 15) -> list[dict]:
    d = _req("GET", f"/users/{quote(mailbox)}/messages", params={
        "$filter": "flag/flagStatus eq 'flagged'", "$top": str(limite),
        "$select": "id,subject,from,receivedDateTime,bodyPreview,webLink"})
    return [{
        "id": m["id"], "assunto": m.get("subject") or "(sem assunto)",
        "de": ((m.get("from") or {}).get("emailAddress") or {}).get("name", ""),
        "recebido": (m.get("receivedDateTime") or "")[:10], "previa": (m.get("bodyPreview") or "")[:200], "link": m.get("webLink", ""),
    } for m in d.get("value", [])]


def _html_para_texto(h: str) -> str:
    """HTML de e-mail -> texto simples. Tags de bloco viram quebra de linha; tags inline (<b>, <a>, <span>)
    somem sem deixar espaço (senão sobra "sexta ." no meio da frase)."""
    h = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<\s*/?\s*(br|p|div|li|tr|table|ul|ol|h[1-6]|blockquote)\b[^>]*>", "\n", h)
    h = re.sub(r"(?i)</\s*(td|th)\s*>", " ", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    t = re.sub(r"[ \t\r\f\v\xa0]+", " ", t)
    return re.sub(r"\n{2,}", "\n", re.sub(r" ?\n ?", "\n", t)).strip()


def texto_email(mailbox: str, msg_id: str) -> dict:
    m = _req("GET", f"/users/{quote(mailbox)}/messages/{quote(msg_id, safe='')}", params={"$select": "subject,from,body,webLink,receivedDateTime"})
    corpo = m.get("body") or {}
    texto = _html_para_texto(corpo["content"]) if corpo.get("contentType", "").lower() == "html" else (corpo.get("content") or "")
    de = ((m.get("from") or {}).get("emailAddress") or {}).get("name", "")
    return {"assunto": m.get("subject") or "", "de": de, "link": m.get("webLink", ""),
            "texto": f"Assunto: {m.get('subject') or ''}\nDe: {de}\n\n{texto}"[:8000]}
