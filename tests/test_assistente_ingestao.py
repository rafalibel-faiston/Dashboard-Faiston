"""Quebra em blocos da ingestão (capacidade B) — puramente lógico, não
depende de banco nem de embedding, roda sempre (não precisa de
TEST_DATABASE_URL).
"""
from app.assistente.ingestao import quebrar_em_blocos

_FRASE = (
    "Esta e a frase numero {i} do paragrafo, com texto suficiente para "
    "simular um procedimento operacional real da Faiston."
)


def _documento_sintetico(n_paragrafos=6, frases_por_paragrafo=15):
    paragrafos = []
    for p in range(n_paragrafos):
        frases = " ".join(_FRASE.format(i=i) for i in range(frases_por_paragrafo))
        paragrafos.append(f"Paragrafo {p}: {frases}")
    return "\n\n".join(paragrafos)


def test_blocos_ficam_dentro_da_faixa_exceto_o_ultimo():
    doc = _documento_sintetico()
    blocos = quebrar_em_blocos(doc, alvo_min=500, alvo_max=700, sobreposicao=80)
    assert len(blocos) >= 2
    for bloco in blocos[:-1]:
        n = len(bloco.split())
        assert 500 <= n <= 700, f"bloco fora da faixa: {n} palavras"


def test_nenhum_bloco_termina_no_meio_de_frase():
    doc = _documento_sintetico()
    blocos = quebrar_em_blocos(doc)
    for bloco in blocos:
        assert bloco.rstrip()[-1] in ".!?", f"bloco não termina em pontuação: ...{bloco[-40:]!r}"


def test_ha_overlap_entre_blocos_consecutivos():
    doc = _documento_sintetico()
    blocos = quebrar_em_blocos(doc, sobreposicao=80)
    assert len(blocos) >= 2
    for anterior, proximo in zip(blocos, blocos[1:]):
        palavras_finais_anterior = anterior.split()[-10:]
        palavras_iniciais_proximo = proximo.split()[:10]
        # pelo menos uma palavra do fim do bloco anterior reaparece no
        # começo do próximo -- confirma que existe overlap de verdade
        assert set(palavras_finais_anterior) & set(palavras_iniciais_proximo)


def test_documento_curto_vira_um_bloco_so():
    doc = "Paragrafo unico com poucas palavras. Segunda frase curta."
    blocos = quebrar_em_blocos(doc)
    assert len(blocos) == 1
    assert blocos[0].startswith("Paragrafo unico")


def test_documento_vazio_nao_gera_bloco():
    assert quebrar_em_blocos("") == []
    assert quebrar_em_blocos("   \n\n   ") == []


def test_paragrafo_unico_gigante_ainda_quebra_por_frase():
    # um paragrafo sozinho que estoura o alvo_max -- tem que quebrar
    # dentro dele por sentença, nunca no meio de uma frase
    frases = " ".join(_FRASE.format(i=i) for i in range(60))
    doc = f"Paragrafo enorme sem quebra: {frases}"
    blocos = quebrar_em_blocos(doc, alvo_min=500, alvo_max=700, sobreposicao=80)
    assert len(blocos) >= 2
    for bloco in blocos:
        assert bloco.rstrip()[-1] in ".!?"
