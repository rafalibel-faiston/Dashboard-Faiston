"""
Testes da sinalização de sobrecarga (/api/carga-equipe): como os pontos são
somados a partir do peso das tarefas na fila, quando alguém vira "atolado" e
quem enxerga a carga de quem.
"""
import uuid
from datetime import date, timedelta

import pytest


def _definir_senha(uid: int, senha: str):
    """O cadastro de usuário não aceita mais senha vinda do admin (a pessoa
    define pelo link de boas-vindas), então o teste grava o hash direto pra
    conseguir logar como ela."""
    import main
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("UPDATE usuarios SET senha_hash=%s, primeiro_acesso=FALSE WHERE id=%s",
                (main.hash_senha(senha), uid))
    conn.commit(); cur.close(); conn.close()


def _tipo_por_peso(client, peso: int) -> int:
    """ID de um tipo de atividade com o peso pedido (a régua de peso vem do
    seed do catálogo, então sempre existe pelo menos um de cada)."""
    tipos = client.get("/api/tipos-atividade").json()
    candidatos = [t for t in tipos if t["peso"] == peso]
    assert candidatos, f"nenhum tipo de atividade com peso {peso} no catálogo"
    return candidatos[0]["id"]


@pytest.fixture()
def pessoa(admin_client):
    """Funcionário temporário + limpeza das tarefas criadas pra ele (a FK de
    tarefas.usuario_id não tem ON DELETE, então tarefa pendente trava o
    DELETE do usuário)."""
    usuario = f"teste_carga_{uuid.uuid4().hex[:8]}"
    nome = f"Carga Teste {uuid.uuid4().hex[:6]}"
    senha = "senhaTeste123"
    resp = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "senha": senha, "nome": nome,
        "email": f"{usuario}@exemplo.teste",
        "perfil": "funcionario", "cargo": "backoffice",
    })
    assert resp.status_code == 200, resp.text
    uid = resp.json()["id"]
    _definir_senha(uid, senha)
    criadas = []

    def criar(peso: int, dias_prazo: int = 30, status: str = "aberto"):
        r = admin_client.post("/api/tarefas", json={
            "descricao": "Tarefa de teste de carga",
            "cliente": "Demandas Gerais",
            "funcionario_id": uid,
            "tipo_atividade_id": _tipo_por_peso(admin_client, peso),
            "data_prazo": (date.today() + timedelta(days=dias_prazo)).isoformat(),
            "status": status,
        })
        assert r.status_code == 200, r.text
        criadas.append(r.json()["id"])
        return r.json()["id"]

    yield {"id": uid, "usuario": usuario, "senha": senha, "nome": nome, "criar": criar}

    for tid in criadas:
        admin_client.delete(f"/api/tarefas/{tid}")
    admin_client.delete(f"/api/usuarios/{uid}")


def _minha_linha(client, usuario_id: int) -> dict:
    dados = client.get("/api/carga-equipe").json()
    linha = next((p for p in dados["equipe"] if p["usuario_id"] == usuario_id), None)
    assert linha is not None, "pessoa não apareceu na carga da equipe"
    return linha


class TestContagem:
    def test_sem_tarefa_aparece_livre_e_tranquilo(self, admin_client, pessoa):
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["tarefas"] == 0
        assert linha["pontos"] == 0
        assert linha["nivel"] == "tranquilo"

    def test_pontos_somam_o_peso_das_tarefas_na_fila(self, admin_client, pessoa):
        pessoa["criar"](peso=4)
        pessoa["criar"](peso=4)
        pessoa["criar"](peso=1)
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["tarefas"] == 3
        assert linha["peso_total"] == 9
        assert linha["pontos"] == 9

    def test_concluida_nao_conta(self, admin_client, pessoa):
        pessoa["criar"](peso=4)
        pessoa["criar"](peso=4, status="concluido")
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["tarefas"] == 1
        assert linha["pontos"] == 4

    def test_atrasada_pesa_uma_vez_e_meia(self, admin_client, pessoa):
        pessoa["criar"](peso=4, dias_prazo=-3)
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["atrasadas"] == 1
        assert linha["pontos"] == 6.0

    def test_vencendo_hoje_pesa_um_quarto_a_mais(self, admin_client, pessoa):
        pessoa["criar"](peso=4, dias_prazo=0)
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["vence_ja"] == 1
        assert linha["atrasadas"] == 0
        assert linha["pontos"] == 5.0


