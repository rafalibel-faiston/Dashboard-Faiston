"""Capacidade F (checkin diário) — puramente lógico: cursor falso pra
pendência/contexto/registro, e o gatilho de interceptação da resposta
no router. Roda sempre (não precisa de TEST_DATABASE_URL).
"""
import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.assistente import capacidade_checkin as checkin

_RAIZ = Path(__file__).resolve().parent.parent


def test_capacidade_checkin_so_usa_sql_literal_fixa():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_checkin.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas = 0
    for node in ast.walk(arvore):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            chamadas += 1
            primeiro_arg = node.args[0]
            assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str), (
                f"cur.execute() na linha {node.lineno} não usa string literal fixa"
            )
    assert chamadas >= 6


def test_capacidade_checkin_nao_importa_nada_de_llm():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_checkin.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    for node in ast.walk(arvore):
        if isinstance(node, ast.ImportFrom) and node.module and "llm" in node.module:
            raise AssertionError("capacidade_checkin.py importa de llm.py -- só router.py deve falar com o modelo")


class _CursorFalso:
    def __init__(self, respostas):
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


def test_pendente_hoje_true_quando_nao_existe_registro():
    cursor = _CursorFalso([None])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert checkin.pendente_hoje(1) is True
    finally:
        mp.undo()


def test_pendente_hoje_false_quando_ja_existe_registro():
    cursor = _CursorFalso([(1,)])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert checkin.pendente_hoje(1) is False
    finally:
        mp.undo()


def test_aguardando_resposta_true_quando_resumo_enviado_sem_resposta():
    cursor = _CursorFalso([(1,)])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert checkin.aguardando_resposta(1) is True
    finally:
        mp.undo()


def test_aguardando_resposta_false_quando_ja_respondeu():
    cursor = _CursorFalso([None])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert checkin.aguardando_resposta(1) is False
    finally:
        mp.undo()


def test_contexto_ontem_monta_dado_real_e_plano_anterior():
    cursor = _CursorFalso([
        (3, 7200),  # COUNT/SUM
        [("Acionamento cliente X", "Cliente A"), ("Auditoria Y", "Cliente B")],  # exemplos
        ("Vou revisar os chamados pendentes",),  # plano de ontem
    ])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        contexto = checkin.contexto_ontem(1)
    finally:
        mp.undo()
    assert contexto["tarefas_concluidas"] == 3
    assert contexto["tempo_total_s"] == 7200
    assert len(contexto["exemplos"]) == 2
    assert contexto["plano_que_a_pessoa_tinha_dito_ontem"] == "Vou revisar os chamados pendentes"
    assert contexto["data"] == (date.today() - timedelta(days=1)).isoformat()


def test_contexto_ontem_sem_plano_anterior_devolve_none():
    cursor = _CursorFalso([
        (0, 0),
        [],
        None,
    ])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        contexto = checkin.contexto_ontem(1)
    finally:
        mp.undo()
    assert contexto["plano_que_a_pessoa_tinha_dito_ontem"] is None


def test_marcar_enviado_e_registrar_resposta_usam_get_conn():
    cursor = _CursorFalso([None])
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor))
        assert checkin.marcar_enviado(1, "resumo de teste") is True
    finally:
        mp.undo()

    cursor2 = _CursorFalso([None])
    mp2 = pytest.MonkeyPatch()
    try:
        mp2.setattr(checkin, "get_conn", lambda: _ConexaoFalsa(cursor2))
        assert checkin.registrar_resposta(1, "vou revisar chamados") is True
    finally:
        mp2.undo()


def test_banco_offline_nunca_lanca_excecao():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(checkin, "get_conn", lambda: None)
        assert checkin.pendente_hoje(1) is False
        assert checkin.aguardando_resposta(1) is False
        assert checkin.contexto_ontem(1) == {"erro": "banco_offline"}
        assert checkin.marcar_enviado(1, "x") is False
        assert checkin.registrar_resposta(1, "x") is False
    finally:
        mp.undo()


# --- router: intercepta a próxima pergunta quando aguardando resposta ---

def test_router_intercepta_resposta_quando_aguardando_checkin():
    import sys
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.assistente import router as router_mod, db

    app = FastAPI()
    app.include_router(router_mod.router)
    sess = {"id": 1, "nome": "Teste", "perfil": "admin", "time": "Projetos"}

    capturado = {}
    with patch.object(db, "get_session", lambda token: sess), \
         patch.object(router_mod, "aguardando_resposta_checkin", lambda uid: True), \
         patch.object(router_mod, "registrar_resposta_checkin", lambda uid, resp: capturado.setdefault("resposta", resp) or True), \
         patch.object(router_mod.assistente_log, "criar_pergunta", lambda *a, **k: 1), \
         patch.object(router_mod.assistente_log, "finalizar", lambda *a, **k: None):
        client = TestClient(app)
        resp = client.post(
            "/assistente/pergunta",
            json={"pergunta": "vou revisar os chamados abertos"},
            cookies={"faiston_token": "x"},
        )
    assert resp.status_code == 200
    assert "capacidade\": \"checkin\"" in resp.text
    assert capturado["resposta"] == "vou revisar os chamados abertos"
