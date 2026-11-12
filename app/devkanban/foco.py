"""'Foco do dia': ranking do que atacar agora, por regras (sem IA).

Deliberadamente determinístico: funciona sem chave de LLM, é explicável (cada
ponto vem com um motivo legível) e é a base que o briefing da IA só narra --
o modelo nunca decide a prioridade sozinho.
"""
from datetime import date, datetime
from typing import Optional

_PESO_PRIORIDADE = {"urgente": 60, "alta": 40, "media": 10, "baixa": 0}


def _dias_ate(prazo_iso: Optional[str], hoje: date) -> Optional[int]:
    if not prazo_iso:
        return None
    try:
        return (date.fromisoformat(prazo_iso) - hoje).days
    except ValueError:
        return None


def _dias_desde(dt: Optional[datetime], hoje: date) -> Optional[int]:
    if not dt:
        return None
    return (hoje - dt.date()).days


def pontuar(t: dict, hoje: Optional[date] = None) -> tuple[int, list[str]]:
    """(score, motivos) de uma tarefa. Concluída e backlog não entram no foco (0)."""
    hoje = hoje or date.today()
    if t["status"] not in ("todo", "doing"):
        return 0, []
    score, motivos = 0, []

    d = _dias_ate(t.get("prazo"), hoje)
    if d is not None:
        if d < 0:
            score += 100 + min(-d, 30)
            motivos.append(f"atrasada há {-d}d")
        elif d == 0:
            score += 80
            motivos.append("vence hoje")
        elif d == 1:
            score += 50
            motivos.append("vence amanhã")
        elif d <= 3:
            score += 25
            motivos.append(f"vence em {d}d")

    prio = t.get("prioridade", "media")
    score += _PESO_PRIORIDADE.get(prio, 10)
    if prio == "urgente":
        motivos.append("urgente")
    elif prio == "alta":
        motivos.append("prioridade alta")

    if t["status"] == "doing":
        score += 30
        parado = _dias_desde(t.get("atualizado_dt"), hoje)
        if parado is not None and parado >= 5:
            score += 20
            motivos.append(f"parada há {parado}d")
        else:
            desde = _dias_desde(t.get("iniciado_dt"), hoje)
            if desde is not None and desde >= 1:
                motivos.append(f"em progresso há {desde}d")
    elif t.get("pausado"):
        score += 15
        motivos.append("pausada")

    tot, ok = t.get("checklist_total", 0), t.get("checklist_concluidos", 0)
    if tot and ok < tot and (tot - ok) <= 1 and ok > 0:
        score += 10
        motivos.append(f"quase pronta ({ok}/{tot})")

    return score, motivos


def ranquear(tarefas: list[dict], limite: int = 5, hoje: Optional[date] = None) -> list[dict]:
    """Top N por score (desempate: prazo mais próximo, depois id). Só quem pontuou > 0."""
    pont = []
    for t in tarefas:
        s = t.get("score")
        if s is None:
            s, m = pontuar(t, hoje)
            t["score"], t["motivos"] = s, m
        if s > 0:
            pont.append(t)
    pont.sort(key=lambda t: (-t["score"], t.get("prazo") or "9999-12-31", t["id"]))
    return pont[:limite]
