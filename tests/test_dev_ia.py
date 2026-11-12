"""IA do Kanban da Área de Dev (/api/dev-ia/*).

O modelo é substituído por um falso (ia._llm): o ambiente de teste não alcança o
provedor, e o que interessa é o que o BACKEND faz com a resposta -- sanitizar,
aplicar whitelist, revalidar, cair em regras sem IA e limitar uso."""
import json
from datetime import date, timedelta

import pytest

from app.assistente.llm import ModeloIndisponivel
from app.devkanban import ia


def _mk(dev_client, titulo, **kw):
    body = {"titulo": titulo}
    body.update(kw)
    r = dev_client.post("/api/dev-tarefas", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _get(dev_client, tid):
    return next(t for t in dev_client.get("/api/dev-tarefas").json() if t["id"] == tid)


@pytest.fixture()
def llm(monkeypatch):
    """Troca o modelo. `llm.responder(texto)` ou `llm.falhar(motivo)`; `llm.chamadas` guarda as mensagens enviadas."""
    class Falso:
        def __init__(self):
            self.chamadas, self._resp, self._erro = [], "", None
        def responder(self, texto):
            self._resp, self._erro = texto, None
        def falhar(self, motivo="sem_chave"):
            self._erro = motivo
    f = Falso()

    async def _falso(mensagens, temperature=0.2):
        f.chamadas.append(mensagens)
        if f._erro:
            raise ModeloIndisponivel("falha simulada", motivo=f._erro)
        return f._resp
    monkeypatch.setattr(ia, "_llm", _falso)
    return f


class TestAcesso:
    @pytest.mark.parametrize("metodo,caminho,corpo", [
        ("get", "/api/dev-ia/status", None), ("get", "/api/dev-ia/briefing", None),
        ("post", "/api/dev-ia/capturar", {"texto": "x" * 20}), ("post", "/api/dev-ia/comando", {"texto": "oi"}),
        ("post", "/api/dev-ia/standup", {}), ("post", "/api/dev-ia/comando/aplicar", {"acoes": []}),
        ("post", "/api/dev-ia/capturar/confirmar", {"tarefas": []}),
    ])
    def test_admin_comum_e_anonimo_nao_acessam(self, admin_client, app, metodo, caminho, corpo):
        from fastapi.testclient import TestClient
        for c in (admin_client, TestClient(app)):
            r = getattr(c, metodo)(caminho, **({"json": corpo} if corpo is not None else {}))
            assert r.status_code == 403, f"{caminho}: {r.status_code}"

    def test_status(self, dev_client):
        d = dev_client.get("/api/dev-ia/status").json()
        assert set(d) == {"llm_configurado", "modelo"}


class TestJsonESanitizacao:
    def test_extrai_json_de_bloco_markdown(self):
        assert ia.extrair_json('Claro!\n```json\n{"a": 1}\n```\nfim') == {"a": 1}

    def test_extrai_json_com_texto_em_volta(self):
        assert ia.extrair_json('Aqui está: {"a": {"b": 2}} ok') == {"a": {"b": 2}}

    def test_sem_json_levanta_erro(self):
        with pytest.raises(ValueError):
            ia.extrair_json("não consegui")

    def test_rascunho_corrige_enums_data_e_limites(self):
        r = ia.sanitizar_rascunho({
            "titulo": "  Corrigir   bug\n X ", "prioridade": "critica", "prazo": "31/12", "status": "xyz",
            "tags": ["dispacher", "DISPACHER", "novo", "", "a", "b", "c", "d"], "checklist": [f"p{i}" for i in range(30)] + ["  "],
        }, ["Dispacher"])
        assert r["titulo"] == "Corrigir bug X"
        assert r["prioridade"] == "media" and r["prazo"] is None and r["status"] == "todo"
        assert r["tags"][0] == "Dispacher" and len(r["tags"]) == 5          # reaproveita grafia, dedupe, máx 5
        assert len(r["checklist"]) == 12

    def test_rascunho_sem_titulo_e_descartado(self):
        assert ia.sanitizar_rascunho({"titulo": "   "}, []) is None
        assert ia.sanitizar_rascunho("texto solto", []) is None


class TestCapturar:
    def test_devolve_rascunhos_sanitizados(self, dev_client, llm):
        _mk(dev_client, "Existente", tags=["Dispatcher"])
        llm.responder('```json\n' + json.dumps({"tarefas": [
            {"titulo": "Revisar contrato do Dispatcher", "prioridade": "alta", "prazo": "2026-12-01", "tags": ["dispatcher"],
             "checklist": ["Ler cláusulas", "Enviar ao jurídico"]},
            {"titulo": "", "prioridade": "urgente"},
        ] + [{"titulo": f"Extra {i}"} for i in range(8)]}) + '\n```')
        r = dev_client.post("/api/dev-ia/capturar", json={"texto": "Reunião: revisar o contrato do Dispatcher até 01/12"})
        assert r.status_code == 200, r.text
        rs = r.json()["rascunhos"]
        assert len(rs) <= 5 and rs[0]["titulo"] == "Revisar contrato do Dispatcher"
        assert rs[0]["tags"] == ["Dispatcher"]                      # grafia existente
        assert rs[0]["status"] == "todo" and rs[0]["checklist"] == ["Ler cláusulas", "Enviar ao jurídico"]
        assert "Dispatcher" in llm.chamadas[0][0]["content"]        # tags existentes foram ao prompt
        assert date.today().isoformat() in llm.chamadas[0][0]["content"]

    def test_texto_curto_demais(self, dev_client, llm):
        assert dev_client.post("/api/dev-ia/capturar", json={"texto": "oi"}).status_code == 400

    def test_resposta_ilegivel_vira_502(self, dev_client, llm):
        llm.responder("desculpe, não sei")
        assert dev_client.post("/api/dev-ia/capturar", json={"texto": "texto longo o suficiente"}).status_code == 502

    def test_sem_ia_vira_503_com_mensagem_clara(self, dev_client, llm):
        llm.falhar("sem_chave")
        r = dev_client.post("/api/dev-ia/capturar", json={"texto": "texto longo o suficiente"})
        assert r.status_code == 503 and "IA" in r.json()["detail"]


class TestConfirmarCaptura:
    def test_cria_com_checklist_origem_e_ignora_responsavel_invalido(self, dev_client):
        r = dev_client.post("/api/dev-ia/capturar/confirmar", json={"origem": "ia", "tarefas": [
            {"titulo": "Da IA", "prioridade": "urgente", "tags": ["Proj"], "checklist": ["a", "b"], "atribuido_a": 999999}]})
        assert r.status_code == 200, r.text
        tid = r.json()["criadas"][0]
        try:
            t = _get(dev_client, tid)
            assert t["origem"] == "ia" and t["checklist_total"] == 2 and t["atribuido_a"] is None and t["prioridade"] == "urgente"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_dedupe_por_origem_ref(self, dev_client):
        ref = f"msg-{date.today().isoformat()}-{id(self)}"
        corpo = {"origem": "email", "tarefas": [{"titulo": "Do e-mail", "origem_ref": ref}]}
        r1 = dev_client.post("/api/dev-ia/capturar/confirmar", json=corpo).json()
        r2 = dev_client.post("/api/dev-ia/capturar/confirmar", json=corpo).json()
        try:
            assert len(r1["criadas"]) == 1 and r1["duplicadas"] == 0
            assert r2["criadas"] == [] and r2["duplicadas"] == 1
        finally:
            for tid in r1["criadas"]:
                dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_origem_invalida(self, dev_client):
        assert dev_client.post("/api/dev-ia/capturar/confirmar", json={"origem": "hack", "tarefas": []}).status_code == 400


class TestBriefing:
    def test_com_ia_usa_o_texto_do_modelo_e_manda_so_dados_do_quadro(self, dev_client, llm):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        tid = _mk(dev_client, "Tarefa atrasada do briefing", prazo=ontem, prioridade="alta")
        try:
            llm.responder("Comece pela tarefa atrasada.")
            d = dev_client.get("/api/dev-ia/briefing?escopo=todas").json()
            assert d["gerado_com"] == "ia" and d["texto"] == "Comece pela tarefa atrasada."
            assert any(f["id"] == tid for f in d["foco"])
            assert "Tarefa atrasada do briefing" in llm.chamadas[0][1]["content"]
            assert "atrasada há 1d" in llm.chamadas[0][1]["content"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_sem_ia_cai_em_regras_com_aviso(self, dev_client, llm):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        tid = _mk(dev_client, "Atrasada regras", prazo=ontem)
        try:
            llm.falhar("sem_chave")
            d = dev_client.get("/api/dev-ia/briefing?escopo=todas").json()
            assert d["gerado_com"] == "regras" and "Atrasada regras" in d["texto"] and "IA não configurada" in d["aviso"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")

    def test_escopo_minhas_inclui_sem_dono_e_exclui_de_outros(self, dev_client, admin_client, llm):
        import uuid
        usuario = f"teste_dev2_{uuid.uuid4().hex[:8]}"
        outro = admin_client.post("/api/usuarios", json={"usuario": usuario, "nome": "Outro Dev", "perfil": "dev",
                                                         "email": f"{usuario}@exemplo.teste"}).json()["id"]
        a = _mk(dev_client, "Sem dono escopo")
        b = _mk(dev_client, "Do outro escopo", atribuido_a=outro)
        try:
            llm.falhar("sem_chave")
            foco_ids = {f["id"] for f in dev_client.get("/api/dev-ia/briefing?escopo=minhas").json()["foco"]}
            assert a in foco_ids and b not in foco_ids
        finally:
            dev_client.delete(f"/api/dev-tarefas/{a}"); dev_client.delete(f"/api/dev-tarefas/{b}")
            admin_client.delete(f"/api/usuarios/{outro}")


class TestStandup:
    def test_regras_listam_concluidas_andamento_e_bloqueios(self, dev_client, llm):
        feita = _mk(dev_client, "Standup feita", status="done")
        fazendo = _mk(dev_client, "Standup fazendo", status="doing")
        pausada = _mk(dev_client, "Standup pausada", status="todo", pausado=True)
        try:
            llm.falhar("sem_chave")
            d = dev_client.post("/api/dev-ia/standup", json={"escopo": "todas"}).json()
            assert d["gerado_com"] == "regras"
            t = d["texto"]
            assert "Standup feita" in t.split("Em andamento")[0]
            assert "Standup fazendo" in t.split("Em andamento")[1].split("O que vou fazer")[0]
            assert "Standup pausada" in t.split("Bloqueios")[1]
        finally:
            for i in (feita, fazendo, pausada):
                dev_client.delete(f"/api/dev-tarefas/{i}")

    def test_salvar_grava_no_diario(self, dev_client, llm):
        tid = _mk(dev_client, "Standup salvar", status="done")
        diario_id = None
        try:
            llm.responder("O que fiz:\n- Standup salvar")
            d = dev_client.post("/api/dev-ia/standup", json={"escopo": "todas", "salvar": True, "periodo": "semana"}).json()
            diario_id = d["diario_id"]
            itens = dev_client.get("/api/dev-diario").json()
            item = next(i for i in itens if i["id"] == diario_id)
            assert item["titulo"].startswith("Standup da semana") and "Standup salvar" in item["descricao"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{tid}")
            if diario_id:
                dev_client.delete(f"/api/dev-diario/{diario_id}")

    def test_periodo_invalido(self, dev_client):
        assert dev_client.post("/api/dev-ia/standup", json={"periodo": "ano"}).status_code == 400

    def test_inicio_do_periodo_ontem_na_segunda_volta_a_sexta(self):
        assert ia.inicio_periodo("ontem", date(2026, 11, 9)) == date(2026, 11, 6)   # segunda
        assert ia.inicio_periodo("ontem", date(2026, 11, 12)) == date(2026, 11, 11)
        assert ia.inicio_periodo("semana", date(2026, 11, 12)) == date(2026, 11, 5)


class TestComando:
    def test_proposta_so_tem_acoes_da_whitelist_com_ids_reais(self, dev_client, llm):
        a, b = _mk(dev_client, "Cmd A", tags=["Dispatcher"]), _mk(dev_client, "Cmd B", tags=["Dispatcher"])
        try:
            llm.responder(json.dumps({"resposta": "Feito, veja:", "acoes": [
                {"tipo": "mover", "ids": [a, b, 999999], "status": "backlog"},
                {"tipo": "mover", "ids": [a], "status": "inexistente"},          # status inválido -> some
                {"tipo": "excluir", "ids": [a]},                                   # fora da whitelist -> some
                {"tipo": "prioridade", "ids": [a], "prioridade": "urgente"},
                {"tipo": "atribuir", "ids": [a], "usuario": "Fulano Que Não Existe"},   # pessoa inexistente -> some
                {"tipo": "filtrar", "tag": "dispatcher", "atrasadas": True},
            ]}))
            r = dev_client.post("/api/dev-ia/comando", json={"texto": "move tudo do dispatcher pro backlog"})
            assert r.status_code == 200, r.text
            d = r.json()
            tipos = [x["tipo"] for x in d["acoes"]]
            assert tipos == ["mover", "prioridade", "filtrar"]
            assert d["acoes"][0]["ids"] == [a, b] and "Cmd A" in d["acoes"][0]["resumo"]
            assert d["acoes"][2]["filtro"] == {"tag": "Dispatcher", "atrasadas": True} and d["acoes"][2]["mutavel"] is False
            assert d["resposta"] == "Feito, veja:"
            assert _get(dev_client, a)["status"] == "todo"                          # propor NÃO altera nada
        finally:
            dev_client.delete(f"/api/dev-tarefas/{a}"); dev_client.delete(f"/api/dev-tarefas/{b}")

    def test_aplicar_executa_e_revalida_no_servidor(self, dev_client, llm):
        a, b = _mk(dev_client, "Apl A", tags=["X"]), _mk(dev_client, "Apl B", status="doing")
        try:
            acoes = [
                {"tipo": "mover", "ids": [a, b], "status": "done"},
                {"tipo": "prioridade", "ids": [a], "prioridade": "urgente"},
                {"tipo": "adicionar_tag", "ids": [a], "tag": "x"},          # já tem 'X' -> não duplica
                {"tipo": "adicionar_tag", "ids": [b], "tag": "Novo"},
                {"tipo": "remover_tag", "ids": [a], "tag": "X"},
                {"tipo": "atribuir", "ids": [a], "usuario": "Dev de Teste"},
                {"tipo": "prazo", "ids": [b], "prazo": "2026-12-24"},
                {"tipo": "excluir", "ids": [a]},                            # forjado: ignorado
                {"tipo": "mover", "ids": [999999], "status": "todo"},       # id inexistente: ignorado
            ]
            r = dev_client.post("/api/dev-ia/comando/aplicar", json={"acoes": acoes})
            assert r.status_code == 200, r.text
            ta, tb = _get(dev_client, a), _get(dev_client, b)
            assert ta["status"] == "done" and tb["status"] == "done" and ta["concluido_em"]
            assert ta["prioridade"] == "urgente" and ta["tags"] == []
            assert tb["tags"] == ["Novo"] and tb["prazo"] == "2026-12-24"
            assert ta["atribuido_a_nome"] == "Dev de Teste"
        finally:
            dev_client.delete(f"/api/dev-tarefas/{a}"); dev_client.delete(f"/api/dev-tarefas/{b}")

    def test_renomear_tag_unifica_typo_e_remove_duplicata(self, dev_client):
        a = _mk(dev_client, "Typo A", tags=["Dispacher", "x"])
        b = _mk(dev_client, "Typo B", tags=["Dispatcher", "Dispacher"])      # já tem a certa: não pode duplicar
        c = _mk(dev_client, "Sem relação", tags=["Outra"])
        try:
            r = dev_client.post("/api/dev-ia/comando/aplicar", json={"acoes": [{"tipo": "renomear_tag", "de": "dispacher", "para": "Dispatcher"}]})
            assert r.status_code == 200, r.text
            assert _get(dev_client, a)["tags"] == ["Dispatcher", "x"]
            assert _get(dev_client, b)["tags"] == ["Dispatcher"]
            assert _get(dev_client, c)["tags"] == ["Outra"]
        finally:
            for i in (a, b, c):
                dev_client.delete(f"/api/dev-tarefas/{i}")

    def test_renomear_tag_inexistente_ou_igual_e_descartado(self, dev_client):
        a = _mk(dev_client, "Rename guard", tags=["Real"])
        try:
            for acao in ({"tipo": "renomear_tag", "de": "Fantasma", "para": "X"}, {"tipo": "renomear_tag", "de": "Real", "para": "Real"},
                         {"tipo": "renomear_tag", "de": "Real", "para": ""}):
                assert dev_client.post("/api/dev-ia/comando/aplicar", json={"acoes": [acao]}).status_code == 400
        finally:
            dev_client.delete(f"/api/dev-tarefas/{a}")

    def test_aplicar_cria_tarefa_marcada_como_ia(self, dev_client):
        r = dev_client.post("/api/dev-ia/comando/aplicar", json={"acoes": [{"tipo": "criar", "titulo": "Nasceu do comando", "tags": ["Z"]}]})
        assert r.status_code == 200 and r.json()["criadas"] == 1
        t = next(t for t in dev_client.get("/api/dev-tarefas").json() if t["titulo"] == "Nasceu do comando")
        try:
            assert t["origem"] == "ia" and t["tags"] == ["Z"]
        finally:
            dev_client.delete(f"/api/dev-tarefas/{t['id']}")

    def test_aplicar_sem_nada_valido_e_rejeitado(self, dev_client):
        for acoes in ([], [{"tipo": "excluir", "ids": [1]}], [{"tipo": "mover", "ids": [999999], "status": "todo"}],
                      [{"tipo": "filtrar", "tag": "x"}]):
            assert dev_client.post("/api/dev-ia/comando/aplicar", json={"acoes": acoes}).status_code == 400

    def test_so_dev_aplica(self, admin_client):
        assert admin_client.post("/api/dev-ia/comando/aplicar", json={"acoes": []}).status_code == 403


class TestLimiteDeUso:
    def test_excesso_de_chamadas_vira_429(self, dev_client, llm, monkeypatch):
        from app.devkanban import router as r
        monkeypatch.setattr(r, "LIMITE_CHAMADAS_MIN", 2)
        r._chamadas.clear()
        llm.falhar("sem_chave")
        codigos = [dev_client.get("/api/dev-ia/briefing").status_code for _ in range(3)]
        assert codigos == [200, 200, 429]
