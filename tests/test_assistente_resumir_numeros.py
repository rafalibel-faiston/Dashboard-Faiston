"""Critério de aceite da Fase 2 (capacidade resumir): todo número que
aparece no texto gerado pelo modelo tem que vir do JSON de entrada — o
modelo nunca calcula nada (regra 3 do CLAUDE.md do assistente).

Puramente lógico: não depende de banco nem de chamada ao modelo, roda
sempre (não precisa de TEST_DATABASE_URL).
"""
from app.assistente.capacidade_resumir import todos_numeros_batem

AGREGADO = {
    "periodo_inicio": "2026-08-17",
    "periodo_fim": "2026-08-23",
    "por_funcionario": [
        {"nome": "João", "concluidas_semana": 14, "horas_concluidas_semana": 3.5, "abertas_hoje": 2},
        {"nome": "Fernanda", "concluidas_semana": 9, "horas_concluidas_semana": 12.0, "abertas_hoje": 0},
    ],
    "por_cliente": [
        {"cliente": "Arcos Dourados", "concluidas_semana": 11, "abertas_hoje": 3},
    ],
    "limite_dias_atraso": 7,
    "tickets_abertos_acima_do_limite": 5,
    "atendimentos_campo_concluidos_na_semana": 6,
}


def test_texto_so_com_numeros_do_json_passa():
    texto = (
        "João concluiu 14 tarefas esta semana, somando 3.5 horas, e tem 2 abertas hoje. "
        "Fernanda concluiu 9, com 12 horas. O cliente Arcos Dourados teve 11 tarefas "
        "concluídas e 3 em aberto. Há 5 tickets abertos há mais de 7 dias e 6 atendimentos "
        "de campo concluídos na semana."
    )
    assert todos_numeros_batem(texto, AGREGADO)


def test_texto_com_numero_inventado_reprova():
    # 42 não existe em lugar nenhum do agregado -- o modelo teria calculado
    # ou inventado, o que a regra 3 proíbe.
    texto = "A equipe concluiu 42 tarefas esta semana."
    assert not todos_numeros_batem(texto, AGREGADO)


def test_texto_com_percentual_calculado_reprova():
    # Percentual/variação não está no JSON -- teria sido calculado pelo
    # modelo, proibido mesmo que os números-base individualmente existam.
    texto = "A equipe teve 20% de aumento em relação à semana passada."
    assert not todos_numeros_batem(texto, AGREGADO)


def test_texto_sem_numero_nenhum_sempre_passa():
    assert todos_numeros_batem("Nenhuma tarefa foi concluída nesta janela.", AGREGADO)


def test_agregado_vazio_so_aceita_texto_sem_numero():
    vazio = {"por_funcionario": [], "por_cliente": [], "tickets_abertos_ha_mais_de_7_dias": 0,
              "atendimentos_campo_concluidos_na_semana": 0}
    assert todos_numeros_batem("Nada foi concluído nesta semana.", vazio)
    assert todos_numeros_batem("Zero atendimentos e 0 tickets atrasados.", vazio)
    assert not todos_numeros_batem("Foram 3 atendimentos.", vazio)
