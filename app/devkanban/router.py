"""Endpoints do Kanban da Área de Dev: IA (/api/dev-ia/*).
As conexões (webhooks, GitHub, calendário) entram no mesmo router nas próximas fases.

Tudo restrito a perfil 'dev' (mesma regra do /api/dev-tarefas) e executado com a
sessão de quem pediu. A IA nunca escreve sozinha: propõe (capturar/comando) e o
usuário confirma (confirmar/aplicar), com revalidação no servidor."""
import time
from collections import defaultdict, deque
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.assistente import llm as _llm_cfg
from app.assistente.llm import ModeloIndisponivel
from app.devkanban import db as dkdb
from app.devkanban import eventos, foco, ia

router = APIRouter(prefix="/api/dev-ia", tags=["dev-ia"])

ORIGENS_CONFIRMAR = {"ia", "email", "manual"}
LIMITE_CHAMADAS_MIN = 20
_chamadas: dict[int, deque] = defaultdict(deque)


def _sessao_dev(token: Optional[str]) -> dict:
    sess = dkdb.get_session(token)
    if not dkdb.eh_dev(sess):
        raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    return sess


def _limitar(sess: dict) -> None:
    """Teto simples por usuário (chamadas que gastam LLM): evita custo descontrolado por loop ou clique repetido."""
    agora = time.monotonic()
    q = _chamadas[sess["id"]]
    while q and agora - q[0] > 60:
        q.popleft()
    if len(q) >= LIMITE_CHAMADAS_MIN:
        raise HTTPException(status_code=429, detail="Muitas chamadas de IA em sequência — aguarde um minuto.")
    q.append(agora)


def _carregar() -> tuple[list[dict], list[dict]]:
    conn = dkdb.get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        tarefas = dkdb.listar_tarefas(cur)
        cur.execute("SELECT id, nome FROM usuarios WHERE perfil = 'dev' AND ativo = TRUE ORDER BY nome")
        usuarios = [{"id": r[0], "nome": r[1]} for r in cur.fetchall()]
        cur.close()
        return tarefas, usuarios
    finally:
        conn.close()


def _escopo(tarefas: list[dict], sess: dict, escopo: str) -> list[dict]:
    if escopo == "todas":
        return tarefas
    return [t for t in tarefas if t["atribuido_a"] in (None, sess["id"])]


def _erro_ia(e: Exception) -> HTTPException:
    if isinstance(e, ModeloIndisponivel):
        return HTTPException(status_code=503, detail=ia.erro_da_falha(e))
    return HTTPException(status_code=502, detail="A IA devolveu uma resposta que não consegui entender. Tente reformular.")


class CapturarIn(BaseModel):
    texto: str


class ConfirmarIn(BaseModel):
    tarefas: List[dict]
    origem: str = "ia"


class ComandoIn(BaseModel):
    texto: str


class AplicarIn(BaseModel):
    acoes: List[dict]


class StandupIn(BaseModel):
    periodo: str = "ontem"        # 'ontem' | 'semana'
    escopo: str = "minhas"        # 'minhas' | 'todas'
    salvar: bool = False          # grava no Diário da equipe


@router.get("/status")
def status(faiston_token: str = Cookie(None)):
    _sessao_dev(faiston_token)
    return {"llm_configurado": bool(_llm_cfg.LLM_API_KEY), "modelo": _llm_cfg.LLM_MODEL}


@router.post("/capturar")
async def capturar(body: CapturarIn, faiston_token: str = Cookie(None)):
    sess = _sessao_dev(faiston_token)
    if len(body.texto.strip()) < 8:
        raise HTTPException(status_code=400, detail="Cole um texto um pouco maior pra eu transformar em tarefas.")
    _limitar(sess)
    tarefas, usuarios = await run_in_threadpool(_carregar)
    tags = sorted({tg for t in tarefas for tg in t["tags"]}, key=str.lower)
    try:
        rascunhos = await ia.capturar(body.texto, date.today(), tags)
    except (ModeloIndisponivel, ValueError) as e:
        raise _erro_ia(e)
    return {"rascunhos": rascunhos}


