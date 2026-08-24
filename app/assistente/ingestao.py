"""Ingestão de documento de procedimento (.md/.docx/.pdf) — quebra em
blocos, gera embedding e grava em documento/documento_chunk.

Reprocessar um documento apaga os blocos antigos dele antes de inserir os
novos (regra da especificação): a versão vigente é sempre a última
ingestão, nunca acumula lixo de versões anteriores.
"""
import re
from pathlib import Path
from typing import List, NamedTuple, Optional

from app.assistente.db import get_conn
from app.assistente.embeddings import embed_passages


class _Sentenca(NamedTuple):
    texto: str
    fim_paragrafo: bool


def _dividir_paragrafos(texto: str) -> List[str]:
    paragrafos = re.split(r"\n\s*\n", texto.strip())
    return [p.strip() for p in paragrafos if p.strip()]


def _dividir_sentencas(paragrafo: str) -> List[str]:
    partes = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ú0-9\"'(])", paragrafo.strip())
    return [p.strip() for p in partes if p.strip()]


def _tokenizar(texto: str) -> List[_Sentenca]:
    unidades: List[_Sentenca] = []
    for paragrafo in _dividir_paragrafos(texto):
        sentencas = _dividir_sentencas(paragrafo)
        for idx, s in enumerate(sentencas):
            unidades.append(_Sentenca(s, idx == len(sentencas) - 1))
    return unidades


def _preparar_overlap(atual: List[_Sentenca], sobreposicao: int):
    overlap: List[_Sentenca] = []
    palavras = 0
    for u in reversed(atual):
        if palavras >= sobreposicao:
            break
        overlap.insert(0, u)
        palavras += len(u.texto.split())
    return overlap, palavras


def quebrar_em_blocos(
    texto: str, alvo_min: int = 500, alvo_max: int = 700, sobreposicao: int = 80
) -> List[str]:
    """Blocos de alvo_min-alvo_max palavras, com `sobreposicao` palavras de
    overlap entre blocos consecutivos. Nunca corta no meio de frase: todo
    limite de bloco cai numa borda de sentença. Prioriza fechar o bloco
    numa borda de parágrafo assim que atinge alvo_min; só ultrapassa pra
    dentro do próximo parágrafo quando ainda não bateu o mínimo."""
    unidades = _tokenizar(texto)
    blocos: List[str] = []
    atual: List[_Sentenca] = []
    palavras_atual = 0
    i = 0
    while i < len(unidades):
        u = unidades[i]
        n = len(u.texto.split())
        cabe = palavras_atual + n <= alvo_max or not atual
        if cabe:
            atual.append(u)
            palavras_atual += n
            i += 1
            if palavras_atual >= alvo_min and u.fim_paragrafo:
                blocos.append(" ".join(x.texto for x in atual))
                atual, palavras_atual = _preparar_overlap(atual, sobreposicao)
        else:
            blocos.append(" ".join(x.texto for x in atual))
            atual, palavras_atual = _preparar_overlap(atual, sobreposicao)
    if atual:
        texto_final = " ".join(x.texto for x in atual).strip()
        if texto_final:
            blocos.append(texto_final)
    return blocos


def extrair_texto_md(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8")


def extrair_texto_docx(caminho: Path) -> str:
    import docx

    doc = docx.Document(str(caminho))
    paragrafos = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragrafos)


def _palavras_coladas(texto: str) -> bool:
    """Heurística pra detectar quando a extração perdeu os espaços (PDF
    que posiciona caractere por caractere sem glifo de espaço — comum em
    exportação de certas ferramentas). Compara caracteres totais contra
    número de "palavras" (separadas por qualquer espaço em branco): texto
    normal fica em torno de 5 caracteres por palavra; texto colado — no
    caso extremo, sem espaço nenhum — vira uma "palavra" só, enorme."""
    texto = texto.strip()
    if len(texto) < 50:
        return False
    palavras = texto.split()
    if not palavras:
        return False
    media = len(texto) / len(palavras)
    return media > 20


