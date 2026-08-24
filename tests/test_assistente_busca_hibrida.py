"""Reciprocal Rank Fusion da capacidade B (explicar) — puramente lógico,
não depende de banco nem de embedding, roda sempre (não precisa de
TEST_DATABASE_URL).
"""
from app.assistente.capacidade_explicar import RRF_K, fundir_rrf


def test_chunk_nos_dois_resultados_fica_em_primeiro():
    # chunk 1: 2º na vetorial, 1º na textual -- soma os dois scores
    # chunk 2: só aparece na vetorial, em 1º
    vetorial = [
        (2, 10, "Doc A", "texto do chunk 2", 1),
        (1, 10, "Doc A", "texto do chunk 1", 2),
    ]
    textual = [
        (1, 10, "Doc A", "texto do chunk 1", 1),
    ]
    resultado = fundir_rrf(vetorial, textual, top_k=5)
    assert resultado[0]["chunk_id"] == 1
    score_esperado_1 = 1 / (RRF_K + 2) + 1 / (RRF_K + 1)
    score_esperado_2 = 1 / (RRF_K + 1)
    assert abs(resultado[0]["score"] - score_esperado_1) < 1e-9
    assert abs(resultado[1]["score"] - score_esperado_2) < 1e-9


def test_respeita_top_k():
    vetorial = [(i, 1, "Doc", f"texto {i}", i) for i in range(1, 11)]
    resultado = fundir_rrf(vetorial, [], top_k=5)
    assert len(resultado) == 5
    # os 5 primeiros por posição (score mais alto = posição mais baixa)
    assert [r["chunk_id"] for r in resultado] == [1, 2, 3, 4, 5]


def test_listas_vazias_devolve_vazio():
    assert fundir_rrf([], [], top_k=5) == []


def test_so_na_busca_textual_ainda_aparece():
    # termo literal (ex.: sigla) que só a busca textual pega
    textual = [(7, 3, "POP RMA", "CFOP 5915 aplica-se a...", 1)]
    resultado = fundir_rrf([], textual, top_k=5)
    assert len(resultado) == 1
    assert resultado[0]["chunk_id"] == 7
    assert resultado[0]["documento_titulo"] == "POP RMA"
