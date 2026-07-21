"""
Fixtures compartilhadas da suite de testes.

O app usa psycopg2 puro (sem ORM/dependency injection), então não dá pra
trocar o banco por um mock sem reescrever main.py. A estratégia aqui é
rodar contra um Postgres real de teste — nunca o de produção.

Por segurança, os testes só rodam se TEST_DATABASE_URL estiver definida
explicitamente (nunca cai para DATABASE_URL, pra nunca sujar produção
por engano). Defina no ambiente ou num .env local (gitignored) antes de
rodar `pytest`.
"""
import os
import uuid

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if not TEST_DATABASE_URL:
    # Tenta carregar de um .env local (mesmo padrão que main.py usa via dotenv)
    try:
        from dotenv import dotenv_values
        env_file = dotenv_values(os.path.join(os.path.dirname(__file__), "..", ".env"))
        TEST_DATABASE_URL = env_file.get("TEST_DATABASE_URL")
    except Exception:
        pass

if not TEST_DATABASE_URL:
    collect_ignore_glob = ["*"]  # não coleta nenhum teste sem banco de teste configurado
else:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def pytest_collection_modifyitems(config, items):
    if not TEST_DATABASE_URL:
        skip = pytest.mark.skip(reason="TEST_DATABASE_URL não definida — veja tests/conftest.py")
        for item in items:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def app():
    import main  # importa (e roda setup_banco()) só depois do DATABASE_URL de teste estar setado
    return main.app


@pytest.fixture(scope="session")
def admin_client(app):
    """Cliente HTTP autenticado como admin (usuário seed padrão admin/admin123)."""
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.post("/api/login", json={"usuario": "admin", "senha": "admin123"})
    assert resp.status_code == 200, f"login falhou: {resp.text}"
    return client


@pytest.fixture()
def cliente_teste(admin_client):
    """Cria um cliente (empresa) temporário pra isolar os testes; limpa no teardown."""
    nome = f"TESTE-CLAUDE-{uuid.uuid4().hex[:8]}"
    resp = admin_client.post("/api/clientes", json={"nome": nome})
    assert resp.status_code == 200, resp.text
    cid = resp.json()["id"]
    yield cid
    admin_client.delete(f"/api/clientes/{cid}")
