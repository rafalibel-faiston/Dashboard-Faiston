"""Áreas, frentes e catálogo de pesos das atividades.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.acesso import CARGOS_SERVICE_DESK, CARGO_VALIDOS, PERFIL_VALIDOS, TIME_STATUS_REPORT, _times_validos_cur
from app.core.auth import get_session
from app.core.db import get_db
from app.service_desk.db import AREA_SERVICE_DESK


router = APIRouter()


# --- CATÁLOGO DE PESOS ---
class NovoTipoAtividade(BaseModel):
    frente_id: int
    nome: str
    peso: int
    descricao: str = ""

class PesoUpdate(BaseModel):
    peso: int

class NovaFrente(BaseModel):
    area: str
    nome: str

class AreaModel(BaseModel):
    nome: str
    usa_projetos: bool = True
    # None = não mexer. Antes o default era True e o PUT de renomear/ligar
    # projetos reativava área desativada sem ninguém pedir.
    ativo: Optional[bool] = None
    # None = não mexer (o PUT de renomear/ligar projetos não manda estas duas).
    # Lista vazia é tratada como "todos", pra não deixar área que não aceita
    # ninguém -- o admin tira o que não quer, não esvazia.
    cargos: Optional[List[str]] = None
    perfis: Optional[List[str]] = None

def _normalizar_lista_area(valores, catalogo):
    if valores is None:
        return None
    filtrada = [v for v in catalogo if v in valores]
    return filtrada or list(catalogo)

def _cargos_da_area(nome, cargos):
    """Cargos do Service Desk só valem na área Service Desk (regra de
    CARGOS_SERVICE_DESK) -- em qualquer outra área são descartados, senão quem
    recebesse o cargo lá passava a enxergar o módulo do SD."""
    if cargos is None or nome == AREA_SERVICE_DESK:
        return cargos
    return [c for c in cargos if c not in CARGOS_SERVICE_DESK] or            [c for c in CARGO_VALIDOS if c not in CARGOS_SERVICE_DESK]

# Áreas cujo NOME está amarrado no código: 'Projetos' decide o Status Report
# (TIME_STATUS_REPORT) e é o default de quem não tem área; 'Service Desk' é como
# o módulo do SD acha a equipe. Renomear qualquer uma quebrava o acesso e o
# boot recriava a área com o nome original, vazia.
AREAS_DO_SISTEMA = (TIME_STATUS_REPORT, AREA_SERVICE_DESK)

def _usuarios_ativos_na_area(cur, nome):
    cur.execute("SELECT COUNT(*) FROM usuarios WHERE COALESCE(time,'Projetos')=%s AND ativo=TRUE", (nome,))
    return cur.fetchone()[0]

@router.get("/api/areas")
def listar_areas(todas: bool = False, faiston_token: str = Cookie(None)):
    """Leitura liberada pra qualquer logado: o front monta com isto todo select
    de time e descobre se a área trabalha por projeto. `todas=1` (só admin)
    inclui as inativas -- é o que a tela de cadastro precisa."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    incluir_inativas = todas and sess["perfil"] == "admin"
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT a.id, a.nome, a.usa_projetos, a.ativo,
                   (SELECT COUNT(*) FROM usuarios u
                     WHERE COALESCE(u.time,'Projetos') = a.nome AND u.ativo = TRUE),
                   (SELECT COUNT(*) FROM projetos p
                     WHERE COALESCE(p.time,'Projetos') = a.nome AND p.ativo = TRUE),
                   a.cargos, a.perfis
            FROM areas a
            {'' if incluir_inativas else 'WHERE a.ativo = TRUE'}
            ORDER BY a.nome
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {
            # Catálogo junto da lista: a tela monta as caixas de seleção com
            # ele, e assim não repete os nomes em JavaScript.
            "catalogo": {"cargos": list(CARGO_VALIDOS), "perfis": list(PERFIL_VALIDOS)},
            "areas": [{"id": r[0], "nome": r[1], "usa_projetos": r[2], "ativo": r[3],
                       "usuarios": r[4], "projetos": r[5],
                       "cargos": [c for c in CARGO_VALIDOS if r[6] is None or c in r[6]],
                       "perfis": [pf for pf in PERFIL_VALIDOS if r[7] is None or pf in r[7]]}
                      for r in rows],
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/areas")
def criar_area(a: AreaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    nome = (a.nome or "").strip()
    if not nome: raise HTTPException(status_code=400, detail="Informe o nome da área")
    if len(nome) > 50: raise HTTPException(status_code=400, detail="Nome da área: no máximo 50 caracteres")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        import json as _json
        cargos = _cargos_da_area(nome, _normalizar_lista_area(a.cargos, CARGO_VALIDOS) or list(CARGO_VALIDOS))
        perfis = _normalizar_lista_area(a.perfis, PERFIL_VALIDOS) or list(PERFIL_VALIDOS)
        cur = conn.cursor()
        cur.execute("INSERT INTO areas (nome, usa_projetos, cargos, perfis) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (nome) DO NOTHING RETURNING id",
                    (nome, a.usa_projetos, _json.dumps(cargos), _json.dumps(perfis)))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=400, detail="Já existe uma área com esse nome")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": row[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/areas/{aid}")
