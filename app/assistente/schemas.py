"""Contratos de entrada e saída do assistente OPS."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PerguntaRequest(BaseModel):
    pergunta: str = Field(..., min_length=1, max_length=2000)
    contexto_tela: Optional[str] = None


class FeedbackRequest(BaseModel):
    log_id: int
    util: bool


class SinalizacaoFeedbackRequest(BaseModel):
    # 1 útil | -1 não útil | -2 nunca mais este detector
    feedback: Literal[1, -1, -2]


class OrdemOnboardingRequest(BaseModel):
    # posição do documento na trilha de onboarding; None remove da trilha
    ordem_onboarding: Optional[int] = None


class PopularDadosTesteRequest(BaseModel):
    # usuario_id alvo dos dados sintéticos; None = quem está chamando
    usuario_id: Optional[int] = None


class AnuncioEnvioRequest(BaseModel):
    # quantidade de destinatários que o admin viu na tela; o servidor
    # recusa o envio se a lista mudou desde então (ver router)
    confirmar_total: int