def extrair_texto_pdf(caminho: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(caminho))
    paginas = []
    for pagina in reader.pages:
        # extraction_mode="layout" reconstrói a posição 2D do texto em vez
        # de só detectar espaço por lacuna entre caracteres -- lida melhor
        # com PDF que não usa glifo de espaço de verdade entre palavras.
        # Mas em alguns PDFs (geradores/fontes específicos) o layout sai
        # vazio onde o modo padrão ainda extrai algo -- nesse caso, texto
        # colado é sempre melhor que nenhum texto, então cai pro "plain".
        texto_pagina = (pagina.extract_text(extraction_mode="layout") or "").strip()
        if not texto_pagina:
            texto_pagina = (pagina.extract_text() or "").strip()
        if texto_pagina:
            paginas.append(texto_pagina)
    texto = "\n\n".join(paginas)
    if _palavras_coladas(texto):
        print(
            "[assistente/ingestao] Aviso: texto extraído de "
            f"'{caminho.name}' parece sem espaços entre palavras "
            "(PDF sem glifo de espaço) — qualidade da busca pode ficar ruim "
            "pra este documento."
        )
    return texto


_EXTRATORES = {
    ".md": extrair_texto_md,
    ".markdown": extrair_texto_md,
    ".docx": extrair_texto_docx,
    ".pdf": extrair_texto_pdf,
}


def extrair_texto(caminho: Path) -> str:
    extensao = caminho.suffix.lower()
    extrator = _EXTRATORES.get(extensao)
    if not extrator:
        raise ValueError(f"Extensão não suportada: {extensao} (aceita .md, .docx, .pdf)")
    return extrator(caminho)


def ingerir_documento(
    titulo: str,
    conteudo_texto: str,
    origem: Optional[str] = None,
    versao: Optional[str] = None,
) -> Optional[int]:
    """Quebra, gera embedding (com prefixo 'passage: ', e o título do
    documento prefixado no texto antes do embedding — melhora a
    recuperação de blocos do meio do documento, que sozinhos perdem
    contexto) e grava. Reprocessar apaga os blocos antigos antes de
    inserir os novos. Devolve o documento_id, ou None se o banco estiver
    fora."""
    blocos = quebrar_em_blocos(conteudo_texto)
    if not blocos:
        return None

    textos_para_embedding = [f"{titulo}\n\n{bloco}" for bloco in blocos]
    embeddings = embed_passages(textos_para_embedding)

    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM documento WHERE titulo = %s",
            (titulo,),
        )
        row = cur.fetchone()
        if row:
            documento_id = row[0]
            cur.execute(
                "UPDATE documento SET origem = %s, versao = %s, atualizado_em = now(), ativo = true WHERE id = %s",
                (origem, versao, documento_id),
            )
            cur.execute("DELETE FROM documento_chunk WHERE documento_id = %s", (documento_id,))
        else:
            cur.execute(
                "INSERT INTO documento (titulo, origem, versao) VALUES (%s, %s, %s) RETURNING id",
                (titulo, origem, versao),
            )
            documento_id = cur.fetchone()[0]

        for ordem, (bloco, vetor) in enumerate(zip(blocos, embeddings)):
            cur.execute(
                "INSERT INTO documento_chunk (documento_id, ordem, texto, embedding) VALUES (%s, %s, %s, %s::vector)",
                (documento_id, ordem, bloco, vetor),
            )
        conn.commit()
        cur.close()
        conn.close()
        return documento_id
    except Exception as e:
        print(f"[assistente/ingestao] Erro ao ingerir '{titulo}': {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None


def listar_documentos() -> List[dict]:
    """Pra tela de admin (static/assistente/documentos.html): título,
    origem, versão e quantas blocos cada documento tem hoje."""
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.id, d.titulo, d.origem, d.versao, d.atualizado_em, d.ativo,
                   COUNT(c.id) AS blocos
            FROM documento d
            LEFT JOIN documento_chunk c ON c.documento_id = d.id
            GROUP BY d.id
            ORDER BY d.atualizado_em DESC
            """
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {
                "id": r[0],
                "titulo": r[1],
                "origem": r[2],
                "versao": r[3],
                "atualizado_em": r[4].isoformat() if r[4] else None,
                "ativo": r[5],
                "blocos": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[assistente/ingestao] Erro ao listar documentos: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return []


def remover_documento(documento_id: int) -> bool:
    """Remove o documento e, por ON DELETE CASCADE, todos os chunks dele —
    some da busca imediatamente."""
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM documento WHERE id = %s", (documento_id,))
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return ok
    except Exception as e:
        print(f"[assistente/ingestao] Erro ao remover documento {documento_id}: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False
