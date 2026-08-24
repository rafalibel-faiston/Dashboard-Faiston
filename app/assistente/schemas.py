"""Contratos de entrada e saída do assistente OPS."""
from typing import Optional

from pydantic import BaseModel, Field


class PerguntaRequest(BaseModel):
    pergunta: str = Field(..., min_length=1, max_length=2000)
    contexto_tela: Optional[str] = None


class FeedbackRequest(BaseModel):
    log_id: int
    util: bool
