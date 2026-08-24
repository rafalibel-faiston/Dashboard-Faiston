"""Cliente do modelo. Único arquivo que fala com a API do provedor —
regra 4 do CLAUDE.md do assistente: nada de nome de provedor fora daqui,
tudo vem de variável de ambiente. Trocar de provedor depois é editar só
este arquivo.

Variáveis de ambiente:
    LLM_BASE_URL   endpoint compatível com OpenAI (default: Groq)
    LLM_API_KEY    chave do provedor (cai para GROQ_API_KEY se não setada,
                   porque o resto do app já usa essa variável hoje —
                   ver /api/ia/insights em main.py)
    LLM_MODEL      id do modelo (default: llama-3.1-8b-instant, o mesmo já
                   validado nesta conta Groq)
    LLM_TIMEOUT_S  timeout da chamada em segundos (default: 30)
"""
import os
from typing import AsyncIterator, Optional, Tuple

from openai import AsyncOpenAI

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.1-8b-instant")
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


class ModeloIndisponivel(Exception):
    """Sem chave configurada, ou a chamada ao modelo falhou/deu timeout."""


async def completar_stream(
    mensagens: list[dict], temperature: float = 0.2
) -> AsyncIterator[Tuple[Optional[str], Optional[object]]]:
    """Gera a resposta token a token. Cada item é (delta, usage):
    delta com texto e usage None enquanto está gerando; delta None e
    usage preenchido no chunk final (stream_options include_usage é o que
    faz a contagem de tokens chegar no fim do stream)."""
    if not LLM_API_KEY:
        raise ModeloIndisponivel(
            "Nenhuma chave de API configurada (defina LLM_API_KEY ou GROQ_API_KEY)."
        )
    try:
        stream = await _client.chat.completions.create(
            model=LLM_MODEL,
            messages=mensagens,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.usage:
                yield None, chunk.usage
            elif chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content, None
    except ModeloIndisponivel:
        raise
    except Exception as e:
        raise ModeloIndisponivel(str(e)) from e
