"""Capacidade B — explicar. Busca híbrida (vetorial + textual) fundida por
Reciprocal Rank Fusion. Regra 2 do CLAUDE.md do assistente: toda resposta
carrega os documentos de origem; sem trecho relevante, a resposta é
recusa — nunca conhecimento geral do modelo. Isso é reforçado no prompt
(sistema_explicar.md), não aqui: esta função só recupera trechos, nunca
decide se a pergunta "tem resposta".
"""
from typing import List, Optional, TypedDict

from app.assistente.db import get_conn_vector
from app.assistente.embeddings import embed_query

RRF_K = 60
_LIMITE_POR_BUSCA = 20
_TOP_K = 5


class Trecho(TypedDict):
    chunk_id: int
    documento_id: int
    documento_titulo: str
    texto: str
    score: float


def buscar_hibrido(pergunta: str, top_k: int = _TOP_K) -> Optional[List[Trecho]]:
    """Duas buscas (vetorial e textual), cada uma trazendo até
    `_LIMITE_POR_BUSCA` candidatos, fundidas por RRF: score = Σ 1/(60 +
    posição) somando as duas listas. Fica com os `top_k` melhores.

    A busca textual não é opcional — pega termo literal ("Fluxo B", sigla,
    código) que a vetorial sozinha erra.

    Devolve `None` em erro de banco/busca (o router trata como falha do
    assistente); `[]` só quando a busca rodou certinho mas não há nenhum
    documento indexado ainda (estado normal antes da primeira ingestão)."""
    conn = get_conn_vector()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        vetor = embed_query(pergunta)

        cur.execute(
            """
            SELECT c.id, c.documento_id, d.titulo, c.texto,
                   ROW_NUMBER() OVER (ORDER BY c.embedding <=> %s) AS posicao
            FROM documento_chunk c
            JOIN documento d ON d.id = c.documento_id
            WHERE d.ativo = true
            ORDER BY c.embedding <=> %s
            LIMIT %s
            """,
            (vetor, vetor, _LIMITE_POR_BUSCA),
        )
        resultado_vetorial = cur.fetchall()

        cur.execute(
            """
            SELECT c.id, c.documento_id, d.titulo, c.texto,
                   ROW_NUMBER() OVER (ORDER BY ts_rank(c.tsv, q) DESC) AS posicao
            FROM documento_chunk c
            JOIN documento d ON d.id = c.documento_id, plainto_tsquery('portuguese', %s) q
            WHERE d.ativo = true AND c.tsv @@ q
            ORDER BY ts_rank(c.tsv, q) DESC
            LIMIT %s
            """,
            (pergunta, _LIMITE_POR_BUSCA),
        )
        resultado_textual = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/capacidade_explicar] Erro na busca híbrida: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return None

    return fundir_rrf(resultado_vetorial, resultado_textual, top_k)


def fundir_rrf(resultado_vetorial: list, resultado_textual: list, top_k: int = _TOP_K) -> List[Trecho]:
    """Reciprocal Rank Fusion pura — sem banco, testável isolada. Cada
    linha de entrada é (chunk_id, documento_id, titulo, texto, posicao);
    score = Σ 1/(RRF_K + posição) somando as duas listas onde o chunk
    aparece."""
    scores: dict = {}
    dados: dict = {}
    for chunk_id, documento_id, titulo, texto, posicao in [*resultado_vetorial, *resultado_textual]:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + posicao)
        dados[chunk_id] = (documento_id, titulo, texto)

    melhores = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [
        {
            "chunk_id": chunk_id,
            "documento_id": dados[chunk_id][0],
            "documento_titulo": dados[chunk_id][1],
            "texto": dados[chunk_id][2],
            "score": score,
        }
        for chunk_id, score in melhores
    ]


def montar_mensagem_trechos(trechos: List[Trecho]) -> str:
    """Trechos numerados, na ordem que o prompt de sistema pede pra citar
    (ex.: [2]). O número é só a posição na lista — não é o chunk_id nem o
    documento_id, pra não vazar detalhe interno pro modelo/usuário."""
    partes = []
    for i, t in enumerate(trechos, start=1):
        partes.append(f"[{i}] ({t['documento_titulo']})\n{t['texto']}")
    return "\n\n".join(partes)


_TRECHO_PREVIA_CHARS = 220


def montar_fontes(trechos: List[Trecho]) -> List[dict]:
    """O que vai pro campo `fontes` do log e pro evento SSE `fontes` — o
    chip clicável mostra título + uma prévia curta, não o chunk inteiro
    (isso já foi usado pra redigir a resposta)."""
    fontes = []
    for t in trechos:
        trecho = t["texto"].strip()
        if len(trecho) > _TRECHO_PREVIA_CHARS:
            trecho = trecho[:_TRECHO_PREVIA_CHARS].rstrip() + "…"
        fontes.append({
            "documento_id": t["documento_id"],
            "titulo": t["documento_titulo"],
            "trecho": trecho,
        })
    return fontes
