"""Capacidade A (achar) — formatação de resposta e validação do
catálogo, puramente lógico: não depende de banco nem de chamada ao
modelo, roda sempre (não precisa de TEST_DATABASE_URL).
"""
import ast
from pathlib import Path

import pytest

from app.assistente.capacidade_achar import formatar_resposta
from app.assistente.catalogo import executar


# --- Critério de aceite da Fase 4: nada de SQL construída a partir da --
# --- saída do modelo. Checagem estática, não só revisão manual. -------

def test_nenhuma_sql_e_montada_dinamicamente_no_catalogo():
    codigo = (Path(__file__).resolve().parent.parent / "app" / "assistente" / "catalogo.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas_execute = 0
    for node in ast.walk(arvore):
        eh_execute = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        )
        if not eh_execute:
            continue
        chamadas_execute += 1
        primeiro_arg = node.args[0]
        assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str), (
            f"cur.execute() na linha {node.lineno} de catalogo.py não usa string "
            "literal fixa como query -- pode estar montando SQL dinamicamente a "
            "partir de argumento do modelo"
        )
    assert chamadas_execute >= 4, "esperava pelo menos uma query por função do catálogo"


def test_catalogo_nao_expoe_tarefa_de_outra_pessoa_pra_nao_admin():
    """Regressão de um vazamento real: `tarefas_por_cliente` e
    `atividades_campo_pendentes` liam a base inteira, sem escopo por
    pessoa -- passava despercebido enquanto o piloto era só admin, mas
    virou vazamento assim que perfil 'funcionario' ganhou acesso ao
    assistente (regra 5: a consulta roda com a permissão de quem
    perguntou).

    Toda query sobre `tarefas`/`status_atividades` no catálogo agora tem
    que ou escopar por quem perguntou, ou estar num ramo explícito de
    admin. `escala_n2` fica de fora de propósito: escala de plantão é
    informação compartilhada por natureza (todo mundo precisa saber quem
    está de sobreaviso).
    """
    codigo = (Path(__file__).resolve().parent.parent / "app" / "assistente" / "catalogo.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)

    def dentro_de_ramo_admin(no, raiz):
        """True se `no` está dentro de um `if sess["perfil"] == "admin"`."""
        for pai in ast.walk(raiz):
            if isinstance(pai, ast.If) and "admin" in ast.dump(pai.test):
                for filho in ast.walk(pai):
                    if filho is no:
                        # só conta se estiver no corpo do if, não no else
                        for do_corpo in pai.body:
                            for n in ast.walk(do_corpo):
                                if n is no:
                                    return True
        return False

    verificadas = 0
    for node in ast.walk(arvore):
        eh_execute = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        )
        if not eh_execute:
            continue
        sql = node.args[0].value
        if "FROM tarefas" not in sql and "FROM status_atividades" not in sql:
            continue
        verificadas += 1
        escopada = (
            "usuario_id = %s" in sql
            or "tecnico ILIKE %s" in sql
            or "n2_responsavel ILIKE %s" in sql
        )
        assert escopada or dentro_de_ramo_admin(node, arvore), (
            f"query na linha {node.lineno} de catalogo.py lê tarefas/status_atividades "
            "sem escopar por quem perguntou e fora de um ramo de admin -- "
            "expõe dado de outra pessoa pra perfil não-admin"
        )
    assert verificadas >= 4


# --- formatar_resposta: nunca o modelo calcula, sempre o código -------

def test_minhas_tarefas_status_especifico():
    texto = formatar_resposta("minhas_tarefas", {"status": "aberto", "total": 3})
    assert "3" in texto
    assert "aberto" not in texto.lower() or "aberta" in texto.lower()  # usa o rótulo, não o valor cru


def test_minhas_tarefas_por_status():
    texto = formatar_resposta("minhas_tarefas", {"por_status": {"aberto": 2, "concluido": 5}})
    assert "2" in texto and "5" in texto


def test_minhas_tarefas_vazio():
    texto = formatar_resposta("minhas_tarefas", {"por_status": {}})
    assert "não tem nenhuma tarefa" in texto.lower()


def test_tarefas_por_cliente_nao_encontrado():
    texto = formatar_resposta("tarefas_por_cliente", {"cliente": "Cliente Inexistente", "encontrado": False})
    assert "Cliente Inexistente" in texto
    assert "não encontrei" in texto.lower()


def test_tarefas_por_cliente_encontrado():
    texto = formatar_resposta(
        "tarefas_por_cliente",
        {"cliente": "Arcos Dourados", "encontrado": True, "por_status": {"aberto": 4}},
    )
    assert "Arcos Dourados" in texto
    assert "4" in texto


