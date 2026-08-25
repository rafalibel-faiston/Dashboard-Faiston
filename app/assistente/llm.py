"""Cliente do modelo. Único arquivo que fala com a API do provedor —
regra 4 do CLAUDE.md do assistente: nada de nome de provedor fora daqui,
tudo vem de variável de ambiente. Trocar de provedor depois é editar só
este arquivo.

Variáveis de ambiente:
    LLM_BASE_URL   endpoint compatível com OpenAI (default: Groq)
    LLM_API_KEY    chave do provedor (cai para GROQ_API_KEY se não setada,
                   porque o resto do app já usa essa variável hoje —
                   ver /api/ia/insights em main.py)
    LLM_MODEL      id do modelo (default: openai/gpt-oss-20b — a Groq
                   descontinuou llama-3.1-8b-instant em 17/jun/2026 e
                   recomenda este como substituto direto; conferir
                   console.groq.com/docs/models se voltar a dar 404)
    LLM_TIMEOUT_S  timeout da chamada em segundos (default: 30)
"""
import os
from types import SimpleNamespace
from typing import AsyncIterator, Optional, Tuple

import openai
from openai import AsyncOpenAI

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-20b")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "30"))

# Construir o client não faz chamada de rede — é seguro mesmo sem chave
# configurada. A falta de chave só vira erro na hora de usar (completar_stream),
# nunca na hora de importar o módulo. O assistente cair não pode derrubar
# a tela do OPS (regra do CLAUDE.md) — e um KeyError na import quebraria
# o main.py inteiro, não só o assistente.
_client = AsyncOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY or "sem-chave-configurada",
    timeout=LLM_TIMEOUT_S,
)

print(
    f"[assistente/llm] base_url={LLM_BASE_URL} model={LLM_MODEL} "
    f"chave_configurada={bool(LLM_API_KEY)}"
)


class ModeloIndisponivel(Exception):
    """Sem chave configurada, ou a chamada ao modelo falhou/deu timeout.

    `motivo` é o que vai pra assistente_log.motivo_falha — mais específico
    que a mensagem pro usuário, que fica sempre genérica (regra: nunca
    stack trace nem detalhe interno pro usuário, mas o log precisa ser
    diagnosticável)."""

    def __init__(self, mensagem: str, motivo: str = "erro_modelo"):
        super().__init__(mensagem)
        self.motivo = motivo


def _extrair_usage(chunk) -> Optional[object]:
    """A Groq manda o uso do streaming em `x_groq` (chunk final), não no
    `usage` padrão da OpenAI -- e `x_groq` chega como dict puro (campo que
    o schema da SDK não conhece), não como objeto com atributo. Normaliza
    os formatos possíveis pro mesmo shape (objeto com prompt_tokens/
    completion_tokens) pra nenhum outro arquivo precisar saber da
    diferença -- é a única função que conhece esse detalhe do provedor."""
    x_groq = getattr(chunk, "x_groq", None)
    if isinstance(x_groq, dict):
        usage = x_groq.get("usage")
    else:
        usage = getattr(x_groq, "usage", None)
    if usage is None:
        usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        return SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
    return usage


def _classificar_erro(e: Exception) -> str:
    if isinstance(e, openai.AuthenticationError):
        return "chave_invalida"
    if isinstance(e, openai.NotFoundError):
        return "modelo_invalido"
    if isinstance(e, openai.RateLimitError):
        return "limite_taxa"
    if isinstance(e, openai.APITimeoutError):
        return "timeout"
    if isinstance(e, openai.APIConnectionError):
        return "sem_rede"
    return "erro_modelo"


async def completar_stream(
    mensagens: list[dict], temperature: float = 0.2
) -> AsyncIterator[Tuple[Optional[str], Optional[object]]]:
    """Gera a resposta token a token. Cada item é (delta, usage):
    delta com texto e usage None enquanto está gerando; delta None e
    usage preenchido no chunk final (ver _extrair_usage — a Groq manda o
    uso num campo próprio, não no padrão `stream_options` da OpenAI)."""
    if not LLM_API_KEY:
        raise ModeloIndisponivel(
            "Nenhuma chave de API configurada (defina LLM_API_KEY ou GROQ_API_KEY).",
            motivo="sem_chave",
        )
    try:
        # Sem stream_options aqui: é sintaxe da OpenAI, e a Groq historicamente
        # rejeita parâmetro que não reconhece (BadRequestError em toda chamada,
        # sem gerar um token sequer). A Groq expõe o uso de outro jeito no
        # streaming: campo `x_groq` no chunk final, não `chunk.usage` — ver
        # docs.groq.com. Lemos os dois pra não quebrar se um dia a Groq passar
        # a suportar o padrão OpenAI também.
        stream = await _client.chat.completions.create(
            model=LLM_MODEL,
            messages=mensagens,
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream:
            usage = _extrair_usage(chunk)
            if usage is not None:
                yield None, usage
            elif chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content, None
    except ModeloIndisponivel:
        raise
    except Exception as e:
        motivo = _classificar_erro(e)
        print(f"[assistente/llm] Erro ({motivo}) chamando {LLM_MODEL} em {LLM_BASE_URL}: {type(e).__name__}: {e}")
        raise ModeloIndisponivel(str(e), motivo=motivo) from e


async def completar_com_ferramentas(mensagens: list[dict], ferramentas: list[dict], temperature: float = 0.0):
    """Chamada sem streaming — usada pela capacidade 'achar'. O modelo só
    escolhe qual função do catálogo chamar e com que argumento; quem
    valida e executa é o backend (catalogo.py), nunca o modelo."""
    if not LLM_API_KEY:
        raise ModeloIndisponivel(
            "Nenhuma chave de API configurada (defina LLM_API_KEY ou GROQ_API_KEY).",
            motivo="sem_chave",
        )
    try:
        return await _client.chat.completions.create(
            model=LLM_MODEL,
            messages=mensagens,
            tools=ferramentas,
            tool_choice="auto",
            temperature=temperature,
        )
    except Exception as e:
        motivo = _classificar_erro(e)
        print(f"[assistente/llm] Erro ({motivo}) chamando {LLM_MODEL} com ferramentas: {type(e).__name__}: {e}")
        raise ModeloIndisponivel(str(e), motivo=motivo) from e
