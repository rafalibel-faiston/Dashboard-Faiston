"""Capacidade D — redação. Único arquivo do pacote `capacidade_observar`
que fala com o modelo (via `app.assistente.llm`) — os detectores
(detectores.py) nunca calculam nada com IA, só SQL puro. O modelo aqui só
transforma o padrão já detectado em texto curto; se não achar ação
nenhuma pra sugerir, descarta a sinalização inteira (a válvula de escape
"DESCARTAR" do prompt, que mata a sinalização antes dela chegar em
alguém)."""
import json
from pathlib import Path
from typing import Optional

from app.assistente.llm import ModeloIndisponivel, completar_stream

_DIR = Path(__file__).resolve().parent.parent
_PROMPT_OBSERVAR = (_DIR / "prompts" / "sistema_observar.md").read_text(encoding="utf-8")


async def redigir(detector: str, evidencia: dict) -> Optional[str]:
    """Devolve o texto do aviso, ou `None` se o modelo descartou
    (DESCARTAR) ou a chamada falhou — nos dois casos a sinalização não é
    gravada. Um padrão real que não rende conselho nenhum é ruído, não
    aviso; e uma falha do modelo não pode virar sinalização com texto
    vazio ou quebrado."""
    mensagens = [
        {"role": "system", "content": _PROMPT_OBSERVAR},
        {"role": "user", "content": json.dumps({"detector": detector, **evidencia}, ensure_ascii=False)},
    ]
    partes = []
    try:
        async for delta, _usage in completar_stream(mensagens, temperature=0.3):
            if delta:
                partes.append(delta)
    except ModeloIndisponivel as e:
        print(f"[assistente/observar] Modelo indisponível ao redigir ({detector}): {e}")
        return None

    texto = "".join(partes).strip()
    if not texto or texto.upper().startswith("DESCARTAR"):
        return None
    return texto