@router.post("/capturar/confirmar")
def confirmar(body: ConfirmarIn, faiston_token: str = Cookie(None)):
    sess = _sessao_dev(faiston_token)
    if body.origem not in ORIGENS_CONFIRMAR:
        raise HTTPException(status_code=400, detail="Origem inválida")
    if len(body.tarefas) > 20:
        raise HTTPException(status_code=400, detail="Tarefas demais de uma vez")
    conn = dkdb.get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM usuarios WHERE perfil = 'dev' AND ativo = TRUE")
        validos = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT unnest(tags) FROM dev_tarefas")
        tags = [r[0] for r in cur.fetchall()]
        criadas, duplicadas = [], 0
        for bruto in body.tarefas:
            r = ia.sanitizar_rascunho(bruto, tags)
            if not r:
                continue
            if r["atribuido_a"] not in validos:
                r["atribuido_a"] = None
            ref = str(bruto.get("origem_ref") or "")[:200] or None if isinstance(bruto, dict) else None
            tid = dkdb.criar_tarefa(cur, r, sess["id"], origem=body.origem, origem_ref=ref)
            if tid:
                criadas.append(tid)
            else:
                duplicadas += 1
        conn.commit()
        cur.close()
        for tid in criadas:
            eventos.emitir("tarefa.criada", tid)
        return {"sucesso": True, "criadas": criadas, "duplicadas": duplicadas}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/briefing")
async def briefing(escopo: str = "minhas", faiston_token: str = Cookie(None)):
    sess = _sessao_dev(faiston_token)
    _limitar(sess)
    tarefas, _ = await run_in_threadpool(_carregar)
    return await ia.briefing(_escopo(tarefas, sess, escopo), date.today(), sess["nome"])


@router.post("/standup")
async def standup(body: StandupIn, faiston_token: str = Cookie(None)):
    sess = _sessao_dev(faiston_token)
    if body.periodo not in ("ontem", "semana"):
        raise HTTPException(status_code=400, detail="Período inválido")
    _limitar(sess)
    tarefas, _ = await run_in_threadpool(_carregar)
    out = await ia.standup(_escopo(tarefas, sess, body.escopo), body.periodo, date.today(), sess["nome"])
    if body.salvar:
        titulo = f"Standup {'da semana' if body.periodo == 'semana' else ''} {date.today().strftime('%d/%m/%Y')}".replace("  ", " ")

        def _gravar():
            conn = dkdb.get_conn()
            if not conn:
                raise HTTPException(status_code=500, detail="Banco offline")
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO dev_diario (titulo, descricao, autor_id, autor_nome) VALUES (%s,%s,%s,%s) RETURNING id",
                            (titulo, out["texto"], sess["id"], sess["nome"]))
                novo = cur.fetchone()[0]
                conn.commit(); cur.close()
                return novo
            finally:
                conn.close()
        out["diario_id"] = await run_in_threadpool(_gravar)
    return out


@router.post("/comando")
async def comando(body: ComandoIn, faiston_token: str = Cookie(None)):
    sess = _sessao_dev(faiston_token)
    if not body.texto.strip():
        raise HTTPException(status_code=400, detail="Digite o que você quer fazer")
    _limitar(sess)
    tarefas, usuarios = await run_in_threadpool(_carregar)
    foco.anexar(tarefas)
    try:
        return await ia.comando(body.texto, date.today(), tarefas, usuarios)
    except (ModeloIndisponivel, ValueError) as e:
        raise _erro_ia(e)


@router.post("/comando/aplicar")
def aplicar(body: AplicarIn, faiston_token: str = Cookie(None)):
    sess = _sessao_dev(faiston_token)
    tarefas, usuarios = _carregar()
    tags = sorted({tg for t in tarefas for tg in t["tags"]}, key=str.lower)
    # Revalida do zero: o que vem do cliente é só uma sugestão do que aplicar.
    acoes = [a for a in ia.validar_acoes(body.acoes, tarefas, usuarios, tags) if a.get("mutavel")]
    if not acoes:
        raise HTTPException(status_code=400, detail="Nenhuma ação válida para aplicar")
    conn = dkdb.get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        feito = ia.aplicar_acoes(cur, acoes, sess)
        conn.commit()
        cur.close()
        for a in acoes:
            for tid in (a.get("ids") or [])[:50]:
                eventos.emitir(("tarefa.concluida" if a.get("status") == "done" else "tarefa.movida") if a["tipo"] == "mover" else "tarefa.atualizada", tid)
        return {"sucesso": True, **feito}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
