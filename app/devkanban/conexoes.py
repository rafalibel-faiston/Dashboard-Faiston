"""Conexões do Kanban da Área de Dev.

  /api/dev-integracoes/*   (só perfil 'dev', cookie de sessão + CSRF)
      status, webhooks de entrada/saída, estado de PR/issue do GitHub,
      calendário (prazo -> Outlook) e e-mails sinalizados (Outlook -> tarefa)
  /api/dev-hooks/in/{token}  (PÚBLICO, autenticado pelo token na URL; isento de CSRF)
      recebe eventos de n8n/TiFlux/Hub (tipo 'generico') e do GitHub (tipo 'github')
"""
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.assistente import llm as _llm_cfg
from app.devkanban import db as dkdb
from app.devkanban import eventos, github, graph, ia

router = APIRouter(prefix="/api/dev-integracoes", tags=["dev-integracoes"])
router_entrada = APIRouter(prefix="/api/dev-hooks/in", tags=["dev-hooks"])

LIMITE_ENTRADA_MIN = 120
TAMANHO_MAX_CORPO = 256 * 1024
_uso_hook: dict[str, deque] = defaultdict(deque)


def _dev(token) -> dict:
    sess = dkdb.get_session(token)
    if not dkdb.eh_dev(sess):
        raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    return sess


def _conn():
    conn = dkdb.get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco offline")
    return conn


