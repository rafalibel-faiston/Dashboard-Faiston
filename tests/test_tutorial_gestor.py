"""
Tour do gestor: abre no primeiro acesso de quem é cadastrado (flag no banco,
não no navegador) e para de abrir depois de visto.
"""
import uuid

from tests.test_carga_equipe import _definir_senha


def test_gestor_novo_ve_tour_uma_vez(app, admin_client):
    from tests.conftest import login_client
    usuario = f"teste_tour_{uuid.uuid4().hex[:8]}"
    r = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "nome": "Tour Teste", "email": f"{usuario}@exemplo.teste",
        "perfil": "gestor", "time": "Projetos",
    })
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    try:
        _definir_senha(uid, "senhaTeste123")
        gestor = login_client(app, usuario, "senhaTeste123")
        assert gestor.get("/api/tutorial-gestor/status").json() == {"visto": False}
        assert gestor.post("/api/tutorial-gestor/visto").status_code == 200
        assert gestor.get("/api/tutorial-gestor/status").json() == {"visto": True}
    finally:
        admin_client.delete(f"/api/usuarios/{uid}")


def test_tour_exige_login(app):
    from fastapi.testclient import TestClient
    anon = TestClient(app)
    assert anon.get("/api/tutorial-gestor/status").status_code == 401
