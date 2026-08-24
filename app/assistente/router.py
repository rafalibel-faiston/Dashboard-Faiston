"""Endpoints do assistente OPS.

Fase 1: log + caixa de perguntas, resposta genérica em streaming.
Fase 2: capacidade C (resumir) por gatilho de palavra-chave.
Fase 3: capacidade B (explicar) — se houver documento indexado, toda
pergunta que não for pedido de resumo passa pela busca híbrida; sem
documento nenhum ainda, cai no genérico da Fase 1 (comportamento atual
até a primeira ingestão).
"""
import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.assistente import db, log as assistente_log
from app.assistente.capacidade_explicar import buscar_hibrido, montar_fontes, montar_mensagem_trechos
from app.assistente.capacidade_resumir import montar_agregado_semana
from app.assistente.llm import ModeloIndisponivel, completar_stream
from app.assistente.schemas import FeedbackRequest, PerguntaRequest

router = APIRouter(prefix="/assistente", tags=["assistente"])

_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _DIR.parent.parent / "static" / "assistente"
_PROMPT_GENERICO = (_DIR / "prompts" / "sistema_generico.md").read_text(encoding="utf-8")
_PROMPT_RESUMIR = (_DIR / "prompts" / "sistema_resumir.md").read_text(encoding="utf-8")
_PROMPT_EXPLICAR = (_DIR / "prompts" / "sistema_explicar.md").read_text(encoding="utf-8")
_RECUSA_SEM_DOCUMENTO = "Não encontrei isso na base de procedimentos."