# ══════════════════════════════════════════════════════════════════
# Status geral
# ══════════════════════════════════════════════════════════════════
@router.get("/status")
def status(faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    return {
        "ia": {"configurado": bool(_llm_cfg.LLM_API_KEY), "modelo": _llm_cfg.LLM_MODEL},
        "github": {"token": github.token_configurado()},
        "graph": {"configurado": graph.configurado()},
    }


# ══════════════════════════════════════════════════════════════════
# Webhooks de ENTRADA (cadastro)
# ══════════════════════════════════════════════════════════════════
class HookEntradaIn(BaseModel):
    nome: str
    tipo: str = "generico"
    origem: str = "n8n"
    tag_padrao: str = ""
    status_padrao: str = "todo"
    atribuido_padrao: Optional[int] = None


class AtivoIn(BaseModel):
    ativo: bool


_COLS_ENTRADA = "id, nome, tipo, token, segredo, origem, tag_padrao, status_padrao, atribuido_padrao, ativo, criado_em, ultimo_uso, ultimo_resultado"


def _hook_entrada_dict(r) -> dict:
    return {"id": r[0], "nome": r[1], "tipo": r[2], "token": r[3], "segredo": r[4], "origem": r[5], "tag_padrao": r[6],
            "status_padrao": r[7], "atribuido_padrao": r[8], "ativo": r[9],
            "criado_em": r[10].strftime("%d/%m/%Y %H:%M") if r[10] else "",
            "ultimo_uso": r[11].strftime("%d/%m/%Y %H:%M") if r[11] else None, "ultimo_resultado": r[12],
            "caminho": f"/api/dev-hooks/in/{r[3]}"}


@router.get("/hooks-entrada")
def listar_hooks_entrada(faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {_COLS_ENTRADA} FROM dev_hooks_entrada ORDER BY id")
        return [_hook_entrada_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@router.post("/hooks-entrada")
def criar_hook_entrada(b: HookEntradaIn, faiston_token: str = Cookie(None)):
    sess = _dev(faiston_token)
    nome = b.nome.strip()[:80]
    if not nome:
        raise HTTPException(status_code=400, detail="Dê um nome ao webhook")
    if b.tipo not in ("generico", "github"):
        raise HTTPException(status_code=400, detail="Tipo inválido")
    origem = (ia._limpa(b.origem, 30).lower() or ("github" if b.tipo == "github" else "n8n"))
    status_padrao = b.status_padrao if b.status_padrao in dkdb.STATUS_VALIDOS else "todo"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO dev_hooks_entrada (nome, tipo, token, segredo, origem, tag_padrao, status_padrao, atribuido_padrao, criado_por)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING """ + _COLS_ENTRADA,
                    (nome, b.tipo, secrets.token_urlsafe(24), secrets.token_urlsafe(24) if b.tipo == "github" else "",
                     origem, ia._limpa(b.tag_padrao, 40), status_padrao, b.atribuido_padrao, sess["id"]))
        out = _hook_entrada_dict(cur.fetchone())
        conn.commit()
        return out
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.put("/hooks-entrada/{hid}")
def ativar_hook_entrada(hid: int, b: AtivoIn, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE dev_hooks_entrada SET ativo=%s WHERE id=%s", (b.ativo, hid))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Webhook não encontrado")
        return {"sucesso": True}
    finally:
        conn.close()


@router.delete("/hooks-entrada/{hid}")
def excluir_hook_entrada(hid: int, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dev_hooks_entrada WHERE id=%s", (hid,))
        conn.commit()
        return {"sucesso": True}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# Webhooks de SAÍDA (cadastro)
# ══════════════════════════════════════════════════════════════════
class HookSaidaIn(BaseModel):
    nome: str
    url: str
    eventos: List[str] = []


_COLS_SAIDA = "id, nome, url, segredo, eventos, ativo, criado_em, ultimo_status, ultimo_erro, ultimo_em"


def _hook_saida_dict(r) -> dict:
    return {"id": r[0], "nome": r[1], "url": r[2], "segredo": r[3], "eventos": r[4] or [], "ativo": r[5],
            "criado_em": r[6].strftime("%d/%m/%Y %H:%M") if r[6] else "", "ultimo_status": r[7], "ultimo_erro": r[8],
            "ultimo_em": r[9].strftime("%d/%m/%Y %H:%M") if r[9] else None}


@router.get("/hooks-saida")
def listar_hooks_saida(faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {_COLS_SAIDA} FROM dev_hooks_saida ORDER BY id")
        return {"hooks": [_hook_saida_dict(r) for r in cur.fetchall()], "eventos_disponiveis": list(eventos.EVENTOS)}
    finally:
        conn.close()


@router.post("/hooks-saida")
def criar_hook_saida(b: HookSaidaIn, faiston_token: str = Cookie(None)):
    sess = _dev(faiston_token)
    nome = b.nome.strip()[:80]
    if not nome:
        raise HTTPException(status_code=400, detail="Dê um nome ao webhook")
    ok, motivo = eventos.url_segura(b.url)
    if not ok:
        raise HTTPException(status_code=400, detail=motivo)
    evs = [e for e in b.eventos if e in eventos.EVENTOS]
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO dev_hooks_saida (nome, url, segredo, eventos, criado_por) VALUES (%s,%s,%s,%s,%s) RETURNING " + _COLS_SAIDA,
                    (nome, b.url.strip(), secrets.token_urlsafe(24), evs, sess["id"]))
        out = _hook_saida_dict(cur.fetchone())
        conn.commit()
        return out
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.put("/hooks-saida/{hid}")
def ativar_hook_saida(hid: int, b: AtivoIn, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE dev_hooks_saida SET ativo=%s WHERE id=%s", (b.ativo, hid))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Webhook não encontrado")
        return {"sucesso": True}
    finally:
        conn.close()


@router.delete("/hooks-saida/{hid}")
def excluir_hook_saida(hid: int, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dev_hooks_saida WHERE id=%s", (hid,))
        conn.commit()
        return {"sucesso": True}
    finally:
        conn.close()


@router.post("/hooks-saida/{hid}/testar")
def testar_hook_saida(hid: int, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT url, segredo FROM dev_hooks_saida WHERE id=%s", (hid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Webhook não encontrado")
        amostra = {"id": 0, "titulo": "Tarefa de teste do Faiston OPS", "status": "todo", "prioridade": "media", "tags": ["Teste"]}
        st, erro = eventos.enviar_hook({"url": row[0], "segredo": row[1]}, "ping", amostra)
        cur.execute("UPDATE dev_hooks_saida SET ultimo_status=%s, ultimo_erro=%s, ultimo_em=NOW() WHERE id=%s", (st, erro or None, hid))
        conn.commit()
        return {"status": st, "erro": erro or None}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# GitHub
# ══════════════════════════════════════════════════════════════════
class UrlsIn(BaseModel):
    urls: List[str]


@router.post("/github/status")
async def github_status(b: UrlsIn, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    urls = [u for u in b.urls if github.interpretar(u)]
    return await run_in_threadpool(github.status_lote, urls)


# ══════════════════════════════════════════════════════════════════
# Calendário (prazo -> Outlook)
# ══════════════════════════════════════════════════════════════════
def _mailbox(cur, usuario_id: Optional[int]) -> Optional[str]:
    if not usuario_id:
        return None
    cur.execute("SELECT email FROM usuarios WHERE id=%s", (usuario_id,))
    r = cur.fetchone()
    return (r[0] or "").strip() or None if r else None


def _erro_graph(e: graph.GraphErro) -> HTTPException:
    return HTTPException(status_code=503 if e.status in (401, 403, 503) else 502, detail=str(e))


@router.post("/calendario/{tid}")
def calendario_agendar(tid: int, faiston_token: str = Cookie(None)):
    """Cria (ou atualiza) um evento de dia inteiro no Outlook do responsável (ou de quem pediu) no dia do prazo."""
    sess = _dev(faiston_token)
    if not graph.configurado():
        raise HTTPException(status_code=503, detail="Microsoft Graph não configurado neste ambiente")
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT titulo, prazo, link, atribuido_a, evento_id, evento_mailbox FROM dev_tarefas WHERE id=%s", (tid,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        titulo, prazo, link, atrib, evento_id, mb_atual = t
        if not prazo:
            raise HTTPException(status_code=400, detail="Defina um prazo na tarefa antes de agendar")
        mailbox = mb_atual or _mailbox(cur, atrib) or _mailbox(cur, sess["id"])
        if not mailbox:
            raise HTTPException(status_code=400, detail="O responsável não tem e-mail cadastrado")
        try:
            if evento_id:
                graph.atualizar_evento(mailbox, evento_id, titulo, prazo.isoformat(), link or "")
            else:
                evento_id = graph.criar_evento(mailbox, titulo, prazo.isoformat(), link or "")
        except graph.GraphErro as e:
            raise _erro_graph(e)
        cur.execute("UPDATE dev_tarefas SET evento_id=%s, evento_mailbox=%s WHERE id=%s", (evento_id, mailbox, tid))
        conn.commit()
        return {"sucesso": True, "evento_id": evento_id, "mailbox": mailbox}
    finally:
        conn.close()


@router.delete("/calendario/{tid}")
def calendario_remover(tid: int, faiston_token: str = Cookie(None)):
    _dev(faiston_token)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT evento_id, evento_mailbox FROM dev_tarefas WHERE id=%s", (tid,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        if r[0] and r[1]:
            try:
                graph.remover_evento(r[1], r[0])
            except graph.GraphErro as e:
                raise _erro_graph(e)
        cur.execute("UPDATE dev_tarefas SET evento_id=NULL, evento_mailbox=NULL WHERE id=%s", (tid,))
        conn.commit()
        return {"sucesso": True}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# Outlook (e-mail sinalizado -> tarefa)
# ══════════════════════════════════════════════════════════════════
def _meu_email(sess: dict) -> str:
    conn = _conn()
    try:
        cur = conn.cursor()
        mb = _mailbox(cur, sess["id"])
    finally:
        conn.close()
    if not mb:
        raise HTTPException(status_code=400, detail="Seu usuário não tem e-mail cadastrado")
    return mb


@router.get("/outlook/emails")
def outlook_emails(faiston_token: str = Cookie(None)):
    sess = _dev(faiston_token)
    if not graph.configurado():
        raise HTTPException(status_code=503, detail="Microsoft Graph não configurado neste ambiente")
    try:
        return graph.emails_sinalizados(_meu_email(sess))
    except graph.GraphErro as e:
        raise _erro_graph(e)


@router.get("/outlook/email")
def outlook_email(id: str, faiston_token: str = Cookie(None)):
    sess = _dev(faiston_token)
    if not graph.configurado():
        raise HTTPException(status_code=503, detail="Microsoft Graph não configurado neste ambiente")
    try:
        return graph.texto_email(_meu_email(sess), id)
    except graph.GraphErro as e:
        raise _erro_graph(e)


# ══════════════════════════════════════════════════════════════════
# Webhook de ENTRADA (público, autenticado pelo token da URL)
# ══════════════════════════════════════════════════════════════════
def _limitar_hook(token: str) -> None:
    agora = time.monotonic()
    q = _uso_hook[token]
    while q and agora - q[0] > 60:
        q.popleft()
    if len(q) >= LIMITE_ENTRADA_MIN:
        raise HTTPException(status_code=429, detail="Limite de chamadas por minuto atingido")
    q.append(agora)


def _buscar_hook(token: str) -> Optional[dict]:
    conn = dkdb.get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {_COLS_ENTRADA}, criado_por FROM dev_hooks_entrada WHERE token=%s AND ativo", (token,))
        r = cur.fetchone()
        if not r:
            return None
        d = _hook_entrada_dict(r[:-1])
        d["criado_por"] = r[-1]
        return d
    finally:
        conn.close()


def _comentar(cur, tid: int, autor: str, texto: str) -> None:
    cur.execute("INSERT INTO dev_tarefa_comentarios (tarefa_id, usuario_id, usuario_nome, texto) VALUES (%s, NULL, %s, %s)",
                (tid, autor[:100], texto[:2000]))


def processar_generico(cur, hook: dict, p: dict) -> dict:
    """Payload do n8n/TiFlux/Hub -> upsert/mover/concluir/comentar por origem_ref."""
    acao = str(p.get("acao") or "upsert").lower()
    ref = ia._limpa(p.get("origem_ref"), 200) or None
    existente = None
    if ref:
        cur.execute("SELECT id FROM dev_tarefas WHERE origem=%s AND origem_ref=%s", (hook["origem"], ref))
        r0 = cur.fetchone()
        existente = r0[0] if r0 else None

    if acao in ("mover", "concluir", "comentar"):
        if not ref:
            raise HTTPException(status_code=400, detail="'origem_ref' é obrigatório para esta ação")
        if not existente:
            raise HTTPException(status_code=404, detail="Nenhuma tarefa com esse origem_ref")
        if acao == "comentar":
            texto = str(p.get("comentario") or "").strip()
            if not texto:
                raise HTTPException(status_code=400, detail="'comentario' é obrigatório")
            _comentar(cur, existente, hook["nome"], texto)
            return {"resultado": "comentada", "id": existente, "evento": None}
        novo = "done" if acao == "concluir" else p.get("status")
        if novo not in dkdb.STATUS_VALIDOS:
            raise HTTPException(status_code=400, detail="'status' inválido")
        dkdb.mover_tarefa(cur, existente, novo)
        return {"resultado": "movida", "id": existente, "evento": None}

    if acao != "upsert":
        raise HTTPException(status_code=400, detail="Ação inválida (use upsert, mover, concluir ou comentar)")

    cur.execute("SELECT DISTINCT unnest(tags) FROM dev_tarefas")
    tags_existentes = [r[0] for r in cur.fetchall()]

    if existente:
        sets, vals = [], []
        if p.get("titulo") and ia._limpa(p["titulo"], 200):
            sets.append("titulo=%s"); vals.append(ia._limpa(p["titulo"], 200))
        if "descricao" in p:
            sets.append("descricao=%s"); vals.append(str(p.get("descricao") or "")[:4000])
        if p.get("prioridade") in dkdb.PRIORIDADES_VALIDAS:
            sets.append("prioridade=%s"); vals.append(p["prioridade"])
        if "prazo" in p:
            sets.append("prazo=%s"); vals.append(ia._data_iso(p.get("prazo")))
        if "link" in p:
            sets.append("link=%s"); vals.append(str(p.get("link") or "")[:500])
        if isinstance(p.get("tags"), list):
            sets.append("tags=%s"); vals.append(ia._tags_limpas(p["tags"], tags_existentes))
        if sets:
            cur.execute(f"UPDATE dev_tarefas SET {', '.join(sets)}, atualizado_em=NOW() WHERE id=%s", (*vals, existente))
        if p.get("status") in dkdb.STATUS_VALIDOS:
            dkdb.mover_tarefa(cur, existente, p["status"])
        return {"resultado": "atualizada", "id": existente, "evento": None}

    if not ia._limpa(p.get("titulo"), 200):
        raise HTTPException(status_code=400, detail="'titulo' é obrigatório para criar")
    tags = list(p.get("tags") if isinstance(p.get("tags"), list) else [])
    if hook["tag_padrao"] and not any(str(t).lower() == hook["tag_padrao"].lower() for t in tags):
        tags.insert(0, hook["tag_padrao"])
    r = ia.sanitizar_rascunho({**p, "tags": tags, "status": p.get("status") or hook["status_padrao"]}, tags_existentes)
    r["atribuido_a"] = hook["atribuido_padrao"]
    tid = dkdb.criar_tarefa(cur, r, hook["criado_por"], origem=hook["origem"], origem_ref=ref)
    if tid is None:                                  # corrida: outra chamada criou a mesma origem_ref agora há pouco
        return {"resultado": "ignorada", "id": None, "evento": None}
    return {"resultado": "criada", "id": tid, "evento": None}


@router_entrada.post("/{token}")
async def entrada(token: str, request: Request):
    corpo = await request.body()
    if len(corpo) > TAMANHO_MAX_CORPO:
        raise HTTPException(status_code=413, detail="Corpo grande demais")
    hook = await run_in_threadpool(_buscar_hook, token)
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook não encontrado")      # não distingue inexistente de desativado
    _limitar_hook(token)

    evento = ""
    if hook["tipo"] == "github":
        esperado = eventos.assinar(hook["segredo"], corpo)
        if not hmac.compare_digest(esperado, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(status_code=401, detail="Assinatura inválida")
        evento = request.headers.get("x-github-event", "")
    try:
        p = json.loads(corpo or b"{}")
        if not isinstance(p, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="O corpo precisa ser um JSON (objeto)")

    def _trabalhar():
        conn = dkdb.get_conn()
        if not conn:
            raise HTTPException(status_code=500, detail="Banco offline")
        try:
            cur = conn.cursor()
            movidos = []
            if hook["tipo"] == "github":
                def mover(tid, st):
                    ok = dkdb.mover_tarefa(cur, tid, st)
                    if ok:
                        movidos.append((tid, st))
                    return ok
                out = github.processar_evento(cur, hook["nome"], evento, p, mover, lambda tid, txt: _comentar(cur, tid, "GitHub", txt))
            else:
                out = processar_generico(cur, hook, p)
            cur.execute("UPDATE dev_hooks_entrada SET ultimo_uso=NOW(), ultimo_resultado=%s WHERE id=%s",
                        (json.dumps(out, ensure_ascii=False)[:300], hook["id"]))
            conn.commit()
            return out, movidos
        except HTTPException as e:
            conn.rollback()
            c2 = dkdb.get_conn()
            if c2:
                try:
                    c2.cursor().execute("UPDATE dev_hooks_entrada SET ultimo_uso=NOW(), ultimo_resultado=%s WHERE id=%s", (f"erro: {e.detail}"[:300], hook["id"]))
                    c2.commit()
                finally:
                    c2.close()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    out, movidos = await run_in_threadpool(_trabalhar)
    if hook["tipo"] == "github":                     # só o GitHub emite saída (ver docstring de eventos.py)
        for tid, st in movidos:
            eventos.emitir("tarefa.concluida" if st == "done" else "tarefa.movida", tid)
    return {"ok": True, **{k: v for k, v in out.items() if k != "evento"}}
