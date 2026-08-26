"""
Quem entra no piloto do assistente (ASSISTENTE_PERFIS_PILOTO).

O gate é lido do ambiente no import do módulo, então cada cenário aqui
recarrega o router com a env var que quer testar — é o mesmo caminho que o
deploy faz ao subir com a variável setada.
"""
import importlib
import os

import pytest


def _router_com(valor):
    """Recarrega o router com ASSISTENTE_PERFIS_PILOTO=valor."""
    mp = pytest.MonkeyPatch()
    try:
        if valor is None:
            mp.delenv("ASSISTENTE_PERFIS_PILOTO", raising=False)
        else:
            mp.setenv("ASSISTENTE_PERFIS_PILOTO", valor)
        from app.assistente import router
        return importlib.reload(router)
    finally:
        mp.undo()


PERFIS = ["funcionario", "gestor", "diretor", "demo", "admin", "dev"]


class TestPiloto:
    def test_asterisco_libera_todos_os_perfis(self):
        r = _router_com("*")
        assert [p for p in PERFIS if not r._no_piloto(p)] == []

    def test_lista_explicita_barra_quem_nao_esta_nela(self):
        r = _router_com("admin,funcionario")
        assert r._no_piloto("admin") and r._no_piloto("funcionario")
        assert not r._no_piloto("gestor")
        assert not r._no_piloto("diretor")

    def test_default_sem_env_var_e_so_admin(self):
        """Se a variável sumir do deploy, o assistente fecha em vez de abrir
        pra todo mundo — falha para o lado seguro."""
        r = _router_com(None)
        assert r._no_piloto("admin")
        assert [p for p in PERFIS if p != "admin" and r._no_piloto(p)] == []

    def test_perfil_vazio_ou_nulo_nunca_entra(self):
        r = _router_com("*")
        assert not r._no_piloto(None)
        assert not r._no_piloto("")

    def test_espaco_em_volta_da_virgula_nao_quebra(self):
        r = _router_com(" admin , gestor ")
        assert r._no_piloto("gestor")

    def test_asterisco_nao_abre_a_gestao_da_base(self):
        """`*` amplia quem *pergunta*, nunca quem *gerencia a base*: ingestão e
        remoção de documento continuam exigindo perfil='admin' de verdade.
        Se alguém trocar essa checagem por _no_piloto, com `*` no ar todo
        funcionário passaria a poder apagar procedimento."""
        import ast
        import inspect

        r = _router_com("*")
        fonte = inspect.getsource(r._exigir_admin)
        arvore = ast.parse(fonte.strip())
        comparacoes = [
            n for n in ast.walk(arvore)
            if isinstance(n, ast.Compare)
            and any(isinstance(c, ast.Constant) and c.value == "admin" for c in n.comparators)
        ]
        assert comparacoes, "_exigir_admin deixou de comparar o perfil com 'admin'"
        assert "_no_piloto" not in fonte, "_exigir_admin não pode depender do gate do piloto"


def teardown_module(module):
    """Devolve o router ao estado do ambiente real, pra não vazar o gate
    recarregado destes testes para os outros arquivos da suíte."""
    from app.assistente import router
    importlib.reload(router)
