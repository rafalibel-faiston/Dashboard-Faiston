"""GitHub: estado de PR/issue linkado numa tarefa + tratamento dos webhooks do GitHub.

Variáveis de ambiente:
    GITHUB_TOKEN    (fine-grained, só leitura: Pull requests, Issues, Checks, Metadata)
    GITHUB_API_URL  (opcional; padrão https://api.github.com -- serve pra GitHub Enterprise)
Sem token funciona só pra repositório público e com limite baixo da API.
"""
import os
import re
import time
from typing import Optional

import requests

_URL = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)", re.I)
_cache: dict[str, tuple[float, dict]] = {}
TTL_S = 90


def token_configurado() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN"))


def interpretar(url: str) -> Optional[tuple[str, str, str, int]]:
    """(dono, repo, 'pull'|'issues', número) ou None se não for link de PR/issue."""
    m = _URL.match((url or "").strip())
    return (m.group(1), m.group(2), m.group(3).lower(), int(m.group(4))) if m else None


def normalizar(url: str) -> Optional[str]:
    p = interpretar(url)
    return f"https://github.com/{p[0].lower()}/{p[1].lower()}/{p[2]}/{p[3]}" if p else None


def _get(caminho: str):
    """Única função que fala HTTP com a API do GitHub (os testes trocam esta). Devolve (status, json)."""
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "faiston-ops-kanban"}
    if token_configurado():
        h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    r = requests.get(os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/") + caminho, headers=h, timeout=8)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def _checks(dono: str, repo: str, sha: str) -> Optional[str]:
    st, d = _get(f"/repos/{dono}/{repo}/commits/{sha}/check-runs?per_page=50")
    runs = d.get("check_runs") if st == 200 and isinstance(d, dict) else None
    if runs:
        concl = {r.get("conclusion") for r in runs}
        if concl & {"failure", "timed_out", "cancelled", "action_required"}:
            return "falhando"
        if any(r.get("status") != "completed" for r in runs):
            return "pendente"
        return "ok"
    st, d = _get(f"/repos/{dono}/{repo}/commits/{sha}/status")
    if st == 200 and d.get("total_count"):
        return {"success": "ok", "failure": "falhando", "error": "falhando", "pending": "pendente"}.get(d.get("state"))
    return None


def status(url: str) -> dict:
    p = interpretar(url)
    if not p:
        return {"erro": "link_invalido"}
    chave = normalizar(url)
    agora = time.time()
    if chave in _cache and agora - _cache[chave][0] < TTL_S:
        return _cache[chave][1]
    dono, repo, tipo, n = p
    st, d = _get(f"/repos/{dono}/{repo}/{'pulls' if tipo == 'pull' else 'issues'}/{n}")
    if st in (401, 403, 404):
        out = {"erro": "sem_acesso", "dica": "Repositório privado? Configure GITHUB_TOKEN." if not token_configurado() else "O token não enxerga este repositório."}
    elif st != 200:
        out = {"erro": "indisponivel"}
    else:
        if tipo == "pull":
            estado = "mergeado" if d.get("merged") else ("fechado" if d.get("state") == "closed" else ("rascunho" if d.get("draft") else "aberto"))
            checks = _checks(dono, repo, d["head"]["sha"]) if estado in ("aberto", "rascunho") and (d.get("head") or {}).get("sha") else None
        else:
            estado = "fechado" if d.get("state") == "closed" else "aberto"
            checks = None
        out = {"tipo": "pr" if tipo == "pull" else "issue", "numero": n, "estado": estado, "titulo": d.get("title", ""),
               "autor": (d.get("user") or {}).get("login", ""), "atualizado": (d.get("updated_at") or "")[:10], "checks": checks,
               "repo": f"{dono}/{repo}", "url": d.get("html_url") or chave}
    _cache[chave] = (agora, out)
    return out


def status_lote(urls: list[str]) -> dict:
    return {u: status(u) for u in list(dict.fromkeys(urls))[:30]}


# ── webhook do GitHub -> quadro ───────────────────────────────────

def tarefas_do_link(cur, url: str) -> list[int]:
    """Tarefas cujo link aponta para esse PR/issue (comparação normalizada)."""
    alvo = normalizar(url)
    if not alvo:
        return []
    cur.execute("SELECT id, link FROM dev_tarefas WHERE link ILIKE %s", ("%github.com/%",))
    return [tid for tid, link in cur.fetchall() if normalizar(link) == alvo]


def processar_evento(cur, hook_nome: str, evento: str, payload: dict, mover, comentar) -> dict:
    """Aplica um evento do GitHub. `mover(tid, status) -> bool` e `comentar(tid, texto)` vêm do chamador
    (assim este módulo não conhece a camada de banco nem os eventos de saída).
    Regras: PR mergeado -> Concluído; PR aberto/pronto pra review -> Em progresso (se estava em Backlog/A fazer);
    PR fechado sem merge -> só comentário; issue fechada -> Concluído; issue reaberta -> A fazer."""
    if evento == "ping":
        return {"acao": "ping"}
    pr = payload.get("pull_request") if evento == "pull_request" else None
    issue = payload.get("issue") if evento == "issues" else None
    obj = pr or issue
    if not obj:
        return {"acao": "ignorado", "motivo": f"evento '{evento}' não tratado"}
    acao_gh = payload.get("action", "")
    ids = tarefas_do_link(cur, obj.get("html_url", ""))
    if not ids:
        return {"acao": "ignorado", "motivo": "nenhuma tarefa com esse link"}
    quem = (payload.get("sender") or {}).get("login", "alguém")
    rotulo = f"PR #{obj.get('number')}" if pr else f"Issue #{obj.get('number')}"
    movidas = 0
    for tid in ids:
        cur.execute("SELECT status FROM dev_tarefas WHERE id = %s", (tid,))
        atual = cur.fetchone()[0]
        if pr and acao_gh == "closed" and pr.get("merged"):
            if atual != "done" and mover(tid, "done"):
                movidas += 1
            comentar(tid, f"{rotulo} mergeado por @{quem}.")
        elif pr and acao_gh == "closed":
            comentar(tid, f"{rotulo} fechado sem merge por @{quem}.")
        elif pr and acao_gh in ("opened", "reopened", "ready_for_review"):
            if atual in ("backlog", "todo") and mover(tid, "doing"):
                movidas += 1
            comentar(tid, f"{rotulo} {'reaberto' if acao_gh == 'reopened' else 'aberto'} por @{quem}.")
        elif issue and acao_gh == "closed":
            if atual != "done" and mover(tid, "done"):
                movidas += 1
            comentar(tid, f"{rotulo} fechada por @{quem}.")
        elif issue and acao_gh == "reopened":
            if atual == "done" and mover(tid, "todo"):
                movidas += 1
            comentar(tid, f"{rotulo} reaberta por @{quem}.")
    return {"acao": "processado", "tarefas": ids, "movidas": movidas}
