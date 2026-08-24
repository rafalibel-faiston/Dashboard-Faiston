"""Capacidade D (observar) — regra 8: a sinalização é da pessoa, nunca
sobre a pessoa. Este arquivo cobre o critério de aceite "inclua um teste
que tenta [acessar sinalização de outra pessoa] e espera 403" — em duas
camadas: a função de acesso (sinalizacoes.py, com cursor falso) e o
endpoint de verdade (router.py, via TestClient com FastAPI), pra garantir
tanto a query quanto o `raise HTTPException(403)` em cima dela.

Puramente lógico: banco e sessão são simulados, roda sempre (não precisa
de TEST_DATABASE_URL nem de app rodando de verdade).
"""
from app.assistente.capacidade_observar import sinalizacoes


# --- Camada 1: sinalizacoes.py com cursor falso, tabela em memória ------

class _CursorSinalizacao:
    """Simula a tabela `sinalizacao` como um dict em memória, só o
    suficiente pras três queries que marcar_vista/registrar_feedback
    disparam."""

    def __init__(self, linhas: dict):
        # linhas: {id: {"usuario_id": int, "vista_em": Any, "feedback": Any}}
        self._linhas = linhas
        self.rowcount = 0
        self._ultimo_select = None

    def execute(self, sql, params):
        if sql.strip().startswith("UPDATE sinalizacao SET vista_em"):
            sinalizacao_id, usuario_id = params
            linha = self._linhas.get(sinalizacao_id)
            if linha and linha["usuario_id"] == usuario_id and linha["vista_em"] is None:
                linha["vista_em"] = "agora"
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif sql.strip().startswith("SELECT 1 FROM sinalizacao"):
            sinalizacao_id, usuario_id = params
            linha = self._linhas.get(sinalizacao_id)
            self._ultimo_select = linha is not None and linha["usuario_id"] == usuario_id
        elif sql.strip().startswith("UPDATE sinalizacao SET feedback"):
            feedback, sinalizacao_id, usuario_id = params
            linha = self._linhas.get(sinalizacao_id)
            if linha and linha["usuario_id"] == usuario_id:
                linha["feedback"] = feedback
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:
            raise AssertionError(f"query inesperada: {sql[:80]!r}")

    def fetchone(self):
        return (1,) if self._ultimo_select else None

    def close(self):
        pass


class _ConexaoSinalizacao:
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


def test_marcar_vista_devolve_true_para_o_dono():
    cursor = _CursorSinalizacao({10: {"usuario_id": 1, "vista_em": None, "feedback": None}})
    mp = __import__("pytest").MonkeyPatch()
    try:
        mp.setattr(sinalizacoes, "get_conn", lambda: _ConexaoSinalizacao(cursor))
        assert sinalizacoes.marcar_vista(10, usuario_id=1) is True
    finally:
        mp.undo()


def test_marcar_vista_devolve_false_para_quem_nao_e_dono():
    cursor = _CursorSinalizacao({10: {"usuario_id": 1, "vista_em": None, "feedback": None}})
    mp = __import__("pytest").MonkeyPatch()
    try:
        mp.setattr(sinalizacoes, "get_conn", lambda: _ConexaoSinalizacao(cursor))
        assert sinalizacoes.marcar_vista(10, usuario_id=2) is False
    finally:
        mp.undo()


def test_registrar_feedback_devolve_false_para_quem_nao_e_dono():
    cursor = _CursorSinalizacao({10: {"usuario_id": 1, "vista_em": None, "feedback": None}})
    mp = __import__("pytest").MonkeyPatch()
    try:
        mp.setattr(sinalizacoes, "get_conn", lambda: _ConexaoSinalizacao(cursor))
        assert sinalizacoes.registrar_feedback(10, usuario_id=2, feedback=1) is False
    finally:
        mp.undo()


def test_registrar_feedback_devolve_true_para_o_dono():
    cursor = _CursorSinalizacao({10: {"usuario_id": 1, "vista_em": None, "feedback": None}})
    mp = __import__("pytest").MonkeyPatch()
    try:
        mp.setattr(sinalizacoes, "get_conn", lambda: _ConexaoSinalizacao(cursor))
        assert sinalizacoes.registrar_feedback(10, usuario_id=1, feedback=-1) is True
    finally:
        mp.undo()


# --- Camada 2: endpoint de verdade via TestClient, sessão simulada ------

def _montar_client_e_dependencias():
    """Monta um FastAPI só com o router do assistente, com `db.get_session`
    trocado por um mapa token->sessão e `sinalizacoes` trocado pela mesma
    tabela em memória da camada 1 -- pra testar o 403 saindo do endpoint
    de verdade, não só da função interna."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.assistente import db, router as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)

    sessoes = {
        "token-dono": {"id": 1, "perfil": "admin"},
        "token-outra-pessoa": {"id": 2, "perfil": "admin"},
    }
    tabela = {10: {"usuario_id": 1, "vista_em": None, "feedback": None}}
    cursor = _CursorSinalizacao(tabela)

    mp = __import__("pytest").MonkeyPatch()
    mp.setattr(db, "get_session", lambda token: sessoes.get(token))
    mp.setattr(
        router_mod.sinalizacoes, "get_conn", lambda: _ConexaoSinalizacao(cursor)
    )
    return TestClient(app), mp


def test_endpoint_visualizar_devolve_403_pra_quem_nao_e_dono():
    client, mp = _montar_client_e_dependencias()
    try:
        resp = client.post(
            "/assistente/sinalizacoes/10/visualizar", cookies={"faiston_token": "token-outra-pessoa"}
        )
        assert resp.status_code == 403
    finally:
        mp.undo()


def test_endpoint_visualizar_funciona_pro_dono():
    client, mp = _montar_client_e_dependencias()
    try:
        resp = client.post(
            "/assistente/sinalizacoes/10/visualizar", cookies={"faiston_token": "token-dono"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"sucesso": True}
    finally:
        mp.undo()


def test_endpoint_feedback_devolve_403_pra_quem_nao_e_dono():
    client, mp = _montar_client_e_dependencias()
    try:
        resp = client.post(
            "/assistente/sinalizacoes/10/feedback",
            json={"feedback": -2},
            cookies={"faiston_token": "token-outra-pessoa"},
        )
        assert resp.status_code == 403
    finally:
        mp.undo()
