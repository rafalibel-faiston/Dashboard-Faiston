"""Critério de aceite da Fase 2 (capacidade resumir): todo número que
aparece no texto gerado pelo modelo tem que vir do JSON de entrada — o
modelo nunca calcula nada (regra 3 do CLAUDE.md do assistente).

Puramente lógico: não depende de banco nem de chamada ao modelo, roda
sempre (não precisa de TEST_DATABASE_URL).
"""
import ast
import inspect
from pathlib import Path

from app.assistente.capacidade_resumir import montar_agregado_semana, todos_numeros_batem

_RAIZ = Path(__file__).resolve().parent.parent


def test_montar_agregado_semana_exige_usuario_id():
    # Regressão de um bug real: a versão original desta função devolvia
    # o agregado da equipe inteira (nome + número de todo mundo) pra
    # qualquer pessoa que perguntasse -- violava a regra 5 (a consulta
    # roda com a permissão de quem perguntou) assim que o piloto deixou
    # de ser só admin. A assinatura exigir usuario_id/nome é a barreira
    # mínima pra isso não voltar via chamada sem argumento.
    parametros = list(inspect.signature(montar_agregado_semana).parameters)
    assert parametros[0] == "usuario_id"
    assert parametros[1] == "nome"


def test_capacidade_resumir_toda_query_de_tarefas_escopada_por_usuario():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_resumir.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas = 0
    for node in ast.walk(arvore):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            primeiro_arg = node.args[0]
            assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str)
            sql = primeiro_arg.value
            if "FROM tarefas" in sql or "FROM status_atividades" in sql:
                chamadas += 1
                assert "usuario_id = %s" in sql or "tecnico ILIKE %s" in sql, (
                    f"query na linha {node.lineno} lê tarefas/status_atividades sem escopar "
                    "por quem perguntou"
                )
    assert chamadas >= 4

AGREGADO = {
    "periodo_inicio": "2026-08-17",
    "periodo_fim": "2026-08-23",
    "concluidas_semana": 14,
    "horas_concluidas_semana": 3.5,
    "abertas_hoje": 2,
    "por_cliente": [
        {"cliente": "Arcos Dourados", "concluidas_semana": 11, "abertas_hoje": 3},
    ],
    "limite_dias_atraso": 7,
    "tickets_abertos_acima_do_limite": 5,
    "atendimentos_campo_concluidos_na_semana": 6,
}


def test_texto_so_com_numeros_do_json_passa():
    texto = (
        "Você concluiu 14 tarefas esta semana, somando 3.5 horas, e tem 2 abertas hoje. "
        "O cliente Arcos Dourados teve 11 tarefas concluídas e 3 em aberto. "
        "Há 5 tickets abertos há mais de 7 dias e 6 atendimentos de campo concluídos na semana."
    )
    assert todos_numeros_batem(texto, AGREGADO)


def test_texto_com_numero_inventado_reprova():
    # 42 não existe em lugar nenhum do agregado -- o modelo teria calculado
    # ou inventado, o que a regra 3 proíbe.
    texto = "Você concluiu 42 tarefas esta semana."
    assert not todos_numeros_batem(texto, AGREGADO)


def test_texto_com_percentual_calculado_reprova():
    # Percentual/variação não está no JSON -- teria sido calculado pelo
    # modelo, proibido mesmo que os números-base individualmente existam.
    texto = "Você teve 20% de aumento em relação à semana passada."
    assert not todos_numeros_batem(texto, AGREGADO)


def test_texto_sem_numero_nenhum_sempre_passa():
    assert todos_numeros_batem("Nenhuma tarefa foi concluída nesta janela.", AGREGADO)


def test_agregado_vazio_so_aceita_texto_sem_numero():
    vazio = {"concluidas_semana": 0, "abertas_hoje": 0, "por_cliente": [],
              "atendimentos_campo_concluidos_na_semana": 0}
    assert todos_numeros_batem("Nada foi concluído nesta semana.", vazio)
    assert todos_numeros_batem("Zero atendimentos e 0 tickets atrasados.", vazio)
    assert not todos_numeros_batem("Foram 3 atendimentos.", vazio)
