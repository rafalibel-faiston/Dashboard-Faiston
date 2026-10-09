"""
Rotas de importação de planilha (openpyxl) e a estrutura dos módulos de app/.

Na refatoração de 2026-10-08 as rotas que liam planilha foram para módulos
próprios levando `global _OPENPYXL_OK, openpyxl`, mas a variável não existia
no topo do módulo novo: toda importação dava NameError (500). Nenhum teste
chamava essas rotas, então passou. Os testes abaixo cobrem as duas pontas:
as rotas de upload respondendo, e nenhum `global` apontando pro vazio.
"""
import ast
import glob
import io

import pytest


def _planilha_sem_aba_reconhecida() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "Qualquer"
    wb.active.append(["coluna", "que", "nada", "reconhece"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("rota", [
    "/api/forecast/importar",
    "/api/status-campo/importar-planilha",
    "/api/escala-n2/importar-planilha",
])
def test_upload_de_planilha_responde_400_e_nao_500(admin_client, rota):
    # Planilha válida mas sem aba reconhecida: a rota tem que abrir o arquivo
    # (openpyxl) e recusar com 400 -- antes de gravar qualquer coisa.
    resp = admin_client.post(rota, files={"file": ("teste.xlsx", _planilha_sem_aba_reconhecida(),
                                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 400, resp.text
    assert "Nenhuma aba reconhecida" in resp.json()["detail"]


def _nomes_do_topo(arvore):
    nomes = set()
    for no in arvore.body:
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nomes.add(no.name)
        elif isinstance(no, (ast.Import, ast.ImportFrom)):
            nomes |= {(a.asname or a.name).split(".")[0] for a in no.names}
        else:
            for n in ast.walk(no):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    nomes.add(n.id)
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    nomes |= {(a.asname or a.name).split(".")[0] for a in n.names}
    return nomes


def test_todo_global_existe_no_topo_do_proprio_modulo():
    problemas = []
    for arq in glob.glob("app/**/*.py", recursive=True) + ["main.py"]:
        arvore = ast.parse(open(arq, encoding="utf-8").read())
        topo = _nomes_do_topo(arvore)
        for no in ast.walk(arvore):
            if isinstance(no, ast.Global):
                problemas += [f"{arq}:{no.lineno} global {g}" for g in no.names if g not in topo]
    assert not problemas, "global sem variável no topo do módulo (NameError na 1ª leitura):\n" + "\n".join(problemas)
