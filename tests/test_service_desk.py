"""
Testes do Service Desk (app/service_desk): regras de turno (funções puras)
e API /api/sd/* -- acesso isolado por time/cargo, registro rápido, KPIs,
encerramento automático de status esquecido e escala 12x36.
"""
import uuid
from datetime import date, datetime, time, timedelta

import pytest

from app.service_desk.turno import deve_encerrar, dias_12x36, janela_contagem, janela_turno

SENHA = "senhaTeste123"


# ── Regras de turno ──────────────────────────────────────────────────────
class TestJanelaTurno:
    def test_noturno_de_madrugada_pertence_ao_turno_de_ontem(self):
        ini, fim = janela_turno(datetime(2026, 9, 24, 2, 0), time(19), time(7))
        assert ini == datetime(2026, 9, 23, 19, 0)
        assert fim == datetime(2026, 9, 24, 7, 0)

    def test_noturno_depois_das_19_abre_turno_de_hoje(self):
        ini, fim = janela_turno(datetime(2026, 9, 24, 20, 0), time(19), time(7))
        assert ini == datetime(2026, 9, 24, 19, 0)
        assert fim == datetime(2026, 9, 25, 7, 0)

    def test_noturno_durante_o_dia_mostra_ultimo_turno(self):
        ini, _ = janela_turno(datetime(2026, 9, 24, 12, 0), time(19), time(7))
        assert ini == datetime(2026, 9, 23, 19, 0)

    def test_diurno(self):
        ini, fim = janela_turno(datetime(2026, 9, 24, 10, 0), time(9), time(18))
        assert (ini, fim) == (datetime(2026, 9, 24, 9), datetime(2026, 9, 24, 18))

    def test_chegada_antecipada_ja_conta_no_turno_que_vai_comecar(self):
        ini, fim = janela_turno(datetime(2026, 9, 24, 17, 30), time(19), time(7))
        assert ini == datetime(2026, 9, 24, 19, 0)

    def test_contagem_e_continua_entre_turnos(self):
        # 12h de um noturno: ainda é hora extra do turno de ontem -- não some da lista
        ini, fim = janela_contagem(datetime(2026, 9, 24, 12, 0), time(19), time(7))
        assert (ini, fim) == (datetime(2026, 9, 23, 17, 0), datetime(2026, 9, 24, 17, 0))
        # diurno: 20h (hora extra) continua no turno das 09h
        ini, fim = janela_contagem(datetime(2026, 9, 24, 20, 0), time(9), time(18))
        assert (ini, fim) == (datetime(2026, 9, 24, 7, 0), datetime(2026, 9, 25, 7, 0))

    def test_sem_jornada_vale_dia_civil(self):
        ini, fim = janela_turno(datetime(2026, 9, 24, 10, 0), None, None)
        assert (ini, fim) == (datetime(2026, 9, 24), datetime(2026, 9, 25))


class TestEncerramentoAutomatico:
    def test_pausa_esquecida_fecha_no_fim_do_turno(self):
        # Pausa às 03h num turno 19h-07h, 4 dias depois ainda aberta (caso 96h do sistema antigo)
        inicio = datetime(2026, 9, 24, 3, 0)
        fim = deve_encerrar(inicio, time(19), time(7), ref=datetime(2026, 9, 28, 7, 0))
        assert fim == datetime(2026, 9, 24, 7, 0)

    def test_dentro_do_turno_nao_fecha(self):
        assert deve_encerrar(datetime(2026, 9, 24, 3, 0), time(19), time(7), ref=datetime(2026, 9, 24, 6, 0)) is None

    def test_sem_jornada_fecha_depois_de_12h(self):
        inicio = datetime(2026, 9, 24, 8, 0)
        assert deve_encerrar(inicio, None, None, ref=datetime(2026, 9, 24, 19, 0)) is None
        assert deve_encerrar(inicio, None, None, ref=datetime(2026, 9, 24, 21, 0)) == datetime(2026, 9, 24, 20, 0)