def atualizar_area(aid: int, a: AreaModel, faiston_token: str = Cookie(None)):
    """Renomear propaga pras tabelas que guardam o NOME da área (usuarios,
    clientes, projetos, frentes): `time` nunca foi chave estrangeira, então sem
    a cascata o rename deixaria todo mundo apontando pro nome antigo. Tudo na
    mesma transação."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    nome = (a.nome or "").strip()
    if not nome: raise HTTPException(status_code=400, detail="Informe o nome da área")
    if len(nome) > 50: raise HTTPException(status_code=400, detail="Nome da área: no máximo 50 caracteres")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome, usa_projetos, ativo FROM areas WHERE id=%s", (aid,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Área não encontrada")
        nome_antigo, usava_projetos, estava_ativa = row[0], bool(row[1]), bool(row[2])
        if nome != nome_antigo and nome_antigo in AREAS_DO_SISTEMA:
            raise HTTPException(status_code=400, detail=(
                f"A área {nome_antigo} é usada pelo sistema e não pode ser renomeada"))
        # Desativar pelo PUT passa pela mesma regra do DELETE.
        if estava_ativa and a.ativo is False:
            n_users = _usuarios_ativos_na_area(cur, nome_antigo)
            if n_users:
                raise HTTPException(status_code=400, detail=(
                    f"A área {nome_antigo} tem {n_users} usuário(s) ativo(s) — "
                    "mova essas pessoas para outra área antes de desativar"))
        # Desligar o projeto de uma área que ainda tem projeto ativo deixaria
        # tarefa apontando pra projeto que nenhuma tela mostra mais. Barra e diz
        # quantos são, pro admin arquivar antes.
        if usava_projetos and not a.usa_projetos:
            cur.execute("SELECT COUNT(*) FROM projetos WHERE COALESCE(time,'Projetos')=%s AND ativo=TRUE",
                        (nome_antigo,))
            n_proj = cur.fetchone()[0]
            if n_proj:
                raise HTTPException(status_code=400, detail=(
                    f"A área {nome_antigo} tem {n_proj} projeto(s) ativo(s) — "
                    "arquive antes de marcá-la como área sem projetos"))
        if nome != nome_antigo:
            cur.execute("SELECT 1 FROM areas WHERE nome=%s AND id<>%s", (nome, aid))
            if cur.fetchone(): raise HTTPException(status_code=400, detail="Já existe uma área com esse nome")
            for tabela, coluna in (("usuarios", "time"), ("clientes", "time"),
                                   ("projetos", "time"), ("frentes", "area")):
                cur.execute(f"UPDATE {tabela} SET {coluna}=%s WHERE {coluna}=%s", (nome, nome_antigo))
        import json as _json
        cargos = _cargos_da_area(nome, _normalizar_lista_area(a.cargos, CARGO_VALIDOS))
        perfis = _normalizar_lista_area(a.perfis, PERFIL_VALIDOS)
        cur.execute("""UPDATE areas SET nome=%s, usa_projetos=%s, ativo=COALESCE(%s, ativo),
                              cargos=COALESCE(%s, cargos), perfis=COALESCE(%s, perfis)
                        WHERE id=%s""",
                    (nome, a.usa_projetos, a.ativo,
                     _json.dumps(cargos) if cargos is not None else None,
                     _json.dumps(perfis) if perfis is not None else None, aid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/areas/{aid}")
def desativar_area(aid: int, faiston_token: str = Cookie(None)):
    """Desativa em vez de apagar: usuário, cliente e projeto guardam o NOME da
    área, então um DELETE de verdade deixaria esse dado apontando pro vazio.
    Área com gente dentro não desativa -- esses usuários sumiriam de todo filtro
    por time."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome FROM areas WHERE id=%s", (aid,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Área não encontrada")
        n_users = _usuarios_ativos_na_area(cur, row[0])
        if n_users:
            raise HTTPException(status_code=400, detail=(
                f"A área {row[0]} tem {n_users} usuário(s) ativo(s) — "
                "mova essas pessoas para outra área antes de desativar"))
        cur.execute("UPDATE areas SET ativo=FALSE WHERE id=%s", (aid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/frentes")
def listar_frentes(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, area, nome FROM frentes WHERE ativo=TRUE ORDER BY area, nome")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "area": r[1], "nome": r[2]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/frentes")
def criar_frente(f: NovaFrente, faiston_token: str = Cookie(None)):
    """Cria uma nova frente (agrupador de tipos de atividade) dentro de uma
    área/time -- cada área tem seu próprio catálogo de peso, independente das
    outras (ex.: o peso do time de Desenvolvimento não tem nada a ver com o
    peso do time de Projetos)."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    if not f.nome.strip(): raise HTTPException(status_code=400, detail="Informe o nome da frente")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Valida contra o cadastro de áreas, não contra lista fixa -- é o que
        # permite montar o catálogo de atividades de uma área recém-criada.
        if f.area not in _times_validos_cur(cur):
            raise HTTPException(status_code=400, detail="Área inválida")
        cur.execute(
            "INSERT INTO frentes (area, nome) VALUES (%s,%s) ON CONFLICT (area, nome) DO NOTHING RETURNING id",
            (f.area, f.nome.strip())
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Já existe uma frente com esse nome nessa área")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": row[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/frentes/{fid}")
def desativar_frente(fid: int, faiston_token: str = Cookie(None)):
    """Desativa em vez de apagar -- mesmo padrão de desativar_tipo_atividade,
    pra tarefas antigas não perderem a referência."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE frentes SET ativo=FALSE WHERE id=%s", (fid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/tipos-atividade")
def listar_tipos_atividade(area: str = "", frente_id: Optional[int] = None,
                           faiston_token: str = Cookie(None)):
    """Leitura liberada pra qualquer usuário logado: todo mundo precisa
    consultar a tabela de pesos na hora de abrir a tarefa."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cond, params = ["f.ativo = TRUE", "t.ativo = TRUE"], []
        if area:
            cond.append("f.area = %s"); params.append(area)
        if frente_id:
            cond.append("t.frente_id = %s"); params.append(frente_id)
        cur.execute(f"""
            SELECT t.id, t.frente_id, f.area, f.nome, t.nome, t.peso, t.descricao
            FROM tipos_atividade t JOIN frentes f ON t.frente_id = f.id
            WHERE {' AND '.join(cond)}
            ORDER BY f.area, f.nome, t.peso DESC, t.nome
        """, tuple(params))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "frente_id": r[1], "area": r[2], "frente": r[3],
                 "nome": r[4], "peso": r[5], "descricao": r[6]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/tipos-atividade/{tid}/peso")
def atualizar_peso_atividade(tid: int, p: PesoUpdate, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    if not 1 <= p.peso <= 4: raise HTTPException(status_code=400, detail="Peso deve ser de 1 a 4")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tipos_atividade SET peso=%s WHERE id=%s", (p.peso, tid))
        if cur.rowcount == 0: raise HTTPException(status_code=404, detail="Tipo de atividade não encontrado")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tipos-atividade")
def criar_tipo_atividade(t: NovoTipoAtividade, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    if not t.nome.strip(): raise HTTPException(status_code=400, detail="Informe o nome da atividade")
    if not 1 <= t.peso <= 4: raise HTTPException(status_code=400, detail="Peso deve ser de 1 a 4")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tipos_atividade (frente_id, nome, peso, descricao) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (frente_id, nome) DO NOTHING RETURNING id",
            (t.frente_id, t.nome.strip(), t.peso, t.descricao)
        )
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=400, detail="Já existe uma atividade com esse nome nessa frente")
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": row[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/tipos-atividade/{tid}")
def desativar_tipo_atividade(tid: int, faiston_token: str = Cookie(None)):
    """Desativa em vez de apagar -- tarefas antigas continuam apontando pro
    tipo, então DELETE de verdade quebraria o histórico."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tipos_atividade SET ativo=FALSE WHERE id=%s", (tid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
