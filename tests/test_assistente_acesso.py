"""
Quem tem acesso ao assistente.

O piloto acabou: não existe mais lista de perfis nem env var
(`ASSISTENTE_PERFIS_PILOTO`) — quem tem sessão válida usa o assistente.
O que continua fechado é a gestão da base de procedimentos, que segue
exigindo perfil='admin'.

Estes testes não tocam o banco: `db.get_session` é trocado por uma função
de mentira, então rodam mesmo sem TEST_DATABASE_URL apontando pra lugar
nenhum de produção.
"""
import asyncio
import inspect

import pytest

from app.assistente import router as r


PERFIS = ["funcionario", "gestor", "diretor", "demo", "admin", "dev"]


@pytest.fixture
def sessao_de(monkeypatch):
    """Faz db.get_session devolver a sessão que o teste quiser (ou None)."""
    def _aplicar(sess):
        monkeypatch.setattr(r.db, "get_session", lambda token: sess)
    return _aplicar


def _sessao(perfil):
    return {"id": 1, "perfil": perfil}


class TestAcesso:
    @pytest.mark.parametrize("perfil", PERFIS)
    def test_qualquer_perfil_logado_e_elegivel(self, perfil, sessao_de):
        sessao_de(_sessao(perfil))
        resp = asyncio.run(r.elegivel("tok"))
        assert resp["elegivel"] is True
        assert resp["admin"] is (perfil == "admin")

    def test_sem_sessao_nao_e_elegivel(self, sessao_de):
        sessao_de(None)
        assert asyncio.run(r.elegivel("tok")) == {"elegivel": False, "admin": False}

    @pytest.mark.parametrize("perfil", PERFIS)
    def test_autenticar_aceita_qualquer_perfil(self, perfil, sessao_de):
        sessao_de(_sessao(perfil))
        assert asyncio.run(r._autenticar("tok"))["perfil"] == perfil

    def test_autenticar_sem_sessao_e_401(self, sessao_de):
        sessao_de(None)
        with pytest.raises(r.HTTPException) as erro:
            asyncio.run(r._autenticar("tok"))
        assert erro.value.status_code == 401

    def test_nao_existe_mais_gate_por_perfil(self):
        """Se alguém reintroduzir a lista de perfis lida do ambiente, o
        assistente volta a fechar sozinho num deploy sem a variável — foi
        exatamente isso que esta remoção resolveu."""
        assert not hasattr(r, "_no_piloto")
        assert not hasattr(r, "_PERFIS_PILOTO")
        assert "perfil" not in inspect.getsource(r._autenticar)


class TestGestaoDaBaseContinuaFechada:
    @pytest.mark.parametrize("perfil", [p for p in PERFIS if p != "admin"])
    def test_nao_admin_nao_gerencia_a_base(self, perfil, sessao_de):
        """Abrir o assistente pra todo mundo amplia quem *pergunta*, nunca
        quem *gerencia a base*: ingestão e remoção de documento continuam
        exigindo perfil='admin' de verdade."""
        sessao_de(_sessao(perfil))
        with pytest.raises(r.HTTPException) as erro:
            asyncio.run(r._exigir_admin("tok"))
        assert erro.value.status_code == 403

    def test_admin_gerencia_a_base(self, sessao_de):
        sessao_de(_sessao("admin"))
        assert asyncio.run(r._exigir_admin("tok"))["perfil"] == "admin"

    def test_sem_sessao_e_401_na_gestao(self, sessao_de):
        sessao_de(None)
        with pytest.raises(r.HTTPException) as erro:
            asyncio.run(r._exigir_admin("tok"))
        assert erro.value.status_code == 401