def test_dias_12x36_dia_sim_dia_nao():
    dias = list(dias_12x36(date(2026, 9, 1), date(2026, 9, 7)))
    assert dias == [date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 5), date(2026, 9, 7)]


# ── API ──────────────────────────────────────────────────────────────────
def _criar_usuario(nome, perfil="funcionario", cargo="", time_="Projetos"):
    import main
    usuario = f"teste_sd_{uuid.uuid4().hex[:8]}"
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO usuarios (usuario, senha_hash, nome, perfil, email, time, cargo)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (usuario, main.hash_senha(SENHA), nome, perfil, f"{usuario}@teste.local", time_, cargo))
    uid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return {"id": uid, "usuario": usuario}


def _apagar_usuario(uid):
    import main
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sd_atendimentos WHERE usuario_id = %s", (uid,))
    cur.execute("DELETE FROM sessoes WHERE usuario_id = %s", (uid,))
    cur.execute("DELETE FROM usuarios WHERE id = %s", (uid,))
    conn.commit(); cur.close(); conn.close()


@pytest.fixture()
def operador(app):
    from tests.conftest import login_client
    u = _criar_usuario("Operador SD Teste", cargo="sd_operador", time_="Service Desk")
    yield {**u, "client": login_client(app, u["usuario"], SENHA)}
    _apagar_usuario(u["id"])


@pytest.fixture()
def supervisor(app):
    from tests.conftest import login_client
    u = _criar_usuario("Supervisor SD Teste", cargo="sd_supervisor", time_="Service Desk")
    yield {**u, "client": login_client(app, u["usuario"], SENHA)}
    _apagar_usuario(u["id"])


@pytest.fixture()
def de_fora(app):
    from tests.conftest import login_client
    u = _criar_usuario("Analista Projetos Teste", cargo="analista", time_="Projetos")
    yield {**u, "client": login_client(app, u["usuario"], SENHA)}
    _apagar_usuario(u["id"])


def _atend(**extra):
    base = {"solicitante_nome": "João Silva", "solicitante_login": "joao.silva", "canal": "Chatbot",
            "fila_entrada": "NOW_BACKOFFICE_SERVICEDESK", "categoria": "Acesso e Senhas",
            "problema": "Reset de senha", "tratativa": "Reset via console AD e teste com o colaborador"}
    base.update(extra)
    return base


class TestAcesso:
    def test_sem_login_401(self, app):
        from fastapi.testclient import TestClient
        assert TestClient(app).get("/api/sd/me").status_code == 401

    def test_outro_time_403(self, de_fora):
        assert de_fora["client"].get("/api/sd/me").status_code == 403
        assert de_fora["client"].post("/api/sd/atendimentos", json=_atend()).status_code == 403

    def test_pagina_redireciona_quem_e_de_fora(self, de_fora):
        resp = de_fora["client"].get("/service-desk", follow_redirects=False)
        assert resp.status_code in (302, 307)

    def test_login_do_operador_cai_no_service_desk(self, operador):
        resp = operador["client"].get("/dashboard", follow_redirects=False)
        assert resp.headers["location"] == "/service-desk"

    def test_operador_nao_ve_atendimento_de_outro(self, operador, supervisor):
        aid = supervisor["client"].post("/api/sd/atendimentos", json=_atend()).json()["id"]
        ids = [a["id"] for a in operador["client"].get("/api/sd/atendimentos?escopo=tudo").json()]
        assert aid not in ids
        assert operador["client"].put(f"/api/sd/atendimentos/{aid}", json=_atend()).status_code == 403

    def test_operador_nao_gerencia_escala(self, operador):
        resp = operador["client"].post("/api/sd/escalas", json={
            "data": date.today().isoformat(), "usuario_id": operador["id"], "hora_inicio": "09:00", "hora_fim": "18:00"})
        assert resp.status_code == 403


