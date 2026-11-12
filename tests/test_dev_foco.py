"""Foco do dia (app/devkanban/foco.py): ranking por regras, sem IA."""
from datetime import date, datetime, timedelta

from app.devkanban.foco import pontuar, ranquear

HOJE = date(2026, 11, 12)


def _t(**kw):
    base = {"id": 1, "status": "todo", "prioridade": "media", "prazo": None, "pausado": False,
            "checklist_total": 0, "checklist_concluidos": 0}
    base.update(kw)
    return base


class TestPontuar:
    def test_concluida_e_backlog_nao_entram_no_foco(self):
        assert pontuar(_t(status="done", prazo="2026-11-01"), HOJE) == (0, [])
        assert pontuar(_t(status="backlog", prioridade="urgente"), HOJE) == (0, [])

    def test_atrasada_vale_mais_que_vence_hoje(self):
        atrasada, m1 = pontuar(_t(prazo="2026-11-10"), HOJE)
        hoje, m2 = pontuar(_t(prazo="2026-11-12"), HOJE)
        assert atrasada > hoje
        assert "atrasada há 2d" in m1 and "vence hoje" in m2

    def test_urgente_sem_prazo_ganha_de_media_sem_prazo(self):
        assert pontuar(_t(prioridade="urgente"), HOJE)[0] > pontuar(_t(), HOJE)[0]

    def test_em_progresso_parada_ha_dias_e_sinalizada(self):
        antigo = datetime(2026, 11, 5, 10, 0)
        s, m = pontuar(_t(status="doing", atualizado_dt=antigo, iniciado_dt=antigo), HOJE)
        assert "parada há 7d" in m

    def test_em_progresso_recente_mostra_ha_quantos_dias(self):
        ini = datetime(2026, 11, 10, 9, 0)
        s, m = pontuar(_t(status="doing", atualizado_dt=datetime(2026, 11, 12, 8, 0), iniciado_dt=ini), HOJE)
        assert "em progresso há 2d" in m

    def test_quase_pronta(self):
        s, m = pontuar(_t(checklist_total=5, checklist_concluidos=4), HOJE)
        assert "quase pronta (4/5)" in m

    def test_pausada_e_lembrada(self):
        s, m = pontuar(_t(pausado=True), HOJE)
        assert "pausada" in m


class TestRanquear:
    def test_ordem_e_limite(self):
        tarefas = [_t(id=i, prazo=(HOJE + timedelta(days=d)).isoformat()) for i, d in enumerate([5, -3, 0, 1, 2, 9, 30], 1)]
        top = ranquear(tarefas, limite=3, hoje=HOJE)
        assert [t["id"] for t in top] == [2, 3, 4]   # atrasada, hoje, amanhã

    def test_sem_pontuacao_fica_de_fora(self):
        assert ranquear([_t(status="done"), _t(id=2, status="backlog")], hoje=HOJE) == []


class TestNaListagem:
    def test_listagem_traz_score_motivos_e_novos_campos(self, dev_client):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Atrasada", "prazo": ontem, "prioridade": "alta"}).json()["id"]
        try:
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["score"] > 100
            assert "atrasada há 1d" in item["motivos"]
            assert item["origem"] == "manual"
            assert item["iniciado_em"] is None
            assert "atualizado_dt" not in item          # objetos datetime não vazam pro JSON
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_iniciado_em_nasce_ao_entrar_em_doing_e_limpa_ao_sair(self, dev_client):
        tid = dev_client.post("/api/dev-tarefas", json={"titulo": "Vai iniciar"}).json()["id"]
        try:
            dev_client.put(f"/api/dev-tarefas/{tid}", json={"titulo": "Vai iniciar", "status": "doing"})
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["iniciado_em"] is not None
            dev_client.post("/api/dev-tarefas/reordenar", json={"status": "todo", "ids": [tid]})
            item = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)
            assert item["iniciado_em"] is None
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")
