"""Capacidade D (observar) — critérios de aceite da especificação,
puramente lógicos: não dependem de banco real nem de chamada ao modelo
(usam conexão/cursor falsos), rodam sempre (não precisam de
TEST_DATABASE_URL).
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.assistente.capacidade_observar import detectores, job

_RAIZ = Path(__file__).resolve().parent.parent


# --- "Nenhuma query de detector chama o modelo" (critério de aceite) ---

def test_detectores_nao_importa_nada_de_llm():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_observar" / "detectores.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    for node in ast.walk(arvore):
        if isinstance(node, ast.ImportFrom) and node.module and "llm" in node.module:
            pytest.fail(f"detectores.py importa de {node.module!r} -- detector não pode falar com o modelo")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "llm" in alias.name:
                    pytest.fail(f"detectores.py importa {alias.name!r} -- detector não pode falar com o modelo")


def test_detectores_so_usa_sql_literal_fixa():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_observar" / "detectores.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas = 0
    for node in ast.walk(arvore):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            chamadas += 1
            primeiro_arg = node.args[0]
            assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str), (
                f"cur.execute() na linha {node.lineno} não usa string literal fixa"
            )
    assert chamadas >= 3


# --- Fakes de banco pra testar a transformação de linha -> achado ------

class _CursorFalso:
    def __init__(self, linhas):
        self._linhas = linhas

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._linhas

    def fetchone(self):
        return self._linhas[0] if self._linhas else None

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


def test_repeticao_identica_monta_achado_com_peso_em_tempo_total():
    agora = datetime(2026, 8, 20, 10, 0)
    linhas = [(7, "Acionamento", "Arcos Dourados", 14, agora - timedelta(days=4), agora, 13200)]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(detectores, "get_conn", lambda: _ConexaoFalsa(_CursorFalso(linhas)))
        achados = detectores.repeticao_identica()
    finally:
        mp.undo()
    assert len(achados) == 1
    a = achados[0]
    assert a["usuario_id"] == 7
    assert a["detector"] == "repeticao_identica"
    assert a["assinatura"] == "Acionamento::Arcos Dourados"
    assert a["peso"] == 13200.0
    assert a["evidencia"]["vezes"] == 14
    assert a["evidencia"]["tempo_total_s"] == 13200


def test_retrabalho_monta_achado_com_peso_proporcional_as_edicoes():
    agora = datetime(2026, 8, 20, 10, 0)
    linhas = [(3, 482, "Ajustar cadastro do cliente", 7, agora - timedelta(days=2), agora)]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(detectores, "get_conn", lambda: _ConexaoFalsa(_CursorFalso(linhas)))
        achados = detectores.retrabalho()
    finally:
        mp.undo()
    assert len(achados) == 1
    a = achados[0]
    assert a["usuario_id"] == 3
    assert a["detector"] == "retrabalho"
    assert a["assinatura"] == "tarefa::482"
    assert a["peso"] == 7 * 1800
    assert a["evidencia"]["tarefa_id"] == 482
    assert a["evidencia"]["vezes"] == 7


def test_pendencia_parada_calcula_atraso_sobre_o_p90():
    # parada ha 15 dias, normal pra esse tipo eh 4 dias -- atraso de 11 dias
    parada_ha_s = 15 * 86400
    p90_segundos = 4 * 86400
    linhas = [(9, 101, "Emitir relatório final", "Faturamento", parada_ha_s, p90_segundos)]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(detectores, "get_conn", lambda: _ConexaoFalsa(_CursorFalso(linhas)))
        achados = detectores.pendencia_parada()
    finally:
        mp.undo()
    assert len(achados) == 1
    a = achados[0]
    assert a["usuario_id"] == 9
    assert a["detector"] == "pendencia_parada"
    assert a["assinatura"] == "tipo::Faturamento"
    assert a["peso"] == pytest.approx(11 * 86400, rel=1e-6)
    assert a["evidencia"]["parada_ha_dias"] == 15.0
    assert a["evidencia"]["normal_dias"] == 4.0


def test_banco_offline_devolve_lista_vazia_sem_lancar_excecao():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(detectores, "get_conn", lambda: None)
        assert detectores.repeticao_identica() == []
        assert detectores.retrabalho() == []
        assert detectores.pendencia_parada() == []
    finally:
        mp.undo()


def test_rodar_todos_filtra_por_usuario_sem_tocar_a_query():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(detectores, "repeticao_identica", lambda: [
            {"usuario_id": 1, "detector": "repeticao_identica", "assinatura": "a", "peso": 1.0, "evidencia": {}},
            {"usuario_id": 2, "detector": "repeticao_identica", "assinatura": "b", "peso": 1.0, "evidencia": {}},
        ])
        mp.setattr(detectores, "retrabalho", lambda: [])
        mp.setattr(detectores, "pendencia_parada", lambda: [])

        todos = detectores.rodar_todos()
        assert len(todos) == 2
        so_do_1 = detectores.rodar_todos(usuario_id=1)
        assert len(so_do_1) == 1
        assert so_do_1[0]["usuario_id"] == 1
    finally:
        mp.undo()


# --- Orçamento (2/semana) e cooldown (30 dias) — critério de aceite ----

class _CursorOrcamento:
    """Cursor falso que sabe responder as três checagens diferentes que
    selecionar_candidatos faz (semana, desligado, cooldown), inspecionando
    um trecho estável do SQL de cada uma."""

    def __init__(self, sinalizacoes_essa_semana=None, detectores_desligados=None, em_cooldown=None):
        self._semana = sinalizacoes_essa_semana or {}
        self._desligados = detectores_desligados or set()
        self._cooldown = em_cooldown or set()
        self._ultimo = None

    def execute(self, sql, params):
        if "COUNT(*) FROM sinalizacao WHERE usuario_id = %s AND criado_em >" in sql:
            usuario_id = params[0]
            self._ultimo = ("semana", self._semana.get(usuario_id, 0))
        elif "feedback = -2" in sql:
            usuario_id, detector = params
            self._ultimo = ("desligado", (usuario_id, detector) in self._desligados)
        elif "interval '30 days'" in sql:
            usuario_id, detector, assinatura = params
            self._ultimo = ("cooldown", (usuario_id, detector, assinatura) in self._cooldown)
        else:
            raise AssertionError(f"query inesperada em selecionar_candidatos: {sql[:80]!r}")

    def fetchone(self):
        tipo, valor = self._ultimo
        if tipo == "semana":
            return (valor,)
        return (1,) if valor else None

    def close(self):
        pass


def _achado(usuario_id, detector, assinatura, peso):
    return {"usuario_id": usuario_id, "detector": detector, "assinatura": assinatura, "peso": peso, "evidencia": {}}


def test_orcamento_limita_a_dois_por_semana_e_prioriza_maior_peso():
    achados = [
        _achado(1, "repeticao_identica", "a", peso=100),
        _achado(1, "repeticao_identica", "b", peso=300),
        _achado(1, "repeticao_identica", "c", peso=200),
    ]
    cursor = _CursorOrcamento(sinalizacoes_essa_semana={1: 0})
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(job, "get_conn", lambda: _ConexaoFalsa(cursor))
        candidatos = job.selecionar_candidatos(achados)
    finally:
        mp.undo()
    assert len(candidatos[1]) == 2
    # os dois de maior peso sobrevivem (b=300, c=200); o de menor (a=100) descarta
    assinaturas = {a["assinatura"] for a in candidatos[1]}
    assert assinaturas == {"b", "c"}


def test_orcamento_ja_cheio_na_semana_nao_deixa_passar_nada():
    achados = [_achado(1, "repeticao_identica", "a", peso=999)]
    cursor = _CursorOrcamento(sinalizacoes_essa_semana={1: 2})  # já bateu o teto
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(job, "get_conn", lambda: _ConexaoFalsa(cursor))
        candidatos = job.selecionar_candidatos(achados)
    finally:
        mp.undo()
    assert 1 not in candidatos


def test_cooldown_de_30_dias_bloqueia_a_mesma_assinatura():
    achados = [_achado(1, "repeticao_identica", "Acionamento::Cliente X", peso=500)]
    cursor = _CursorOrcamento(
        sinalizacoes_essa_semana={1: 0},
        em_cooldown={(1, "repeticao_identica", "Acionamento::Cliente X")},
    )
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(job, "get_conn", lambda: _ConexaoFalsa(cursor))
        candidatos = job.selecionar_candidatos(achados)
    finally:
        mp.undo()
    assert 1 not in candidatos


def test_feedback_menos_dois_desliga_o_detector_permanentemente():
    achados = [_achado(1, "pendencia_parada", "tipo::Faturamento", peso=500)]
    cursor = _CursorOrcamento(
        sinalizacoes_essa_semana={1: 0},
        detectores_desligados={(1, "pendencia_parada")},
    )
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(job, "get_conn", lambda: _ConexaoFalsa(cursor))
        candidatos = job.selecionar_candidatos(achados)
    finally:
        mp.undo()
    assert 1 not in candidatos


# --- dados_teste.py (botão de QA na tela admin) -- também SQL literal --

def test_dados_teste_so_usa_sql_literal_fixa():
    codigo = (_RAIZ / "app" / "assistente" / "capacidade_observar" / "dados_teste.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas = 0
    for node in ast.walk(arvore):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            chamadas += 1
            primeiro_arg = node.args[0]
            assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str), (
                f"cur.execute() na linha {node.lineno} de dados_teste.py não usa string literal fixa"
            )
    assert chamadas >= 5


def test_pessoas_diferentes_tem_orcamento_independente():
    achados = [
        _achado(1, "repeticao_identica", "a", peso=100),
        _achado(2, "repeticao_identica", "b", peso=100),
    ]
    cursor = _CursorOrcamento(sinalizacoes_essa_semana={1: 2, 2: 0})
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(job, "get_conn", lambda: _ConexaoFalsa(cursor))
        candidatos = job.selecionar_candidatos(achados)
    finally:
        mp.undo()
    assert 1 not in candidatos
    assert 2 in candidatos