class TestAtendimentos:
    def test_registrar_conta_no_kpi_e_cria_fila_nova(self, operador):
        c = operador["client"]
        fila_nova = f"FILA_TESTE_{uuid.uuid4().hex[:6].upper()}"
        resp = c.post("/api/sd/atendimentos", json=_atend(fila_entrada=fila_nova))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "concluido"
        assert fila_nova in c.get("/api/sd/opcoes").json()["filas"]
        me = c.get("/api/sd/me").json()
        assert me["kpis"]["total"] == 1
        assert me["kpis"]["fcr_pct"] == 100
        assert me["kpis"]["fila_top"] == fila_nova

    def test_redirecionar_marca_status_e_derruba_fcr(self, operador):
        c = operador["client"]
        c.post("/api/sd/atendimentos", json=_atend())
        r = c.post("/api/sd/atendimentos", json=_atend(fila_destino="NOW_ATENDIMENTO_SAP")).json()
        assert r["status"] == "redirecionado"
        assert c.get("/api/sd/me").json()["kpis"]["fcr_pct"] == 50

    def test_campos_obrigatorios(self, operador):
        resp = operador["client"].post("/api/sd/atendimentos", json=_atend(problema="  "))
        assert resp.status_code == 400

    def test_modelo_rapido_sai_do_historico(self, operador):
        c = operador["client"]
        for _ in range(2):
            c.post("/api/sd/atendimentos", json=_atend(problema="Chave BitLocker", tratativa="Recuperada no portal"))
        modelos = c.get("/api/sd/opcoes").json()["modelos"]
        assert any(m["problema"] == "Chave BitLocker" and m["usos"] == 2 for m in modelos)

    def test_autocompletar_solicitante_devolve_login(self, operador):
        c = operador["client"]
        c.post("/api/sd/atendimentos", json=_atend(solicitante_nome="Maria Lopes Teste", solicitante_login="maria.lopes"))
        sug = c.get("/api/sd/solicitantes?q=Maria Lopes").json()
        assert {"nome": "Maria Lopes Teste", "login": "maria.lopes"} in sug

    def test_editar_e_excluir_o_proprio(self, operador):
        c = operador["client"]
        aid = c.post("/api/sd/atendimentos", json=_atend()).json()["id"]
        r = c.put(f"/api/sd/atendimentos/{aid}", json=_atend(status="pendente")).json()
        assert r["status"] == "pendente"
        assert c.delete(f"/api/sd/atendimentos/{aid}").status_code == 200

    def test_exportar_excel(self, operador):
        operador["client"].post("/api/sd/atendimentos", json=_atend())
        resp = operador["client"].get("/api/sd/atendimentos/exportar?escopo=hoje")
        assert resp.status_code == 200
        assert resp.content[:2] == b"PK"  # xlsx é zip


class TestStatusOperador:
    def test_trocar_status(self, operador):
        c = operador["client"]
        assert c.post("/api/sd/status", json={"status": "online"}).json()["status"] == "online"
        st = c.post("/api/sd/status", json={"status": "pausa", "motivo": "Almoço/Lanche"}).json()
        assert (st["status"], st["motivo"]) == ("pausa", "Almoço/Lanche")
        assert c.get("/api/sd/me").json()["status"]["status"] == "pausa"
        assert c.post("/api/sd/status", json={"status": "voando"}).status_code == 400

    def test_pausa_esquecida_e_fechada_ao_abrir_o_painel(self, operador):
        import main
        c = operador["client"]
        c.post("/api/sd/status", json={"status": "pausa", "motivo": "Banheiro"})
        conn = main.get_db(); cur = conn.cursor()
        cur.execute("UPDATE sd_status_eventos SET inicio = NOW() - INTERVAL '96 hours' WHERE usuario_id = %s AND fim IS NULL",
                    (operador["id"],))
        conn.commit(); cur.close(); conn.close()
        assert c.get("/api/sd/me").json()["status"]["status"] == "offline"


