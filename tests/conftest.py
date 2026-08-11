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

# O admin de seed não nasce mais com senha fixa (endurecimento de 2026-07-23):
# vem de ADMIN_INITIAL_PASSWORD ou é gerado aleatório. Fixamos a variável antes
# de qualquer import de `main` pra que o setup_banco() crie um admin conhecido.
ADMIN_SENHA = os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "senhaTeste123")


# ── Adaptação do TestClient ao endurecimento de sessão ───────────────────────
# Duas coisas mudaram no login e afetam TODO teste que usa TestClient:
#   1. o cookie de sessão passou a ser secure=True, e o cookie jar do httpx só
#      devolve cookie Secure em https -- por isso a base_url deixa de ser http;
#   2. o middleware de CSRF exige o header X-CSRF-Token batendo com o cookie
#      csrf_token em POST/PUT/PATCH/DELETE.
# Em vez de repetir isso nos 6 arquivos de teste que instanciam TestClient
# direto, o ajuste fica aqui: conftest é carregado antes dos módulos de teste,
# então o `from fastapi.testclient import TestClient` deles já pega esta versão.
import fastapi.testclient as _testclient

_TestClientOriginal = _testclient.TestClient


class _ClienteFaiston(_TestClientOriginal):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("base_url", "https://testserver")
        super().__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        resp = super().request(*args, **kwargs)
        # Depois do login o cookie de CSRF existe; ecoa no header daqui pra frente.
        token = self.cookies.get("csrf_token")
        if token and self.headers.get("X-CSRF-Token") != token:
            self.headers["X-CSRF-Token"] = token
        return resp


_testclient.TestClient = _ClienteFaiston


def pytest_collection_modifyitems(config, items):
    if not TEST_DATABASE_URL:
        skip = pytest.mark.skip(reason="TEST_DATABASE_URL não definida — veja tests/conftest.py")
        for item in items:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def app():
    import main  # importa (e roda setup_banco()) só depois do DATABASE_URL de teste estar setado
    return main.app


def login_client(app, usuario: str, senha: str):
    """TestClient logado e já com o header de CSRF armado.

    O CSRFMiddleware (main.py) exige X-CSRF-Token batendo com o cookie
    csrf_token em todo POST/PUT/PATCH/DELETE de /api/*. No navegador quem
    faz isso é o patch global de fetch no index.html; no TestClient ninguém
    fazia, então toda escrita da suíte voltava 403 — o mesmo papel precisa
    ser cumprido aqui.

    base_url em https porque os cookies de sessão e de CSRF são emitidos com
    secure=True: em http o TestClient aceita o Set-Cookie mas não reenvia,
    e o middleware via header sem cookie (403 em toda escrita).
    """
    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="https://testserver")
    resp = client.post("/api/login", json={"usuario": usuario, "senha": senha})
    assert resp.status_code == 200, f"login falhou: {resp.text}"
    token = client.cookies.get("csrf_token")
    assert token, "login não devolveu cookie csrf_token"
    client.headers["X-CSRF-Token"] = token
    return client


@pytest.fixture(scope="session")
def admin_client(app):
    """Cliente HTTP autenticado como admin (senha vinda de ADMIN_INITIAL_PASSWORD)."""
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.post("/api/login", json={"usuario": "admin", "senha": ADMIN_SENHA})
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


@pytest.fixture()
def n2_user(admin_client):
    """Cria um usuário N2 temporário pra isolar os testes; limpa no teardown.

    N2 deixou de ser perfil próprio e virou cargo dentro de 'funcionario'
    (migração de 2026-07-28) -- ver _eh_n2() em main.py.
    """
    usuario = f"teste_n2_{uuid.uuid4().hex[:8]}"
    senha = "senhaTeste123"
    resp = admin_client.post("/api/usuarios", json={
        "usuario": usuario, "senha": senha, "nome": "N2 Fixture Teste",
        "perfil": "funcionario", "cargo": "n2",
    })
    assert resp.status_code == 200, resp.text
    uid = resp.json()["id"]
    yield {"id": uid, "usuario": usuario, "senha": senha}
    admin_client.delete(f"/api/usuarios/{uid}")
