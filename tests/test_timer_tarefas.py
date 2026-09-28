"""
Timer das tarefas no servidor (2026-09-28): várias tarefas contando tempo ao
mesmo tempo, sem depender da aba do navegador, e o PUT atualizando só o que
veio no corpo (antes as ações rápidas do quadro apagavam projeto/horário do
prazo e a tarefa "sumia" das visões por projeto).
"""
import uuid
from datetime import date, timedelta

import pytest

from tests.test_carga_equipe import _definir_senha


def _voltar_timer(tid: int, segundos: int):
    """Simula que o timer foi ligado `segundos` atrás, sem precisar dormir."""
    import main
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("UPDATE tarefas SET timer_inicio = timer_inicio - %s * INTERVAL '1 second' WHERE id=%s",
                (segundos, tid))
    conn.commit(); cur.close(); conn.close()


def _linha(tid: int):
    import main
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, segundos, timer_inicio IS NOT NULL, projeto_id, hora_prazo FROM tarefas WHERE id=%s", (tid,))
    r = cur.fetchone()
    cur.close(); conn.close()
    return r


@pytest.fixture()
def func(app, admin_client):
    """Funcionário logado + tarefas dele; limpa tudo no teardown."""
    from tests.conftest import login_client
    usuario = f"teste_timer_{uuid.uuid4().hex[:8]}"
    senha = "senhaTeste123"
    resp = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "senha": senha, "nome": "Timer Teste",
        "email": f"{usuario}@exemplo.teste", "perfil": "funcionario", "cargo": "backoffice",
    })
    assert resp.status_code == 200, resp.text
    uid = resp.json()["id"]
    _definir_senha(uid, senha)
    client = login_client(app, usuario, senha)
    tipo = client.get("/api/tipos-atividade").json()[0]["id"]
    criadas = []

    def criar(status="aberto", dias_prazo=10, **extra):
        body = {"descricao": "Tarefa timer", "cliente": "Demandas Gerais", "tipo_atividade_id": tipo,
                "data_prazo": (date.today() + timedelta(days=dias_prazo)).isoformat(), "status": status}
        body.update(extra)
        r = client.post("/api/tarefas", json=body)
        assert r.status_code == 200, r.text
        criadas.append(r.json()["id"])
        return r.json()

    yield {"id": uid, "client": client, "criar": criar}
    for tid in criadas:
        admin_client.delete(f"/api/tarefas/{tid}")
    admin_client.delete(f"/api/usuarios/{uid}")