class TestSupervisor:
    def test_equipe_e_config_do_operador(self, supervisor, operador):
        s = supervisor["client"]
        resp = s.put(f"/api/sd/operadores/{operador['id']}", json={
            "matricula": "SD-4821", "nivel": "N2", "site": "Service Desk SP",
            "jornada_inicio": "19:00", "jornada_fim": "07:00", "regime": "12x36", "meta_turno": 25})
        assert resp.status_code == 200, resp.text
        eq = {o["id"]: o for o in s.get("/api/sd/equipe").json()}
        assert eq[operador["id"]]["matricula"] == "SD-4821"
        assert eq[operador["id"]]["kpis"]["meta"] == 25
        me = operador["client"].get("/api/sd/me").json()
        assert me["operador"]["jornada_inicio"] == "19:00"
        assert me["supervisor"] is False

    def test_supervisor_ve_atendimento_da_equipe(self, supervisor, operador):
        aid = operador["client"].post("/api/sd/atendimentos", json=_atend()).json()["id"]
        ids = [a["id"] for a in supervisor["client"].get("/api/sd/atendimentos?escopo=hoje").json()]
        assert aid in ids
        ids_meus = [a["id"] for a in supervisor["client"].get("/api/sd/atendimentos?escopo=hoje&operador=eu").json()]
        assert aid not in ids_meus

    def test_gerar_12x36(self, supervisor, operador):
        s = supervisor["client"]
        d0 = date.today() + timedelta(days=30)
        resp = s.post("/api/sd/escalas/gerar-12x36", json={
            "usuario_id": operador["id"], "data_inicio": d0.isoformat(),
            "data_fim": (d0 + timedelta(days=6)).isoformat()})
        assert resp.json()["criados"] == 4
        itens = s.get(f"/api/sd/escalas?inicio={d0.isoformat()}&dias=7").json()["itens"]
        meus = [i for i in itens if i["usuario_id"] == operador["id"]]
        assert len(meus) == 4 and all(i["noturno"] for i in meus)
        for i in meus:
            s.delete(f"/api/sd/escalas/{i['id']}")

    def test_novo_status_de_chamado(self, supervisor):
        r = supervisor["client"].post("/api/sd/status-tipos", json={"rotulo": "Cancelado Teste", "finaliza": True})
        assert r.json()["chave"] == "cancelado_teste"
        chaves = [x["chave"] for x in supervisor["client"].get("/api/sd/opcoes").json()["status"]]
        assert "cancelado_teste" in chaves


class TestAreaServiceDesk:
    """Integração com o cadastro de áreas (tabela `areas`)."""

    def test_area_criada_so_com_cargos_do_sd(self, admin_client):
        areas = {a["nome"]: a for a in admin_client.get("/api/areas").json()["areas"]}
        sd = areas["Service Desk"]
        assert sd["cargos"] == ["sd_operador", "sd_supervisor"]
        assert sd["usa_projetos"] is False
        assert "sd_operador" not in areas["Projetos"]["cargos"]

    def test_admin_cadastra_operador_na_area(self, admin_client):
        usuario = f"teste_sd_{uuid.uuid4().hex[:8]}"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": SENHA, "nome": "Operador Via Admin", "perfil": "funcionario",
            "cargo": "sd_operador", "time": "Service Desk", "email": f"{usuario}@teste.local"})
        assert resp.status_code == 200, resp.text
        _apagar_usuario(resp.json()["id"])

    def test_cargo_sd_recusado_em_outra_area(self, admin_client):
        usuario = f"teste_sd_{uuid.uuid4().hex[:8]}"
        resp = admin_client.post("/api/usuarios", json={
            "usuario": usuario, "senha": SENHA, "nome": "Fora da Área", "perfil": "funcionario",
            "cargo": "sd_operador", "time": "Projetos", "email": f"{usuario}@teste.local"})
        assert resp.status_code == 400
