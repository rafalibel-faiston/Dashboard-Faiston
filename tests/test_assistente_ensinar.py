"""Capacidade E (ensinar) — onboarding guiado, puramente lógico: cursor
falso pra progresso/etapa, e o gatilho por palavra-chave do router.
Roda sempre (não precisa de TEST_DATABASE_URL).
"""
import ast
from pathlib import Path

import pytest

from app.assistente import capacidade_ensinar as ensinar
from app.assistente.router import _eh_pedido_de_ensinar_continuar, _eh_pedido_de_ensinar_iniciar

_RAIZ = Path(__file__).resolve().parent.parent


def test_capacidade_ensinar_so_usa_sql_literal_fixa():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_ensinar.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas = 0
    for node in ast.walk(arvore):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            chamadas += 1
            primeiro_arg = node.args[0]
            assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str), (
                f"cur.execute() na linha {node.lineno} não usa string literal fixa"
            )
    assert chamadas >= 5


# --- Gatilho por palavra-chave (router) ---------------------------------

def test_iniciar_dispara_com_onboarding():
    assert _eh_pedido_de_ensinar_iniciar("quero começar o onboarding")


def test_iniciar_dispara_com_modo_professor():
    assert _eh_pedido_de_ensinar_iniciar("entra em modo professor")


def test_iniciar_dispara_com_sou_novo_no_time():
    assert _eh_pedido_de_ensinar_iniciar("sou novo no time, o que preciso saber?")


def test_iniciar_nao_dispara_com_sou_novo_sozinho():
    # "sou novo" sem "time"/"equipe"/"aqui" não deve disparar sozinho --
    # frase comum demais pra virar gatilho isolada.
    assert not _eh_pedido_de_ensinar_iniciar("sou novo nisso, como funciona reset de senha?")


def test_iniciar_nao_dispara_em_pergunta_qualquer():
    assert not _eh_pedido_de_ensinar_iniciar("quantas tarefas eu tenho?")


def test_continuar_dispara_com_proxima_aula():
    assert _eh_pedido_de_ensinar_continuar("próxima aula")


def test_continuar_dispara_com_continuar_aula():
    assert _eh_pedido_de_ensinar_continuar("pode continuar aula")


def test_continuar_nao_dispara_com_proximo_sozinho():
    # "próximo" sozinho é comum demais em conversa normal -- só a frase
    # completa "próxima aula"/"continuar aula" conta.
    assert not _eh_pedido_de_ensinar_continuar("qual o próximo passo do chamado?")


# --- capacidade_ensinar.py com cursor/conexão falsos --------------------

class _CursorFalso:
    def __init__(self, respostas):
        # respostas: lista de valores que fetchone()/fetchall() devolve,
        # um por chamada de execute(), na ordem
        self._respostas = list(respostas)
        self._ultima = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self._ultima = self._respostas.pop(0) if self._respostas else None

    def fetchone(self):
        return self._ultima

    def fetchall(self):
        return self._ultima or []

    def close(self):
        pass


class _ConexaoFalsa:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_total_etapas_conta_documentos_com_ordem():
    cursor = _CursorFalso([(3,)])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert ensinar.total_etapas() == 3
    finally:
        mp.undo()


def test_total_etapas_banco_offline_devolve_zero():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "get_conn", lambda: None)
        assert ensinar.total_etapas() == 0
    finally:
        mp.undo()


def test_etapa_por_ordem_concatena_blocos_em_ordem():
    cursor = _CursorFalso([
        (5, "Como abrir chamado"),  # SELECT id, titulo
        [("Primeiro bloco.",), ("Segundo bloco.",)],  # SELECT texto ... ORDER BY ordem
    ])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "get_conn", lambda: _ConexaoFalsa(cursor))
        etapa = ensinar.etapa_por_ordem(1)
    finally:
        mp.undo()
    assert etapa["documento_id"] == 5
    assert etapa["titulo"] == "Como abrir chamado"
    assert "Primeiro bloco." in etapa["texto"]
    assert "Segundo bloco." in etapa["texto"]


def test_etapa_por_ordem_inexistente_devolve_none():
    cursor = _CursorFalso([None])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert ensinar.etapa_por_ordem(99) is None
    finally:
        mp.undo()


def test_progresso_atual_nunca_comecou_devolve_none():
    cursor = _CursorFalso([None])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert ensinar.progresso_atual(1) is None
    finally:
        mp.undo()


def test_progresso_atual_em_andamento():
    cursor = _CursorFalso([(2, None)])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "get_conn", lambda: _ConexaoFalsa(cursor))
        progresso = ensinar.progresso_atual(1)
    finally:
        mp.undo()
    assert progresso == {"etapa_atual": 2, "concluido": False}


def test_iniciar_ou_retomar_ja_tinha_progresso_nao_reinicia():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "progresso_atual", lambda usuario_id: {"etapa_atual": 3, "concluido": False})
        resultado = ensinar.iniciar_ou_retomar(1)
    finally:
        mp.undo()
    assert resultado == {"etapa_atual": 3, "concluido": False}


def test_avancar_sem_proxima_etapa_marca_concluido():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "progresso_atual", lambda usuario_id: {"etapa_atual": 3, "concluido": False})
        mp.setattr(ensinar, "total_etapas", lambda: 3)
        mp.setattr(ensinar, "get_conn", lambda: None)  # banco offline não impede o cálculo em memória
        resultado = ensinar.avancar(1)
    finally:
        mp.undo()
    assert resultado == {"etapa_atual": 3, "concluido": True}


def test_avancar_com_proxima_etapa_disponivel():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ensinar, "progresso_atual", lambda usuario_id: {"etapa_atual": 1, "concluido": False})
        mp.setattr(ensinar, "total_etapas", lambda: 3)
        mp.setattr(ensinar, "get_conn", lambda: None)
        resultado = ensinar.avancar(1)
    finally:
        mp.undo()
    assert resultado == {"etapa_atual": 2, "concluido": False}
