"""Áreas, cargos, perfis e regras de acesso compartilhadas pelas rotas.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi import HTTPException

from app.core.db import get_db


# Áreas (times) da operação. Isto aqui é só a CARGA INICIAL da tabela `areas`:
# a lista de verdade vem do banco (times_validos()), porque desde 2026-08-28 a
# área é cadastro editável na tela de admin -- abrir uma área nova deixou de
# exigir deploy.
TIMES_PADRAO = ('Projetos', 'Logística', 'Rede Credenciada', 'Desenvolvimento')


# Catálogo de cargos e perfis. Fixo de propósito: cada um destes tem
# comportamento preso no código (o N2 tem tela própria, o Backoffice arrasta
# card no Cronograma, o Analista atribui tarefa pro Backoffice...), então
# inventar um nome novo aqui daria um rótulo sem efeito nenhum. O que é
# configurável por área é QUAIS deles ela aceita -- ver a tabela `areas`.
CARGO_VALIDOS = ('analista', 'backoffice', 'n2', 'desenvolvedor', 'sd_operador', 'sd_supervisor')


# Cargos do Service Desk só valem na área Service Desk (criada pelo próprio
# módulo, app/service_desk/db.py) -- não entram na carga inicial das outras.
CARGOS_SERVICE_DESK = ('sd_operador', 'sd_supervisor')


PERFIL_VALIDOS = ('funcionario', 'gestor', 'diretor', 'demo', 'admin', 'dev')


# A área virou cadastro (tabela `areas`), então a lista de times válidos e a
# regra de "essa área trabalha por projeto?" saem do banco, não de constante.
# Quem já tem cursor aberto usa a variante _cur, pra não abrir uma segunda
# conexão no meio da transação.
def _times_validos_cur(cur) -> list:
    cur.execute("SELECT nome FROM areas WHERE ativo=TRUE ORDER BY nome")
    return [r[0] for r in cur.fetchall()]


def times_validos() -> list:
    """Nomes das áreas ativas. Cai pra TIMES_PADRAO com o banco fora -- validar
    cadastro não pode virar 500 por causa disso."""
    conn = get_db()
    if not conn: return list(TIMES_PADRAO)
    try:
        cur = conn.cursor()
        nomes = _times_validos_cur(cur)
        cur.close(); conn.close()
        return nomes or list(TIMES_PADRAO)
    except Exception:
        return list(TIMES_PADRAO)


def _area_usa_projetos_cur(cur, nome: str) -> bool:
    """Área fora do cadastro conta como 'usa projetos' -- é o comportamento de
    sempre, e evita travar dado legado cujo time saiu da lista."""
    cur.execute("SELECT usa_projetos FROM areas WHERE nome=%s", (nome or 'Projetos',))
    row = cur.fetchone()
    return True if not row else bool(row[0])


def _area_lista_cur(cur, coluna: str, nome: str, catalogo) -> list:
    """Cargos ou perfis habilitados na área. Área fora do cadastro devolve o
    catálogo inteiro -- mesma regra de _area_usa_projetos_cur: dado legado não
    pode ficar travado por causa de um nome que saiu da lista."""
    cur.execute(f"SELECT {coluna} FROM areas WHERE nome=%s", (nome or 'Projetos',))
    row = cur.fetchone()
    if not row or row[0] is None:
        return list(catalogo)
    return [v for v in row[0] if v in catalogo]


def _area_cargos_cur(cur, nome: str) -> list:
    return _area_lista_cur(cur, "cargos", nome, CARGO_VALIDOS)


def _area_perfis_cur(cur, nome: str) -> list:
    return _area_lista_cur(cur, "perfis", nome, PERFIL_VALIDOS)


def _exigir_area_com_projeto(cur, nome: str):
    if not _area_usa_projetos_cur(cur, nome):
        raise HTTPException(status_code=400,
                            detail=f"A área {nome} não trabalha por projeto — registre como demanda")


def _eh_n2(sess: dict) -> bool:
    """N2 deixou de ser perfil próprio e virou cargo dentro de
    perfil='funcionario' (migração 2026-07-28)."""
    return bool(sess) and sess.get("perfil") == "funcionario" and sess.get("cargo") == "n2"


# O Status Report (atividades de campo do N2) é operação do time de Projetos --
# Logística e Rede Credenciada não têm o que fazer lá. admin/diretor/dev
# atravessam times por definição (mesma regra do filtro de /api/metricas).
TIME_STATUS_REPORT = 'Projetos'


def _pode_ver_status_report(sess: dict) -> bool:
    if not sess:
        return False
    if sess.get("perfil") in ("admin", "diretor") or sess.get("perfil_real") == "dev":
        return True
    return (sess.get("time") or TIME_STATUS_REPORT) == TIME_STATUS_REPORT


def _exigir_status_report(sess: dict):
    """401 sem sessão, 403 fora do time de Projetos. Usado nas rotas de
    leitura do Status Report (as de escrita já dão 403 no guard de perfil)."""
    if not sess:
        raise HTTPException(status_code=401, detail="Não autenticado")
    if not _pode_ver_status_report(sess):
        raise HTTPException(status_code=403, detail="Status Report é restrito ao time de Projetos")


def _perfil_guia(perfil: str, cargo: str = "", perfil_real: str = "") -> str:
    """Traduz perfil+cargo do banco na aba correspondente do guia (/ajuda).
    O guia tem aba por FUNÇÃO, não por perfil cru: N2 e backoffice vivem
    dentro de perfil='funcionario' desde a migração de 2026-07-28."""
    if perfil == "funcionario" and cargo in ("n2", "backoffice"):
        return cargo
    if perfil_real == "dev":
        return "dev"
    return perfil or ""


def _eh_backoffice(sess: dict) -> bool:
    """Backoffice (cargo dentro de perfil='funcionario') arrasta card pra
    mudar status no Kanban de Cronograma reaproveitado do admin, cria
    atividade nova e importa planilha (2026-08-03, ampliado 2026-08-18) --
    mas não edita/exclui atividade já existente nem gera escala (isso
    continua só admin/gestor/demo/diretor)."""
    return bool(sess) and sess.get("perfil") == "funcionario" and sess.get("cargo") == "backoffice"


def _can_gestao(sess):
    return sess and sess["perfil"] in ("admin", "gestor")