def test_escala_n2_vazia():
    texto = formatar_resposta("escala_n2_do_dia", {"data": "2026-08-24", "escala": []})
    assert "2026-08-24" in texto
    assert "ninguém" in texto.lower()


def test_escala_n2_com_gente():
    texto = formatar_resposta(
        "escala_n2_do_dia",
        {"data": "2026-08-24", "escala": [{"nome": "João", "horario_entrada": "08:00", "modalidade": "presencial", "atribuicao": ""}]},
    )
    assert "João" in texto
    assert "08:00" in texto


def test_atividades_pendentes_vazio():
    texto = formatar_resposta("atividades_campo_pendentes", {"dias": 7, "total": 0, "atividades": []})
    assert "7" in texto
    assert "nenhuma" in texto.lower() or "0" not in texto  # não deve nem citar "0 atividades"


def test_atividades_pendentes_com_dados():
    texto = formatar_resposta(
        "atividades_campo_pendentes",
        {
            "dias": 10,
            "total": 1,
            "atividades": [{"id": 1, "cliente": "NTT", "site": "SP01", "status": "agendado", "data": "2026-08-01"}],
        },
    )
    assert "NTT" in texto
    assert "10" in texto


def test_buscar_carimbo_nenhum_encontrado():
    texto = formatar_resposta("buscar_carimbo", {"termo": "xpto", "encontrados": []})
    assert "não encontrei" in texto.lower()
    assert "xpto" in texto


def test_buscar_carimbo_um_encontrado_devolve_conteudo_ao_pe_da_letra():
    conteudo = "Prezado cliente, seu chamado foi encerrado. Att, equipe Faiston."
    texto = formatar_resposta(
        "buscar_carimbo",
        {"termo": "encerramento", "encontrados": [
            {"id": 1, "titulo": "Encerramento padrão", "categoria": "Atendimento", "conteudo": conteudo},
        ]},
    )
    assert conteudo in texto
    assert "Encerramento padrão" in texto


def test_buscar_carimbo_varios_encontrados_lista_pra_escolher():
    texto = formatar_resposta(
        "buscar_carimbo",
        {"termo": "acion", "encontrados": [
            {"id": 1, "titulo": "Acionamento N2", "categoria": "Campo", "conteudo": "..."},
            {"id": 2, "titulo": "Acionamento cliente", "categoria": "Atendimento", "conteudo": "..."},
        ]},
    )
    assert "Acionamento N2" in texto
    assert "Acionamento cliente" in texto


def test_invalido_vira_recusa_honesta():
    texto = formatar_resposta("_invalido", {})
    assert texto == "Não consegui entender qual informação você precisa."


def test_erro_banco_vira_mensagem_generica_sem_stacktrace():
    texto = formatar_resposta("minhas_tarefas", {"erro": "banco_offline"})
    assert "Traceback" not in texto
    assert "não consegui" in texto.lower()


# --- catalogo.executar: nunca roda função que não está no catálogo -----

def test_executar_funcao_inexistente_gera_valueerror():
    with pytest.raises(ValueError):
        executar("funcao_que_o_modelo_inventou", {}, {"id": 1})


def test_executar_minhas_tarefas_status_invalido_gera_valueerror():
    with pytest.raises(ValueError):
        executar("minhas_tarefas", {"status": "status_que_nao_existe"}, {"id": 1})


def test_executar_tarefas_por_cliente_sem_cliente_gera_valueerror():
    with pytest.raises(ValueError):
        executar("tarefas_por_cliente", {"cliente": ""}, {"id": 1})


def test_executar_escala_data_invalida_gera_valueerror():
    with pytest.raises(ValueError):
        executar("escala_n2_do_dia", {"data": "não é uma data"}, {"id": 1})


def test_executar_atividades_dias_fora_da_faixa_gera_valueerror():
    with pytest.raises(ValueError):
        executar("atividades_campo_pendentes", {"dias": 9999}, {"id": 1})


def test_executar_argumento_nao_declarado_gera_typeerror():
    # o modelo "inventou" um parâmetro que a função não tem
    with pytest.raises(TypeError):
        executar("minhas_tarefas", {"usuario_id_de_outra_pessoa": 999}, {"id": 1})


def test_executar_buscar_carimbo_termo_vazio_gera_valueerror():
    with pytest.raises(ValueError):
        executar("buscar_carimbo", {"termo": "   "}, {"id": 1, "perfil": "funcionario", "time": "Projetos"})
