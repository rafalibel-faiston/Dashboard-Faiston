"""Geração de embedding, local em CPU — a Groq não tem API de embeddings
(regra do CLAUDE.md do assistente), então isso roda dentro do próprio
container, com `sentence-transformers`.

Variáveis de ambiente:
    EMBEDDING_MODEL   default: intfloat/multilingual-e5-small
    EMBEDDING_DIM      default: 384 — tem que bater com VECTOR(384) no
                        schema (documento_chunk). Trocar de modelo depois
                        exige migração + reindexação de tudo.

O modelo e5 espera prefixo "query: " na pergunta e "passage: " no trecho
de documento — sem isso a busca vetorial piora bastante. É por isso que
`embed_query` e `embed_passage` existem separados em vez de uma função
genérica: ninguém de fora deste módulo deveria montar esse prefixo à mão.
"""
import os
import threading
from typing import List

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))

_lock = threading.Lock()
_modelo = None


def _carregar_modelo():
    global _modelo
    if _modelo is None:
        with _lock:
            if _modelo is None:
                # Import tardio: sentence-transformers/torch são pesados pra
                # importar, e nada além deste módulo precisa deles — outros
                # arquivos do assistente sobem rápido mesmo se isto aqui
                # falhar (ex.: modelo não baixado ainda).
                from sentence_transformers import SentenceTransformer
                _modelo = SentenceTransformer(EMBEDDING_MODEL)
    return _modelo


def _codificar(textos: List[str]) -> List[List[float]]:
    modelo = _carregar_modelo()
    vetores = modelo.encode(textos, normalize_embeddings=True, convert_to_numpy=True)
    return [v.tolist() for v in vetores]


def embed_query(pergunta: str) -> List[float]:
    return _codificar([f"query: {pergunta}"])[0]


def embed_passage(texto: str) -> List[float]:
    return _codificar([f"passage: {texto}"])[0]


def embed_passages(textos: List[str]) -> List[List[float]]:
    """Em lote — usado pela ingestão, que gera embedding de vários blocos
    de um documento de uma vez (mais rápido que um a um)."""
    return _codificar([f"passage: {t}" for t in textos])