class TestClassificacao:
    def test_fila_pesada_vira_atolado(self, admin_client, pessoa):
        for _ in range(8):          # 8 x peso 4 = 32 pontos, acima do limite padrão (28)
            pessoa["criar"](peso=4)
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["pontos"] == 32
        assert linha["nivel"] == "atolado"

    def test_atrasadas_demais_atolam_mesmo_com_poucos_pontos(self, admin_client, pessoa):
        for _ in range(3):          # 3 x peso 1 atrasadas = 4,5 pontos, mas 3 vencidas
            pessoa["criar"](peso=1, dias_prazo=-2)
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["pontos"] < 10
        assert linha["atrasadas"] == 3
        assert linha["nivel"] == "atolado"

    def test_resumo_diario_leva_a_sinalizacao_pro_gestor(self, admin_client, pessoa):
        for _ in range(8):
            pessoa["criar"](peso=4)
        html = admin_client.get("/api/admin/resumo-diario").text
        assert "Sinalização de carga" in html
        assert pessoa["nome"] in html
        assert "ATOLADO" in html

    def test_resumo_conta_os_sinalizados(self, admin_client, pessoa):
        for _ in range(8):
            pessoa["criar"](peso=4)
        dados = admin_client.get("/api/carga-equipe").json()
        assert dados["resumo"]["atolados"] >= 1
        assert pessoa["nome"] in dados["resumo"]["nomes_sinalizados"]


class TestAcesso:
    def test_sem_login_nao_acessa(self, app):
        from fastapi.testclient import TestClient
        assert TestClient(app).get("/api/carga-equipe").status_code == 401

    def test_funcionario_ve_so_a_propria_carga(self, app, admin_client, pessoa):
        from tests.conftest import login_client
        pessoa["criar"](peso=2)
        client = login_client(app, pessoa["usuario"], pessoa["senha"])
        dados = client.get("/api/carga-equipe").json()
        assert [p["usuario_id"] for p in dados["equipe"]] == [pessoa["id"]]

    def test_funcionario_nao_calibra_a_regua(self, app, admin_client, pessoa):
        from tests.conftest import login_client
        client = login_client(app, pessoa["usuario"], pessoa["senha"])
        resp = client.put("/api/config/carga-limites",
                          json={"moderado": 5, "pesado": 10, "atolado": 15})
        assert resp.status_code == 403


class TestRegua:
    @pytest.fixture(autouse=True)
    def restaura_regua(self, admin_client):
        """A régua fica salva por time em `configuracoes` — o teste devolve o
        valor anterior pra não vazar pros outros."""
        antes = admin_client.get("/api/config/carga-limites").json()["limites"]
        yield
        admin_client.put("/api/config/carga-limites", json=antes)

    def test_limites_precisam_ser_crescentes(self, admin_client):
        resp = admin_client.put("/api/config/carga-limites",
                                json={"moderado": 20, "pesado": 10, "atolado": 30})
        assert resp.status_code == 400

    def test_regua_calibrada_muda_a_classificacao(self, admin_client, pessoa):
        pessoa["criar"](peso=4)     # 4 pontos: tranquilo na régua padrão
        assert _minha_linha(admin_client, pessoa["id"])["nivel"] == "tranquilo"
        resp = admin_client.put("/api/config/carga-limites",
                                json={"moderado": 2, "pesado": 3, "atolado": 4})
        assert resp.status_code == 200, resp.text
        linha = _minha_linha(admin_client, pessoa["id"])
        assert linha["nivel"] == "atolado"
        assert linha["limite_atolado"] == 4