def _normalizar(txt: str) -> str:
    nfkd = unicodedata.normalize("NFKD", txt.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _eh_pedido_de_resumo(pergunta: str) -> bool:
    """Fase 2: sem classificação de intenção de verdade ainda (isso é
    Fase 4/intencao.py) — um gatilho simples por palavra-chave é
    suficiente pra decidir entre o resumo semanal (SQL + redação) e a
    conversa genérica, sem correr o risco de um NL→SQL aberto."""
    p = _normalizar(pergunta)
    return "resumo" in p and ("semana" in p or "semanal" in p)

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


async def _exigir_admin(faiston_token: Optional[str]) -> dict:
    """Gestão de conteúdo é mais sensível que só perguntar: sempre exige
    perfil='admin' de verdade, independente de quem mais estiver no
    piloto via ASSISTENTE_PERFIS_PILOTO."""
    sess = await run_in_threadpool(db.get_session, faiston_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Não autenticado")
    if sess["perfil"] != "admin":
        raise HTTPException(status_code=403, detail="Só admin pode gerenciar a base de procedimentos")
    return sess


@router.get("/elegivel")
async def elegivel(faiston_token: str = Cookie(None)):
    """O widget consulta isto pra decidir se aparece. Nunca 401/403 aqui —
    só diz sim/não, pra não gerar erro no console em toda tela.
    `admin` vai junto pra o widget decidir se mostra o atalho de gestão
    da base de procedimentos, sem precisar de uma segunda chamada."""
    sess = await run_in_threadpool(db.get_session, faiston_token)
    elegivel_ = bool(sess and sess["perfil"] in _PERFIS_PILOTO)
    return {"elegivel": elegivel_, "admin": bool(elegivel_ and sess["perfil"] == "admin")}


@router.post("/pergunta")
async def pergunta(body: PerguntaRequest, faiston_token: str = Cookie(None)):
    sess = await _autenticar(faiston_token)

    async def stream():
        inicio = time.monotonic()
        log_id = await run_in_threadpool(
            assistente_log.criar_pergunta, sess["id"], body.pergunta, body.contexto_tela
        )

        pedido_resumo = _eh_pedido_de_resumo(body.pergunta)
        trechos = None
        if not pedido_resumo:
            trechos = await run_in_threadpool(buscar_hibrido, body.pergunta)
            if trechos is None:
                latencia_ms = int((time.monotonic() - inicio) * 1000)
                await run_in_threadpool(
                    assistente_log.finalizar,
                    log_id,
                    resposta=None,
                    respondida=False,
                    motivo_falha="erro_modelo",
                    latencia_ms=latencia_ms,
                    capacidade="explicar",
                )
                yield _sse("inicio", {"log_id": log_id, "capacidade": "explicar"})
                yield _sse("erro", {"mensagem": "Não consegui buscar nos procedimentos agora. Tente de novo em instantes."})
                return

        if pedido_resumo:
            capacidade = "resumir"
        elif trechos:
            capacidade = "explicar"
        else:
            capacidade = None
        yield _sse("inicio", {"log_id": log_id, "capacidade": capacidade})

        if pedido_resumo:
            agregado = await run_in_threadpool(montar_agregado_semana)
            if agregado is None:
                latencia_ms = int((time.monotonic() - inicio) * 1000)
                await run_in_threadpool(
                    assistente_log.finalizar,
                    log_id,
                    resposta=None,
                    respondida=False,
                    motivo_falha="erro_modelo",
                    latencia_ms=latencia_ms,
                    capacidade=capacidade,
                )
                yield _sse("erro", {"mensagem": "Não consegui acessar os dados agora pra montar o resumo. Tente de novo em instantes."})
                return
            mensagens = [
                {"role": "system", "content": _PROMPT_RESUMIR},
                {"role": "user", "content": json.dumps(agregado, ensure_ascii=False)},
            ]
        elif trechos:
            mensagens = [
                {"role": "system", "content": _PROMPT_EXPLICAR},
                {"role": "user", "content": f"{montar_mensagem_trechos(trechos)}\n\nPergunta: {body.pergunta}"},
            ]
        else:
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
                motivo_falha=e.motivo,
                latencia_ms=latencia_ms,
                capacidade=capacidade,
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
                capacidade=capacidade,
            )
            yield _sse("erro", {"mensagem": "Algo deu errado ao gerar a resposta. Tente de novo em instantes."})
            return

        latencia_ms = int((time.monotonic() - inicio) * 1000)
        texto_final = "".join(resposta_completa)

        # Regra 2 do CLAUDE.md do assistente: recusa honesta sem trecho
        # relevante é sucesso, não falha -- mas motivo_falha='sem_documento'
        # marca a pergunta como não respondida de fato, pra alimentar
        # /assistente/lacunas (Fase 4) com o que falta documentar.
        eh_recusa = capacidade == "explicar" and texto_final.strip() == _RECUSA_SEM_DOCUMENTO
        motivo_falha = "sem_documento" if eh_recusa else None

        await run_in_threadpool(
            assistente_log.finalizar,
            log_id,
            resposta=texto_final,
            respondida=bool(texto_final),
            motivo_falha=motivo_falha,
            tokens_entrada=tokens_entrada,
            tokens_saida=tokens_saida,
            latencia_ms=latencia_ms,
            capacidade=capacidade,
        )
        if capacidade == "explicar" and trechos and not eh_recusa:
            yield _sse("fontes", {"fontes": montar_fontes(trechos)})
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


# --- Ingestão de documento (Fase 3 — capacidade B) -----------------------

@router.get("/documentos")
async def documentos_admin_page(faiston_token: str = Cookie(None)):
    """Tela de gestão da base de procedimentos — mesmo padrão de gate
    server-side que as outras páginas do OPS (ex.: /dashboard em
    main.py): sem sessão ou sem ser admin, redireciona em vez de
    devolver a página."""
    sess = await run_in_threadpool(db.get_session, faiston_token)
    if not sess:
        return RedirectResponse("/")
    if sess["perfil"] != "admin":
        return RedirectResponse("/dashboard")
    return FileResponse(_STATIC_DIR / "documentos.html")


@router.get("/documentos/lista")
async def listar_documentos_endpoint(faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.ingestao import listar_documentos

    documentos = await run_in_threadpool(listar_documentos)
    return {"documentos": documentos}


@router.post("/documentos")
async def ingerir(
    arquivo: UploadFile = File(...),
    titulo: str = Form(...),
    origem: Optional[str] = Form(None),
    versao: Optional[str] = Form(None),
    faiston_token: str = Cookie(None),
):
    await _exigir_admin(faiston_token)

    from app.assistente.ingestao import extrair_texto, ingerir_documento

    sufixo = Path(arquivo.filename or "").suffix.lower()
    if sufixo not in (".md", ".markdown", ".docx", ".pdf"):
        raise HTTPException(status_code=400, detail="Formato não suportado (use .md, .docx ou .pdf)")

    conteudo = await arquivo.read()
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=sufixo, delete=True) as tmp:
        tmp.write(conteudo)
        tmp.flush()
        try:
            texto = await run_in_threadpool(extrair_texto, Path(tmp.name))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Não consegui ler o arquivo: {e}")

    if not texto.strip():
        raise HTTPException(status_code=400, detail="Arquivo sem texto extraível")

    documento_id = await run_in_threadpool(
        ingerir_documento, titulo, texto, origem, versao
    )
    if documento_id is None:
        raise HTTPException(status_code=500, detail="Não consegui gravar o documento agora")
    return {"documento_id": documento_id, "titulo": titulo}


@router.delete("/documentos/{documento_id}")
async def remover_documento_endpoint(documento_id: int, faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.ingestao import remover_documento

    ok = await run_in_threadpool(remover_documento, documento_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
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
