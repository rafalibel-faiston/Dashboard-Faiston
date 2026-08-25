"""Dados de demonstração (botão de QA na tela admin, fora da capacidade
D) — só a checagem estática de SQL literal fixa, mesmo padrão do resto
do módulo. Roda sempre (não precisa de TEST_DATABASE_URL)."""
import ast
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent


def test_dados_demo_so_usa_sql_literal_fixa():
    codigo = (_RAIZ / "app" / "assistente" / "dados_demo.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    chamadas = 0
    for node in ast.walk(arvore):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            chamadas += 1
            primeiro_arg = node.args[0]
            assert isinstance(primeiro_arg, ast.Constant) and isinstance(primeiro_arg.value, str), (
                f"cur.execute() na linha {node.lineno} de dados_demo.py não usa string literal fixa"
            )
    assert chamadas >= 5


def test_dados_demo_nao_importa_nada_de_llm():
    codigo = (_RAIZ / "app" / "assistente" / "dados_demo.py").read_text(encoding="utf-8")
    arvore = ast.parse(codigo)
    for node in ast.walk(arvore):
        if isinstance(node, ast.ImportFrom) and node.module and "llm" in node.module:
            raise AssertionError("dados_demo.py importa de llm.py -- não deveria, é ferramenta de QA")