class TestVariosTimers:
    def test_duas_tarefas_contam_ao_mesmo_tempo(self, func):
        c = func["client"]
        a, b = func["criar"]()["id"], func["criar"]()["id"]
        assert c.post(f"/api/tarefas/{a}/timer/iniciar").json()["timer_ativo"] is True
        assert c.post(f"/api/tarefas/{b}/timer/iniciar").json()["timer_ativo"] is True
        # Iniciar a segunda NÃO pausa a primeira
        assert _linha(a)[2] is True and _linha(b)[2] is True
        _voltar_timer(a, 100); _voltar_timer(b, 50)
        lista = {t["id"]: t for t in c.get("/api/tarefas?view=func").json()}
        assert lista[a]["timer_ativo"] and lista[b]["timer_ativo"]
        assert 100 <= lista[a]["timer_decorrido"] <= 105
        assert 50 <= lista[b]["timer_decorrido"] <= 55
        assert lista[a]["status"] == lista[b]["status"] == "em_andamento"

    def test_pausar_soma_a_sessao_e_e_idempotente(self, func):
        c = func["client"]
        tid = func["criar"]()["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 300)
        r = c.post(f"/api/tarefas/{tid}/timer/pausar").json()
        assert r["timer_ativo"] is False and 300 <= r["segundos"] <= 305
        # Pausar de novo não soma nada
        assert c.post(f"/api/tarefas/{tid}/timer/pausar").json()["segundos"] == r["segundos"]
        assert _linha(tid)[0] == "em_andamento"

    def test_iniciar_duas_vezes_nao_zera_a_contagem(self, func):
        c = func["client"]
        tid = func["criar"]()["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 120)
        r = c.post(f"/api/tarefas/{tid}/timer/iniciar").json()
        assert r["timer_decorrido"] >= 120

    def test_criar_em_andamento_ja_liga_o_timer(self, func):
        r = func["criar"](status="em_andamento")
        assert r["timer_ativo"] is True

    def test_nao_inicia_timer_de_tarefa_de_outro(self, func, admin_client):
        tid = func["criar"]()["id"]
        assert admin_client.post(f"/api/tarefas/{tid}/timer/iniciar").status_code == 404

    def test_consolidar_nao_muda_o_total(self, func):
        import main
        c = func["client"]
        tid = func["criar"]()["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 90)
        main.consolidar_timers()
        status, seg, rodando, *_ = _linha(tid)
        assert rodando and 90 <= seg <= 95
        t = next(x for x in c.get("/api/tarefas?view=func").json() if x["id"] == tid)
        assert t["timer_decorrido"] <= 2  # o tempo foi pra `segundos`, total igual


class TestPutParcial:
    def test_concluir_pelo_quadro_fecha_timer_e_preserva_campos(self, func):
        c = func["client"]
        tid = func["criar"](hora_prazo="15:30")["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 200)
        # Mesmo corpo que o botão ✓ do quadro manda
        r = c.put(f"/api/tarefas/{tid}", json={"descricao": "Tarefa timer", "cliente": "Demandas Gerais",
                                                "status": "concluido"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["status"] == "concluido" and j["timer_ativo"] is False
        assert 200 <= j["segundos"] <= 205 and j["concluido_em"]
        # hora_prazo não foi apagada (antes ia NULL)
        assert str(_linha(tid)[4])[:5] == "15:30"

    def test_arrastar_para_andamento_liga_e_para_aberto_desliga(self, func):
        c = func["client"]
        tid = func["criar"]()["id"]
        base = {"descricao": "Tarefa timer", "cliente": "Demandas Gerais"}
        assert c.put(f"/api/tarefas/{tid}", json={**base, "status": "em_andamento"}).json()["timer_ativo"] is True
        _voltar_timer(tid, 60)
        j = c.put(f"/api/tarefas/{tid}", json={**base, "status": "aberto"}).json()
        assert j["timer_ativo"] is False and j["segundos"] >= 60

    def test_eco_de_segundos_nao_zera_timer_rodando(self, func):
        """Cliente antigo que devolve os segundos que leu não pode apagar o que
        o timer contou."""
        c = func["client"]
        tid = func["criar"]()["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 1000)
        c.put(f"/api/tarefas/{tid}", json={"descricao": "Tarefa timer", "cliente": "Demandas Gerais",
                                            "status": "em_andamento", "segundos": 1000})
        t = next(x for x in c.get("/api/tarefas?view=func").json() if x["id"] == tid)
        assert t["timer_ativo"] and t["segundos"] + t["timer_decorrido"] >= 1000

    def test_edicao_manual_de_horas_vale_e_timer_segue(self, func):
        c = func["client"]
        tid = func["criar"]()["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 1000)
        j = c.put(f"/api/tarefas/{tid}", json={"descricao": "Tarefa timer", "cliente": "Demandas Gerais",
                                                "segundos": 7200}).json()
        assert j["segundos"] == 7200 and j["timer_ativo"] is True and j["timer_decorrido"] <= 2

    def test_concluir_atrasada_sem_justificativa_barra(self, func):
        c = func["client"]
        tid = func["criar"](dias_prazo=-3)["id"]
        r = c.put(f"/api/tarefas/{tid}", json={"descricao": "Tarefa timer", "cliente": "Demandas Gerais",
                                                "status": "concluido"})
        assert r.status_code == 400
        r = c.put(f"/api/tarefas/{tid}", json={"descricao": "Tarefa timer", "cliente": "Demandas Gerais",
                                                "status": "concluido", "justificativa_atraso": "Cliente atrasou"})
        assert r.status_code == 200 and r.json()["status"] == "concluido"

    def test_put_em_tarefa_de_outro_da_404_em_vez_de_sucesso_falso(self, func, admin_client):
        tid = func["criar"]()["id"]
        r = admin_client.put(f"/api/tarefas/{tid}", json={"descricao": "x", "cliente": "y", "status": "concluido"})
        assert r.status_code == 404
        assert _linha(tid)[0] == "aberto"

    def test_patch_segundos_legado_nunca_diminui(self, func):
        c = func["client"]
        tid = func["criar"]()["id"]
        c.post(f"/api/tarefas/{tid}/timer/iniciar")
        _voltar_timer(tid, 500)
        c.patch(f"/api/tarefas/{tid}/segundos", json={"segundos": 10})
        _st, seg, rodando, *_ = _linha(tid)
        assert rodando and seg >= 500
