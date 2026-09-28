"""
Página de novidades do sistema: histórico carregado na subida, aviso só do
que a pessoa ainda não viu, e publicação restrita ao admin.
"""
import uuid

import pytest

from tests.test_carga_equipe import _definir_senha


@pytest.fixture()
def func_client(app, admin_client):
    from tests.conftest import login_client
    usuario = f"teste_nov_{uuid.uuid4().hex[:8]}"
    senha = "senhaTeste123"
    resp = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "senha": senha, "nome": "Novidades Teste",
        "email": f"{usuario}@exemplo.teste", "perfil": "funcionario", "cargo": "backoffice",
    })
    assert resp.status_code == 200, resp.text
    uid = resp.json()["id"]
    _definir_senha(uid, senha)
    yield login_client(app, usuario, senha)
    admin_client.delete(f"/api/usuarios/{uid}")


def test_historico_vem_carregado(func_client):
    dados = func_client.get("/api/novidades").json()
    titulos = [n["titulo"] for n in dados["novidades"]]
    assert "Timer em várias tarefas ao mesmo tempo" in titulos
    assert dados["pode_publicar"] is False
    # mais recente primeiro
    datas = [n["publicado_em"] for n in dados["novidades"]]
    assert datas == sorted(datas, reverse=True)


def test_aviso_some_depois_de_marcar_visto(func_client):
    assert func_client.get("/api/novidades/nao-vistas").json()["total"] >= 1
    assert func_client.post("/api/novidades/vistas").status_code == 200
    assert func_client.get("/api/novidades/nao-vistas").json()["total"] == 0


def test_so_admin_publica_e_novidade_aparece_pra_quem_ja_tinha_visto(func_client, admin_client):
    func_client.post("/api/novidades/vistas")
    body = {"titulo": f"Teste {uuid.uuid4().hex[:6]}", "resumo": "r", "itens": ["a", " ", "b"], "categoria": "melhoria"}
    assert func_client.post("/api/novidades", json=body).status_code == 403
    r = admin_client.post("/api/novidades", json=body)
    assert r.status_code == 200, r.text
    nid = r.json()["id"]
    try:
        d = func_client.get("/api/novidades/nao-vistas").json()
        assert d["total"] == 1 and d["itens"][0]["titulo"] == body["titulo"]
        assert d["itens"][0]["itens"] == ["a", "b"]
    finally:
        assert admin_client.delete(f"/api/novidades/{nid}").status_code == 200
    assert func_client.get("/api/novidades/nao-vistas").json()["total"] == 0


def test_categoria_invalida_e_titulo_vazio(admin_client):
    assert admin_client.post("/api/novidades", json={"titulo": "x", "categoria": "outra"}).status_code == 400
    assert admin_client.post("/api/novidades", json={"titulo": "  "}).status_code == 400


def test_pagina_exige_login(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="https://testserver")
    r = c.get("/novidades", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
