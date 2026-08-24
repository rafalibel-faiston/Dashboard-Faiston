"""Endpoints do assistente OPS.

Fase 1: log + caixa de perguntas, resposta genérica em streaming.
Fase 2: capacidade C (resumir) por gatilho de palavra-chave.
Fase 3: capacidade B (explicar) — se houver documento indexado, toda
pergunta que não for pedido de resumo passa pela busca híbrida; sem
documento nenhum ainda, cai no genérico da Fase 1 (comportamento atual
até a primeira ingestão).
Fase 4: capacidade A (achar) — tentada antes de explicar/genérico: o
modelo decide (function calling, tool_choice="auto") se a pergunta bate
com uma das funções do catálogo; se não bater com nenhuma, segue pro
fluxo de explicar/genérico normalmente.

Ordem de tentativa pra toda pergunta que não é pedido de resumo: achar →
explicar → genérico. Ainda sem classificação de intenção de verdade
(intencao.py) — cada capacidade decide sozinha se "é com ela" (achar via
tool_choice="auto" do próprio modelo; explicar via ter achado trecho na
busca híbrida).

Fase 5: capacidade D (observar) — proativa, fora do caminho de
/pergunta. O job diário (capacidade_observar/job.py, agendado no
main.py) detecta padrão, redige e grava sinalização; os endpoints
/sinalizacoes* aqui só leem/atualizam o que já foi gravado, sempre
escopado à pessoa que pergunta.
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
from app.assistente.capacidade_achar import formatar_resposta, identificar_e_executar
from app.assistente.capacidade_ensinar import (
    avancar as avancar_onboarding,
    etapa_por_ordem,
    iniciar_ou_retomar as iniciar_ou_retomar_onboarding,
    progresso_atual as progresso_onboarding,
    total_etapas as total_etapas_onboarding,
)
from app.assistente.capacidade_explicar import buscar_hibrido, montar_fontes, montar_mensagem_trechos
from app.assistente.capacidade_observar import sinalizacoes
from app.assistente.capacidade_resumir import montar_agregado_semana
from app.assistente.llm import ModeloIndisponivel, completar_stream
from app.assistente.schemas import (
    FeedbackRequest,
    OrdemOnboardingRequest,
    PerguntaRequest,
    PopularDadosTesteRequest,
    SinalizacaoFeedbackRequest,
)

router = APIRouter(prefix="/assistente", tags=["assistente"])

_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _DIR.parent.parent / "static" / "assistente"
_PROMPT_GENERICO = (_DIR / "prompts" / "sistema_generico.md").read_text(encoding="utf-8")
_PROMPT_RESUMIR = (_DIR / "prompts" / "sistema_resumir.md").read_text(encoding="utf-8")
_PROMPT_EXPLICAR = (_DIR / "prompts" / "sistema_explicar.md").read_text(encoding="utf-8")
_PROMPT_PROFESSOR = (_DIR / "prompts" / "sistema_professor.md").read_text(encoding="utf-8")
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


_GATILHOS_ENSINAR_INICIAR = (
    "onboarding",
    "modo professor",
    "quero aprender do zero",
)


def _eh_pedido_de_ensinar_iniciar(pergunta: str) -> bool:
    """Gatilho por palavra-chave (mesmo espírito da Fase 2) pra começar
    ou retomar a trilha de onboarding. "sou novo"/"sou nova" só conta
    junto com "time"/"equipe"/"aqui" pra não disparar em toda frase que
    mencionar isso à toa."""
    p = _normalizar(pergunta)
    if any(g in p for g in _GATILHOS_ENSINAR_INICIAR):
        return True
    eh_novo = "sou novo" in p or "sou nova" in p or "acabei de entrar" in p
    return eh_novo and ("time" in p or "equipe" in p or "aqui" in p)


def _eh_pedido_de_ensinar_continuar(pergunta: str) -> bool:
    """Frase distinta o bastante ("próxima aula") pra não colidir com uso
    normal do assistente -- só é checada, além disso, quando a pessoa já
    tem uma trilha em andamento (ver stream())."""
    p = _normalizar(pergunta)
    return "proxima aula" in p or "continuar aula" in p or "proxima etapa" in p

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

        # Fase 6 (capacidade E, ensinar): checado antes de achar/explicar,
        # mesmo nível de prioridade que o resumo — "próxima aula" só conta
        # como continuar a trilha se a pessoa já tiver uma em andamento,
        # pra não interferir em conversa normal.
        pedido_ensinar_iniciar = False
        pedido_ensinar_continuar = False
        if not pedido_resumo:
            pedido_ensinar_iniciar = _eh_pedido_de_ensinar_iniciar(body.pergunta)
            if not pedido_ensinar_iniciar:
                progresso_existente = await run_in_threadpool(progresso_onboarding, sess["id"])
                if progresso_existente and not progresso_existente["concluido"]:
                    pedido_ensinar_continuar = _eh_pedido_de_ensinar_continuar(body.pergunta)
        pedido_ensinar = pedido_ensinar_iniciar or pedido_ensinar_continuar

        etapa_ensinar = None
        if pedido_ensinar:
            progresso = await run_in_threadpool(
                iniciar_ou_retomar_onboarding if pedido_ensinar_iniciar else avancar_onboarding,
                sess["id"],
            )
            total = await run_in_threadpool(total_etapas_onboarding)
            if total and not progresso["concluido"]:
                etapa_ensinar = await run_in_threadpool(etapa_por_ordem, progresso["etapa_atual"])

            if not total or etapa_ensinar is None:
                yield _sse("inicio", {"log_id": log_id, "capacidade": "ensinar"})
                texto_final = (
                    "Você já passou por todas as etapas do onboarding que existem hoje. "
                    "Qualquer dúvida, é só perguntar normalmente."
                    if progresso["concluido"]
                    else "Ainda não tem etapa de onboarding cadastrada, ou não consegui acessar os "
                    "dados agora. Fale com o admin ou tente de novo em instantes."
                )
                yield _sse("texto", {"delta": texto_final})
                latencia_ms = int((time.monotonic() - inicio) * 1000)
                await run_in_threadpool(
                    assistente_log.finalizar,
                    log_id,
                    resposta=texto_final,
                    respondida=True,
                    latencia_ms=latencia_ms,
                    capacidade="ensinar",
                )
                yield _sse("fim", {"tokens_entrada": None, "tokens_saida": None, "latencia_ms": latencia_ms})
                return

        if not pedido_resumo and not pedido_ensinar:
            # Qualquer falha aqui (modelo fora do ar, resposta da API num
            # formato inesperado, etc.) cai pro fluxo de explicar/genérico
            # em vez de derrubar a conversa inteira — achar é só a primeira
            # tentativa, uma tentativa que falha não pode travar as outras.
            # Se o modelo estiver mesmo fora do ar, o erro vai aparecer do
            # mesmo jeito lá na frente, no fluxo que já trata isso.
            try:
                resultado_achar = await identificar_e_executar(body.pergunta, sess)
            except Exception as e:
                import traceback
                print(f"[assistente/router] Falha na tentativa de achar: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                resultado_achar = None

            if resultado_achar is not None:
                nome_funcao, dados, usage = resultado_achar
                yield _sse("inicio", {"log_id": log_id, "capacidade": "achar"})
                texto_final = formatar_resposta(nome_funcao, dados)
                yield _sse("texto", {"delta": texto_final})
                latencia_ms = int((time.monotonic() - inicio) * 1000)
                tokens_entrada = getattr(usage, "prompt_tokens", None) if usage else None
                tokens_saida = getattr(usage, "completion_tokens", None) if usage else None
                await run_in_threadpool(
                    assistente_log.finalizar,
                    log_id,
                    resposta=texto_final,
                    respondida=nome_funcao != "_invalido",
                    motivo_falha="funcao_nao_identificada" if nome_funcao == "_invalido" else None,
                    tokens_entrada=tokens_entrada,
                    tokens_saida=tokens_saida,
                    latencia_ms=latencia_ms,
                    capacidade="achar",
                )
                yield _sse("fim", {
                    "tokens_entrada": tokens_entrada,
                    "tokens_saida": tokens_saida,
                    "latencia_ms": latencia_ms,
                })
                return

        trechos = None
        if not pedido_resumo and not pedido_ensinar:
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

        if pedido_ensinar:
            capacidade = "ensinar"
        elif pedido_resumo:
            capacidade = "resumir"
        elif trechos:
            capacidade = "explicar"
        else:
            capacidade = None
        yield _sse("inicio", {"log_id": log_id, "capacidade": capacidade})

        if pedido_ensinar:
            mensagens = [
                {"role": "system", "content": _PROMPT_PROFESSOR},
                {
                    "role": "user",
                    "content": (
                        f"Etapa {progresso['etapa_atual']} de {total}: {etapa_ensinar['titulo']}\n\n"
                        f"{etapa_ensinar['texto']}"
                    ),
                },
            ]
        elif pedido_resumo:
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


# --- Sinalizações (Fase 5 — capacidade D) ---------------------------------
# Regra 8 do CLAUDE.md do assistente: a sinalização é da pessoa, nunca
# sobre a pessoa. Todo endpoint aqui é escopado por sess["id"] — nenhum
# devolve ou altera sinalização de outra pessoa, sem exceção nem pra
# admin (não existe "ver sinalização de alguém" neste assistente).

@router.get("/sinalizacoes")
async def listar_sinalizacoes(faiston_token: str = Cookie(None)):
    sess = await _autenticar(faiston_token)
    itens = await run_in_threadpool(sinalizacoes.listar_minhas, sess["id"])
    return {"sinalizacoes": itens}


@router.get("/sinalizacoes/contagem")
async def contar_sinalizacoes_nao_vistas(faiston_token: str = Cookie(None)):
    """O widget consulta isto pra decidir o número do badge. Nunca
    401/403 — sem sessão elegível, devolve 0, igual /elegivel."""
    sess = await run_in_threadpool(db.get_session, faiston_token)
    if not sess or sess["perfil"] not in _PERFIS_PILOTO:
        return {"nao_vistas": 0}
    total = await run_in_threadpool(sinalizacoes.contar_nao_vistas, sess["id"])
    return {"nao_vistas": total}


@router.post("/sinalizacoes/{sinalizacao_id}/visualizar")
async def marcar_sinalizacao_vista(sinalizacao_id: int, faiston_token: str = Cookie(None)):
    sess = await _autenticar(faiston_token)
    ok = await run_in_threadpool(sinalizacoes.marcar_vista, sinalizacao_id, sess["id"])
    if not ok:
        raise HTTPException(status_code=403, detail="Sinalização não encontrada")
    return {"sucesso": True}


@router.post("/sinalizacoes/{sinalizacao_id}/feedback")
async def registrar_sinalizacao_feedback(
    sinalizacao_id: int, body: SinalizacaoFeedbackRequest, faiston_token: str = Cookie(None)
):
    sess = await _autenticar(faiston_token)
    ok = await run_in_threadpool(sinalizacoes.registrar_feedback, sinalizacao_id, sess["id"], body.feedback)
    if not ok:
        raise HTTPException(status_code=403, detail="Sinalização não encontrada")
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


@router.patch("/documentos/{documento_id}/onboarding")
async def definir_ordem_onboarding_endpoint(
    documento_id: int, body: OrdemOnboardingRequest, faiston_token: str = Cookie(None)
):
    """Marca (ou remove) a posição de um documento na trilha de
    onboarding (capacidade E, ensinar) — Fase 6."""
    await _exigir_admin(faiston_token)
    from app.assistente.ingestao import definir_ordem_onboarding

    try:
        ok = await run_in_threadpool(definir_ordem_onboarding, documento_id, body.ordem_onboarding)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    return {"sucesso": True}


@router.delete("/documentos/{documento_id}")
async def remover_documento_endpoint(documento_id: int, faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.ingestao import remover_documento

    ok = await run_in_threadpool(remover_documento, documento_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    return {"sucesso": True}


# --- Disparo manual do job diário (Fase 5 — só pra teste/depuração) ------
# O job de verdade roda sozinho, de madrugada, via APScheduler (main.py).
# Este endpoint existe só pra não precisar esperar até 3h da manhã pra
# validar a capacidade D no ambiente de teste — mesmo gate de admin dos
# outros endpoints de gestão, nunca exposto no piloto geral.

@router.post("/observar/rodar-agora")
async def rodar_observar_agora(faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.capacidade_observar.job import rodar

    resumo = await rodar()
    return resumo


@router.get("/observar/diagnostico")
async def diagnostico_observar_endpoint(usuario_id: Optional[int] = None, faiston_token: str = Cookie(None)):
    """Contagem/metadados de sinalização de um usuário -- nunca o texto
    (regra 8 continua valendo pro conteúdo). Só pra depurar por que
    alguém não recebeu sinalização (ver capacidade_observar/diagnostico.py).
    Sem `usuario_id` na query, mira quem está logado."""
    sess = await _exigir_admin(faiston_token)
    from app.assistente.capacidade_observar.diagnostico import resumo

    alvo = usuario_id if usuario_id is not None else sess["id"]
    resultado = await run_in_threadpool(resumo, alvo)
    if resultado.get("erro"):
        raise HTTPException(status_code=500, detail=resultado["erro"])
    return resultado


@router.post("/observar/popular-dados-teste")
async def popular_dados_teste_endpoint(
    body: PopularDadosTesteRequest = PopularDadosTesteRequest(), faiston_token: str = Cookie(None)
):
    """Gera dados sintéticos (tarefas repetidas, retrabalho, pendência
    parada) pra ver a capacidade D detectar alguma coisa sem esperar
    dado real acumular. `usuario_id` no corpo mira outra pessoa (pra
    testar sinalização de mais de uma conta); sem corpo, mira quem está
    logado. Exceção deliberada à regra 1 (só lê) -- ferramenta de QA,
    admin-only, dado sempre marcado com `cliente = 'Cliente Teste
    Observar'` (ver capacidade_observar/dados_teste.py)."""
    sess = await _exigir_admin(faiston_token)
    from app.assistente.capacidade_observar.dados_teste import popular

    alvo = body.usuario_id if body.usuario_id is not None else sess["id"]
    resultado = await run_in_threadpool(popular, alvo)
    if resultado.get("erro") == "usuario_nao_encontrado":
        raise HTTPException(status_code=404, detail=f"usuario_id {alvo} não encontrado")
    if resultado.get("erro"):
        raise HTTPException(status_code=500, detail=resultado["erro"])
    return resultado


@router.post("/observar/limpar-dados-teste")
async def limpar_dados_teste_endpoint(faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.capacidade_observar.dados_teste import limpar

    resultado = await run_in_threadpool(limpar)
    if resultado.get("erro"):
        raise HTTPException(status_code=500, detail=resultado["erro"])
    return resultado


# --- Dados de demonstração (usuários + tarefas variadas, fora da -------
# --- capacidade D) -- mesma ressalva de dados_teste.py: ferramenta de --
# --- QA, exceção deliberada à regra 1 (só lê), admin-only, nunca ------
# --- chamada por nenhuma capacidade conversacional. ---------------------

@router.post("/demo/popular")
async def popular_demo_endpoint(faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.dados_demo import popular

    resultado = await run_in_threadpool(popular)
    if resultado.get("erro"):
        raise HTTPException(status_code=500, detail=resultado["erro"])
    return resultado


@router.post("/demo/limpar")
async def limpar_demo_endpoint(faiston_token: str = Cookie(None)):
    await _exigir_admin(faiston_token)
    from app.assistente.dados_demo import limpar

    resultado = await run_in_threadpool(limpar)
    if resultado.get("erro"):
        raise HTTPException(status_code=500, detail=resultado["erro"])
    return resultado


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
