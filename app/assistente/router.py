"""Endpoints do assistente NEXO. Fase 1: log + caixa de perguntas — resposta
genérica em streaming, sem acesso a dado do sistema nem a documentos ainda
(isso vem nas fases 2/3, só depois de validar esta com a equipe).
"""
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.assistente import db, log as assistente_log
from app.assistente.llm import ModeloIndisponivel, completar_stream
from app.assistente.schemas import FeedbackRequest, PerguntaRequest

router = APIRouter(prefix="/assistente", tags=["assistente"])

_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _DIR.parent.parent / "static" / "assistente"
_PROMPT_GENERICO = (_DIR / "prompts" / "sistema_generico.md").read_text(encoding="utf-8")

# Piloto: só estes perfis veem o widget (servidor e front concordam nisso —
# o front esconde o botão, mas o servidor é quem de fato barra). Lista
# ampliável via env var sem precisar editar código, ex.:
# ASSISTENTE_PERFIS_PILOTO="admin,gestor"
_PERFIS_PILOTO = {
    p.strip()
    for p in os.environ.get("ASSISTENTE_PERFIS_PILOTO", "admin").split(",")
    if p.strip()
}


def _sse(evento: str, data: dict) -> str:
    return f"event: {evento}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _autenticar(faiston_token: Optional[str]) -> dict:
    sess = await run_in_threadpool(db.get_session, faiston_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Não autenticado")
    if sess["perfil"] not in _PERFIS_PILOTO:
        raise HTTPException(status_code=403, detail="Assistente ainda não disponível para seu perfil")
    return sess


@router.get("/elegivel")
async def elegivel(faiston_token: str = Cookie(None)):
    """O widget consulta isto pra decidir se aparece. Nunca 401/403 aqui —
    só diz sim/não, pra não gerar erro no console em toda tela."""
    sess = await run_in_threadpool(db.get_session, faiston_token)
    return {"elegivel": bool(sess and sess["perfil"] in _PERFIS_PILOTO)}


@router.post("/pergunta")
async def pergunta(body: PerguntaRequest, faiston_token: str = Cookie(None)):
    sess = await _autenticar(faiston_token)

    async def stream():
        inicio = time.monotonic()
        log_id = await run_in_threadpool(
            assistente_log.criar_pergunta, sess["id"], body.pergunta, body.contexto_tela
        )
        yield _sse("inicio", {"log_id": log_id, "capacidade": None})

        mensagens = [
            {"role": "system", "content": _PROMPT_GENERICO},
            {"role": "user", "content": body.pergunta},
        ]
        resposta_completa = []
        tokens_entrada = None
        tokens_saida = None
        try:
            async for delta, usage in completar_stream(mensagens):
                if usage is not None:
                    tokens_entrada = getattr(usage, "prompt_tokens", None)
                    tokens_saida = getattr(usage, "completion_tokens", None)
                elif delta:
                    resposta_completa.append(delta)
                    yield _sse("texto", {"delta": delta})
        except ModeloIndisponivel as e:
            latencia_ms = int((time.monotonic() - inicio) * 1000)
            await run_in_threadpool(
                assistente_log.finalizar,
                log_id,
                resposta="".join(resposta_completa) or None,
                respondida=False,
                motivo_falha="timeout" if "timeout" in str(e).lower() else "erro_modelo",
                latencia_ms=latencia_ms,
            )
            yield _sse("erro", {"mensagem": "O assistente não conseguiu responder agora. Tente de novo em instantes."})
            return
        except Exception:
            latencia_ms = int((time.monotonic() - inicio) * 1000)
            await run_in_threadpool(
                assistente_log.finalizar,
                log_id,
                resposta="".join(resposta_completa) or None,
                respondida=False,
                motivo_falha="erro_modelo",
                latencia_ms=latencia_ms,
            )
            yield _sse("erro", {"mensagem": "Algo deu errado ao gerar a resposta. Tente de novo em instantes."})
            return

        latencia_ms = int((time.monotonic() - inicio) * 1000)
        texto_final = "".join(resposta_completa)
        await run_in_threadpool(
            assistente_log.finalizar,
            log_id,
            resposta=texto_final,
            respondida=bool(texto_final),
            tokens_entrada=tokens_entrada,
            tokens_saida=tokens_saida,
            latencia_ms=latencia_ms,
        )
        yield _sse("fim", {
            "tokens_entrada": tokens_entrada,
            "tokens_saida": tokens_saida,
            "latencia_ms": latencia_ms,
        })

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/feedback")
async def feedback(body: FeedbackRequest, faiston_token: str = Cookie(None)):
    sess = await _autenticar(faiston_token)
    ok = await run_in_threadpool(assistente_log.registrar_feedback, body.log_id, sess["id"], body.util)
    if not ok:
        raise HTTPException(status_code=404, detail="Pergunta não encontrada")
    return {"sucesso": True}


# --- Arquivos estáticos do widget ---------------------------------------
# Rotas explícitas em vez de StaticFiles mount, mesmo padrão já usado pelo
# main.py para /faiston-ops-mark.svg — evita qualquer conflito de path com
# os endpoints /assistente/pergunta e /assistente/feedback acima.

@router.get("/widget.js")
def widget_js():
    return FileResponse(_STATIC_DIR / "widget.js", media_type="application/javascript")


@router.get("/widget.css")
def widget_css():
    return FileResponse(_STATIC_DIR / "widget.css", media_type="text/css")


@router.get("/avatar.svg")
def widget_avatar():
    return FileResponse(_STATIC_DIR / "avatar.svg", media_type="image/svg+xml")
