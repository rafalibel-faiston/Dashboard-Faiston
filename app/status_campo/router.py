"""Status de Campo (despachos técnicos por site/cliente).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from typing import List, Optional

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.acesso import _eh_backoffice, _eh_n2, _exigir_status_report, _pode_ver_status_report
from app.core.agenda import _bloqueio_ativo, _hoje_sp
from app.core.auth import get_session
from app.core.db import get_db


router = APIRouter()


# ── Status de Campo (despachos técnicos por site/cliente: ARCOS, ZAMP,
#    HAVAN, Câmeras IP etc.) — substitui a planilha STATUS_REPORT.xlsx ──────
STATUS_CAMPO_VALIDOS = ('agendado', 'em_andamento', 'concluido', 'parcial',
                         'improdutiva_cliente', 'improdutiva_faiston', 'cancelado')
STATUS_CAMPO_TERMINAIS = ('concluido', 'parcial', 'improdutiva_cliente', 'improdutiva_faiston')
PARTICULARIDADES_VALIDAS = ('reversa', 'equipamento_em_posse_do_cliente', 'equipamento_removido', 'equipamento_instalado', 'equipamento_reconfigurado')
LOCALIZACAO_VALIDOS = ('deslocamento', 'no_local')
ACESSO_VALIDOS = ('com_acesso', 'verificando_acesso', 'sem_acesso')
ANDAMENTO_TIPO_VALIDOS = ('instalando', 'trocando', 'removendo', 'validando')
MATERIAL_UNIDADE_VALIDOS = ('unidade', 'metro', 'caixa', 'rolo', 'par', 'pacote', 'kit')

# Lista única de colunas usada tanto em listar_status_campo quanto em
# obter_status_campo, pra não desalinhar SELECT/cols de novo (já causou
# bug em produção quando as colunas de material foram adicionadas).
STATUS_CAMPO_SELECT_SQL = """a.id, a.cliente_id, c.nome, a.data, a.horario_agendado, a.tecnico, a.tecnico_id,
                   a.n2_usuario_id, a.n2_responsavel,
                   a.site_sigla, a.site_nome, a.endereco, a.cidade, a.uf, a.hora_chegada, a.hora_termino,
                   a.detalhamento_tecnico, a.status, a.observacoes, a.criado_em, a.atualizado_em,
                   a.particularidades, a.material_utilizado, a.material_detalhe,
                   a.material_quantidade, a.material_valor, a.ticket, a.andamento_descricao,
                   a.localizacao, a.acesso, a.subprojeto, a.equipamento_removido_detalhe,
                   a.equipamento_instalado_serial, a.equipamento_removido_partnumber,
                   a.equipamento_removido_serial, a.contato_local_nome, a.contato_local_matricula,
                   a.hora_inicio_atividade, a.andamento_tipo, a.andamento_equipamento"""
STATUS_CAMPO_COLS = ["id", "cliente_id", "cliente_nome", "data", "horario_agendado", "tecnico", "tecnico_id",
        "n2_usuario_id", "n2_responsavel",
        "site_sigla", "site_nome", "endereco", "cidade", "uf", "hora_chegada", "hora_termino",
        "detalhamento_tecnico", "status", "observacoes", "criado_em", "atualizado_em",
        "particularidades", "material_utilizado", "material_detalhe",
        "material_quantidade", "material_valor", "ticket", "andamento_descricao",
        "localizacao", "acesso", "subprojeto", "equipamento_removido_detalhe",
        "equipamento_instalado_serial", "equipamento_removido_partnumber",
        "equipamento_removido_serial", "contato_local_nome", "contato_local_matricula",
        "hora_inicio_atividade", "andamento_tipo", "andamento_equipamento"]

class EquipamentoItem(BaseModel):
    partnumber: str = ""
    serial: str = ""
    posse: Optional[str] = None  # 'tecnico' | 'cliente' -- só relevante quando o item é do tipo 'removido'

class MaterialItem(BaseModel):
    descricao: str = ""
    quantidade: Optional[float] = None
    unidade: str = "unidade"
    valor: Optional[float] = None

class StatusAtividadeModel(BaseModel):
    cliente_id: int
    data: str
    horario_agendado: Optional[str] = None
    tecnico: str = ""
    tecnico_id: Optional[int] = None  # resolvido no front via nome digitado batendo com o cadastro de tecnicos
    n2_usuario_id: Optional[int] = None
    n2_responsavel: str = ""
    site_sigla: str = ""
    site_nome: str = ""
    endereco: str = ""
    cidade: str = ""
    uf: str = ""
    hora_chegada: Optional[str] = None
    hora_termino: Optional[str] = None  # usado como "hora de saída" no front, preenchido pelo N2
    detalhamento_tecnico: str = ""
    status: str = "agendado"
    observacoes: str = ""
    particularidades: List[str] = []
    material_utilizado: bool = False
    material_detalhe: str = ""
    material_quantidade: Optional[int] = None
    material_valor: Optional[float] = None
    ticket: str = ""
    andamento_descricao: str = ""
    localizacao: Optional[str] = None
    acesso: Optional[str] = None
    subprojeto: str = ""
    equipamento_removido_detalhe: str = ""
    # Opcional, preenchido só na tela de criação do painel principal (admin)
    # quando o equipamento a instalar/remover já é conhecido de antemão --
    # mesma tabela/formato usado na finalização (ver EquipamentoItem acima).
    equipamentos_instalados: List[EquipamentoItem] = []
    equipamentos_removidos: List[EquipamentoItem] = []
    equipamentos_reconfigurados: List[EquipamentoItem] = []

def _resolver_n2(cur, n2_usuario_id, n2_responsavel_texto):
    """Se veio n2_usuario_id, busca o nome pra cachear em n2_responsavel
    (mesmo padrão de comentarios_projeto.usuario_nome). Sem id, mantém o
    texto livre como veio (compatibilidade / preenchimento manual)."""
    if not n2_usuario_id:
        return None, n2_responsavel_texto
    cur.execute("SELECT nome FROM usuarios WHERE id=%s AND ativo=TRUE", (n2_usuario_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="N2 responsável inválido")
    return n2_usuario_id, row[0]

def _pode_gerenciar_status_campo(sess, conn, aid):
    """admin/gestor/demo podem tudo; n2 só nos despachos onde é o responsável."""
    if sess["perfil"] in ("admin", "gestor", "demo"):
        return True
    if _eh_n2(sess):
        cur = conn.cursor()
        cur.execute("SELECT n2_usuario_id FROM status_atividades WHERE id=%s", (aid,))
        row = cur.fetchone()
        cur.close()
        return bool(row) and row[0] == sess["id"]
    return False

@router.get("/api/status-campo")
def listar_status_campo(data: str = "", data_de: str = "", data_ate: str = "",
                         cliente_id: int = 0, status: str = "", texto: str = "",
                         n2_usuario_id: int = 0, apenas_meu: bool = False,
                         faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        where = ["1=1"]
        params = []
        if data:
            where.append("a.data = %s"); params.append(data)
        if data_de:
            where.append("a.data >= %s"); params.append(data_de)
        if data_ate:
            where.append("a.data <= %s"); params.append(data_ate)
        if cliente_id:
            where.append("a.cliente_id = %s"); params.append(cliente_id)
        if status:
            where.append("a.status = %s"); params.append(status)
        if apenas_meu:
            where.append("a.n2_usuario_id = %s"); params.append(sess["id"])
        elif n2_usuario_id:
            where.append("a.n2_usuario_id = %s"); params.append(n2_usuario_id)
        if texto:
            where.append("""(a.tecnico ILIKE %s OR a.n2_responsavel ILIKE %s OR a.site_sigla ILIKE %s
                              OR a.site_nome ILIKE %s OR a.cidade ILIKE %s OR a.observacoes ILIKE %s)""")
            like = f"%{texto}%"
            params.extend([like, like, like, like, like, like])
        cur.execute(f"""
            SELECT {STATUS_CAMPO_SELECT_SQL}
            FROM status_atividades a
            LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {' AND '.join(where)}
            ORDER BY a.data DESC, a.horario_agendado ASC NULLS LAST
            LIMIT 300
        """, params)
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        materiais_por_id = {}
        if ids:
            cur.execute("""
                SELECT atividade_id, descricao, quantidade, unidade, valor
                FROM status_atividade_materiais WHERE atividade_id = ANY(%s) ORDER BY id
            """, (ids,))
            for m in cur.fetchall():
                materiais_por_id.setdefault(m[0], []).append({
                    "descricao": m[1], "quantidade": float(m[2]) if m[2] is not None else None,
                    "unidade": m[3], "valor": float(m[4]) if m[4] is not None else None})
        cur.close(); conn.close()
        out = []
        for r in rows:
            item = dict(zip(STATUS_CAMPO_COLS, r))
            item["data"] = str(item["data"]) if item["data"] else None
            for k in ("horario_agendado", "hora_chegada", "hora_termino", "hora_inicio_atividade"):
                item[k] = str(item[k])[:5] if item[k] else None
            for k in ("criado_em", "atualizado_em"):
                item[k] = str(item[k])[:16] if item[k] else None
            item["particularidades"] = item["particularidades"] or []
            item["materiais"] = materiais_por_id.get(item["id"], [])
            out.append(item)
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/status-campo/report")
def report_status_campo(data: str, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.id, c.nome, a.site_sigla, a.site_nome, a.tecnico, a.status, a.observacoes,
                   a.horario_agendado, a.cidade, a.uf, a.n2_responsavel,
                   a.particularidades, a.material_utilizado, a.material_detalhe,
                   a.material_quantidade, a.material_valor, a.ticket,
                   a.andamento_descricao, a.localizacao, a.acesso, a.subprojeto,
                   a.equipamento_removido_detalhe, a.andamento_tipo, a.andamento_equipamento,
                   a.hora_inicio_atividade
            FROM status_atividades a
            LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE a.data = %s
            ORDER BY c.nome NULLS LAST, a.horario_agendado ASC NULLS LAST
        """, (data,))
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        materiais_por_id = {}
        if ids:
            cur.execute("""
                SELECT atividade_id, descricao, quantidade, unidade, valor
                FROM status_atividade_materiais WHERE atividade_id = ANY(%s) ORDER BY id
            """, (ids,))
            for m in cur.fetchall():
                materiais_por_id.setdefault(m[0], []).append({
                    "descricao": m[1], "quantidade": float(m[2]) if m[2] is not None else None,
                    "unidade": m[3], "valor": float(m[4]) if m[4] is not None else None})
        cur.close(); conn.close()
        contagem = {}
        por_cliente = {}
        for r in rows:
            (aid, cliente_nome, sigla, nome_site, tecnico, status, obs, horario, cidade, uf, n2,
             particularidades, material_utilizado, material_detalhe,
             material_quantidade, material_valor, ticket,
             andamento_descricao, localizacao, acesso, subprojeto, equip_removido_detalhe,
             andamento_tipo, andamento_equipamento, hora_inicio_atividade) = r
            cliente_nome = cliente_nome or "Sem cliente"
            contagem[status] = contagem.get(status, 0) + 1
            por_cliente.setdefault(cliente_nome, []).append({
                "id": aid, "site": sigla or nome_site or "", "tecnico": tecnico,
                "status": status, "observacoes": obs, "horario_agendado": str(horario)[:5] if horario else None,
                "cidade": cidade, "uf": uf, "n2_responsavel": n2,
                "particularidades": particularidades or [], "material_utilizado": material_utilizado,
                "materiais": materiais_por_id.get(aid, []),
                "ticket": ticket, "andamento_descricao": andamento_descricao,
                "localizacao": localizacao, "acesso": acesso, "subprojeto": subprojeto,
                "equipamento_removido_detalhe": equip_removido_detalhe,
                "andamento_tipo": andamento_tipo, "andamento_equipamento": andamento_equipamento,
                "hora_inicio_atividade": str(hora_inicio_atividade)[:5] if hora_inicio_atividade else None,
            })
        return {"data": data, "contagem": contagem, "por_cliente": por_cliente, "total": len(rows)}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/status-campo/subprojetos")
def listar_subprojetos(cliente_id: int, faiston_token: str = Cookie(None)):
    """Sugestões de subprojeto já usados nesse cliente, pra autocomplete."""
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT subprojeto FROM status_atividades
            WHERE cliente_id=%s AND subprojeto IS NOT NULL AND subprojeto != ''
            ORDER BY subprojeto
        """, (cliente_id,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [r[0] for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/status-campo/{aid}")
def obter_status_campo(aid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT {STATUS_CAMPO_SELECT_SQL}
            FROM status_atividades a
            LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE a.id = %s
        """, (aid,))
        r = cur.fetchone()
        if not r:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="Atividade não encontrada")
        item = dict(zip(STATUS_CAMPO_COLS, r))
        item["data"] = str(item["data"]) if item["data"] else None
        for k in ("horario_agendado", "hora_chegada", "hora_termino", "hora_inicio_atividade"):
            item[k] = str(item[k])[:5] if item[k] else None
        for k in ("criado_em", "atualizado_em"):
            item[k] = str(item[k])[:16] if item[k] else None
        item["particularidades"] = item["particularidades"] or []
        cur.execute("SELECT tipo, partnumber, serial, posse FROM status_atividade_equipamentos WHERE atividade_id=%s ORDER BY id", (aid,))
        item["equipamentos"] = [{"tipo": e[0], "partnumber": e[1], "serial": e[2], "posse": e[3]} for e in cur.fetchall()]
        cur.execute("SELECT descricao, quantidade, unidade, valor FROM status_atividade_materiais WHERE atividade_id=%s ORDER BY id", (aid,))
        item["materiais"] = [{"descricao": m[0], "quantidade": float(m[1]) if m[1] is not None else None,
                               "unidade": m[2], "valor": float(m[3]) if m[3] is not None else None} for m in cur.fetchall()]
        cur.close(); conn.close()
        return item
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/status-campo")
def criar_status_campo(a: StatusAtividadeModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or (sess["perfil"] not in ("admin", "gestor", "demo", "diretor") and not _eh_n2(sess) and not _eh_backoffice(sess)): raise HTTPException(status_code=403)
    if not _pode_ver_status_report(sess): raise HTTPException(status_code=403, detail="Status Report é restrito ao time de Projetos")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        # Uma atividade não pode nascer já concluída/improdutiva -- status
        # inicial é sempre "agendado", independente do que vier no corpo.
        status = "agendado"
        # N2 só cria despacho atribuído a si mesmo -- ignora qualquer
        # n2_usuario_id que venha no corpo da requisição.
        n2_uid = sess["id"] if _eh_n2(sess) else a.n2_usuario_id
        n2_uid, n2_nome = _resolver_n2(cur, n2_uid, a.n2_responsavel)
        bloqueio = _bloqueio_ativo(cur, n2_uid, a.data, a.horario_agendado)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"N2 indisponível nesta data/horário: {bloqueio}")
        particularidades = [p for p in a.particularidades if p in PARTICULARIDADES_VALIDAS]
        localizacao = a.localizacao if a.localizacao in LOCALIZACAO_VALIDOS else None
        acesso = a.acesso if a.acesso in ACESSO_VALIDOS else None
        # Serial a instalar/remover é opcional e só existe na tela de criação
        # do painel principal -- se veio preenchido, garante que a
        # particularidade correspondente também reflita isso.
        instalados = [(it.partnumber.strip(), it.serial.strip()) for it in a.equipamentos_instalados
                      if it.partnumber.strip() or it.serial.strip()]
        removidos = [(it.partnumber.strip(), it.serial.strip()) for it in a.equipamentos_removidos
                     if it.partnumber.strip() or it.serial.strip()]
        reconfigurados = [(it.partnumber.strip(), it.serial.strip()) for it in a.equipamentos_reconfigurados
                           if it.partnumber.strip() or it.serial.strip()]
        if instalados and 'equipamento_instalado' not in particularidades:
            particularidades.append('equipamento_instalado')
        if removidos and 'equipamento_removido' not in particularidades:
            particularidades.append('equipamento_removido')
        if reconfigurados and 'equipamento_reconfigurado' not in particularidades:
            particularidades.append('equipamento_reconfigurado')
        cur.execute("""
            INSERT INTO status_atividades (cliente_id, data, horario_agendado, tecnico, tecnico_id,
                n2_usuario_id, n2_responsavel,
                site_sigla, site_nome, endereco, cidade, uf, hora_chegada, hora_termino,
                detalhamento_tecnico, status, observacoes, criado_por,
                particularidades, material_utilizado, material_detalhe,
                material_quantidade, material_valor, ticket, andamento_descricao,
                localizacao, acesso, subprojeto, equipamento_removido_detalhe)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (a.cliente_id, a.data, a.horario_agendado or None, a.tecnico, a.tecnico_id, n2_uid, n2_nome,
              a.site_sigla, a.site_nome, a.endereco, a.cidade, a.uf.upper()[:2],
              a.hora_chegada or None, a.hora_termino or None,
              a.detalhamento_tecnico, status, a.observacoes, sess["id"],
              particularidades, a.material_utilizado, a.material_detalhe if a.material_utilizado else "",
              a.material_quantidade if a.material_utilizado else None,
              a.material_valor if a.material_utilizado else None, a.ticket, a.andamento_descricao,
              localizacao, acesso, a.subprojeto,
              a.equipamento_removido_detalhe if 'equipamento_removido' in particularidades else ""))
        new_id = cur.fetchone()[0]
        itens = [(new_id, 'instalado', pn, sn) for pn, sn in instalados] + \
                [(new_id, 'removido', pn, sn) for pn, sn in removidos] + \
                [(new_id, 'reconfigurado', pn, sn) for pn, sn in reconfigurados]
        if itens:
            cur.executemany(
                "INSERT INTO status_atividade_equipamentos (atividade_id, tipo, partnumber, serial) VALUES (%s,%s,%s,%s)",
                itens)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/status-campo/{aid}")
def atualizar_status_campo(aid: int, a: StatusAtividadeModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or (sess["perfil"] not in ("admin", "gestor", "demo", "diretor") and not _eh_n2(sess)): raise HTTPException(status_code=403)
    if not _pode_ver_status_report(sess): raise HTTPException(status_code=403, detail="Status Report é restrito ao time de Projetos")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        if not _pode_gerenciar_status_campo(sess, conn, aid):
            raise HTTPException(status_code=403, detail="Você só pode editar atividades onde é o N2 responsável")
        cur = conn.cursor()
        status = a.status if a.status in STATUS_CAMPO_VALIDOS else "agendado"
        n2_uid = sess["id"] if _eh_n2(sess) else a.n2_usuario_id
        n2_uid, n2_nome = _resolver_n2(cur, n2_uid, a.n2_responsavel)
        bloqueio = _bloqueio_ativo(cur, n2_uid, a.data, a.horario_agendado)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"N2 indisponível nesta data/horário: {bloqueio}")
        particularidades = [p for p in a.particularidades if p in PARTICULARIDADES_VALIDAS]
        localizacao = a.localizacao if a.localizacao in LOCALIZACAO_VALIDOS else None
        acesso = a.acesso if a.acesso in ACESSO_VALIDOS else None
        cur.execute("""
            UPDATE status_atividades SET
                cliente_id=%s, data=%s, horario_agendado=%s, tecnico=%s, tecnico_id=%s,
                n2_usuario_id=%s, n2_responsavel=%s,
                site_sigla=%s, site_nome=%s, endereco=%s, cidade=%s, uf=%s,
                hora_chegada=%s, hora_termino=%s, detalhamento_tecnico=%s, status=%s, observacoes=%s,
                particularidades=%s, material_utilizado=%s, material_detalhe=%s,
                material_quantidade=%s, material_valor=%s, ticket=%s, andamento_descricao=%s,
                localizacao=%s, acesso=%s, subprojeto=%s, equipamento_removido_detalhe=%s,
                atualizado_em=NOW()
            WHERE id=%s
        """, (a.cliente_id, a.data, a.horario_agendado or None, a.tecnico, a.tecnico_id, n2_uid, n2_nome,
              a.site_sigla, a.site_nome, a.endereco, a.cidade, a.uf.upper()[:2],
              a.hora_chegada or None, a.hora_termino or None,
              a.detalhamento_tecnico, status, a.observacoes,
              particularidades, a.material_utilizado, a.material_detalhe if a.material_utilizado else "",
              a.material_quantidade if a.material_utilizado else None,
              a.material_valor if a.material_utilizado else None, a.ticket, a.andamento_descricao,
              localizacao, acesso, a.subprojeto,
              a.equipamento_removido_detalhe if 'equipamento_removido' in particularidades else "", aid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _pode_ver_tecnicos(sess):
    """Mesmo grupo que já pode mexer em atividade de campo -- ver
    _pode_gerenciar_status_campo/atualizar_status_campo_status. Endpoint de
    detalhe devolve RG/CPF de gente real, não abre pra qualquer perfil."""
    return bool(sess) and (sess["perfil"] in ("admin", "gestor", "demo", "diretor") or _eh_n2(sess) or _eh_backoffice(sess))

@router.get("/api/tecnicos")
def listar_tecnicos(estado: str = "", faiston_token: str = Cookie(None)):
    """Lista leve (id+nome, sem PII) pra alimentar a busca de técnico por UF
    na tela de atividade. RG/CPF só saem em GET /api/tecnicos/{id}."""
    sess = get_session(faiston_token)
    if not _pode_ver_tecnicos(sess): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if estado.strip():
            cur.execute("SELECT id, nome FROM tecnicos WHERE ativo=TRUE AND UPPER(estado)=%s ORDER BY nome",
                        (estado.strip().upper()[:2],))
        else:
            cur.execute("SELECT id, nome FROM tecnicos WHERE ativo=TRUE ORDER BY nome LIMIT 500")
        out = [{"id": r[0], "nome": r[1]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/tecnicos/{tid}")
def obter_tecnico(tid: int, faiston_token: str = Cookie(None)):
    """Dado sensível (RG/CPF) -- buscado só quando a pessoa clica em 'Ver
    dados do técnico', não vai em nenhuma listagem/relatório."""
    sess = get_session(faiston_token)
    if not _pode_ver_tecnicos(sess): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome, rg, cpf_cnpj, telefone, especialidade FROM tecnicos WHERE id=%s", (tid,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row: raise HTTPException(status_code=404, detail="Técnico não encontrado")
        return {"nome": row[0], "rg": row[1], "cpf_cnpj": row[2], "telefone": row[3], "especialidade": row[4]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class ReatribuirStatusCampoModel(BaseModel):
    novo_n2_usuario_id: int

@router.post("/api/status-campo/{aid}/reatribuir")
def reatribuir_status_campo(aid: int, body: ReatribuirStatusCampoModel, faiston_token: str = Cookie(None)):
    """Handoff self-service: o próprio N2 responsável passa a atividade pra
    outro N2 (ex. precisa sair no meio do atendimento) -- antes só admin/
    gestor conseguiam trocar o responsável, pela tela de edição (2026-07-30,
    a pedido do usuário). Preserva tudo: situação/andamento atuais e o
    histórico de quem fez o quê continuam intactos, só o dono muda daqui
    pra frente -- ver _pode_gerenciar_status_campo/andamentos."""
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        if not _pode_gerenciar_status_campo(sess, conn, aid):
            raise HTTPException(status_code=403, detail="Você só pode reatribuir atividades onde é o N2 responsável")
        cur = conn.cursor()
        cur.execute("SELECT nome FROM usuarios WHERE id=%s AND ativo=TRUE AND perfil='funcionario' AND cargo='n2'",
                    (body.novo_n2_usuario_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Usuário informado não é um N2 ativo")
        novo_nome = row[0]
        cur.execute("SELECT n2_responsavel, data, horario_agendado FROM status_atividades WHERE id=%s", (aid,))
        antigo_nome, data_val, horario_val = cur.fetchone()
        if body.novo_n2_usuario_id == sess["id"]:
            raise HTTPException(status_code=400, detail="Escolha outro N2 -- essa atividade já é sua")
        bloqueio = _bloqueio_ativo(cur, body.novo_n2_usuario_id, str(data_val), horario_val)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"N2 indisponível nesta data/horário: {bloqueio}")
        cur.execute("""
            UPDATE status_atividades SET n2_usuario_id=%s, n2_responsavel=%s, atualizado_em=NOW()
            WHERE id=%s
        """, (body.novo_n2_usuario_id, novo_nome, aid))
        cur.execute("""
            INSERT INTO status_atividade_andamentos (atividade_id, descricao, criado_por, criado_por_nome)
            VALUES (%s, %s, %s, %s)
        """, (aid, f"Atividade repassada de {antigo_nome or sess['nome']} para {novo_nome}", sess["id"], sess["nome"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "novo_responsavel": novo_nome}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class StatusCampoStatusModel(BaseModel):
    status: str
    andamento_descricao: Optional[str] = None
    localizacao: Optional[str] = None
    acesso: Optional[str] = None
    hora_chegada: Optional[str] = None
    hora_inicio_atividade: Optional[str] = None
    andamento_tipo: Optional[str] = None
    andamento_equipamento: Optional[str] = None
    hora_termino: Optional[str] = None
    material_utilizado: Optional[bool] = None
    # Lista (não mais um campo único) -- uma visita pode usar mais de um
    # material, e a quantidade nem sempre é contagem de item (ex.: "28"
    # com unidade "metro" pra cabo de rede). Ver MaterialItem/MATERIAL_UNIDADE_VALIDOS.
    materiais: List[MaterialItem] = []
    # Coletados na finalização (tela "Finalizar atividade"), pra montar o
    # carimbo de encerramento -- ver POST/README do carimbo no front-end.
    # Instalado/removido não são mais mutuamente exclusivos (uma atividade
    # pode envolver os dois ao mesmo tempo) e cada um vira uma lista, já
    # que pode ter mais de uma unidade instalada/removida na mesma visita.
    equipamento_status: List[str] = []  # 'equipamento_instalado' e/ou 'equipamento_removido' e/ou 'equipamento_reconfigurado'
    equipamentos_instalados: List[EquipamentoItem] = []
    equipamentos_removidos: List[EquipamentoItem] = []
    equipamentos_reconfigurados: List[EquipamentoItem] = []
    # Posse agora é por item (EquipamentoItem.posse) -- ver seção "Posse" na
    # migração da tabela status_atividade_equipamentos.
    contato_local_nome: Optional[str] = None
    contato_local_matricula: Optional[str] = None
    # Preenchimento incremental de serial durante o "Atualizar andamento"
    # (2026-08-11), em vez de só na finalização -- opcional, só grava se vier
    # com partnumber/serial preenchido. andamento_equip_tipo escolhe em qual
    # lista entra ('instalado'/'removido'/'reconfigurado'); não é derivado
    # automaticamente de andamento_tipo porque 'trocando' pode ser os dois.
    andamento_equip_tipo: Optional[str] = None
    andamento_equip_item: Optional[EquipamentoItem] = None
    observacoes: Optional[str] = None

def _registrar_andamento(cur, aid, localizacao, acesso, descricao, sess,
                          hora_chegada=None, hora_inicio_atividade=None, andamento_tipo=None,
                          andamento_equipamento=None):
    """Atualiza o snapshot de situação/andamento em status_atividades e, só
    quando há conteúdo real de andamento (tipo e/ou equipamento e/ou
    descrição preenchidos), grava também uma entrada no histórico. Um só
    botão/modal ("Atualizar", ver n2.html) cobre tanto situação (localização/
    acesso/chegada) quanto andamento (tipo/equipamento) -- uma chamada que só
    atualiza situação não passou a poluir a linha do tempo com entradas
    vazias; uma chamada que também descreve o que está sendo feito, sim.
    Todos os campos de horário/localização são COALESCE no update do
    snapshot: só sobrescrevem quando vêm preenchidos, pra uma chamada
    parcial não apagar o que já estava registrado."""
    localizacao = localizacao if localizacao in LOCALIZACAO_VALIDOS else None
    acesso = acesso if acesso in ACESSO_VALIDOS else None
    andamento_tipo = andamento_tipo if andamento_tipo in ANDAMENTO_TIPO_VALIDOS else None
    andamento_equipamento = (andamento_equipamento or "").strip()
    descricao = descricao.strip()
    if andamento_tipo or andamento_equipamento or descricao:
        cur.execute("""
            INSERT INTO status_atividade_andamentos
                (atividade_id, localizacao, acesso, descricao, criado_por, criado_por_nome,
                 hora_chegada, hora_inicio_atividade, andamento_tipo, andamento_equipamento)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (aid, localizacao, acesso, descricao, sess["id"], sess["nome"],
              hora_chegada or None, hora_inicio_atividade or None, andamento_tipo,
              andamento_equipamento))
    cur.execute("""
        UPDATE status_atividades SET andamento_descricao=%s,
            localizacao=COALESCE(%s, localizacao), acesso=COALESCE(%s, acesso),
            hora_chegada=COALESCE(%s, hora_chegada),
            hora_inicio_atividade=COALESCE(%s, hora_inicio_atividade),
            andamento_tipo=COALESCE(%s, andamento_tipo),
            andamento_equipamento=COALESCE(NULLIF(%s, ''), andamento_equipamento),
            atualizado_em=NOW()
        WHERE id=%s
    """, (descricao, localizacao, acesso, hora_chegada or None, hora_inicio_atividade or None,
          andamento_tipo, andamento_equipamento, aid))

def _gerar_ou_atualizar_tarefa_campo(cur, aid):
    """N2-A: ao finalizar uma visita de campo (qualquer status terminal --
    concluído, parcial ou improdutiva), cria sozinho uma tarefa "Atendimento
    em campo" (peso 4) atribuída ao N2 responsável, sem ele digitar nada.
    Decisão de negócio (2026-07-28): visita malsucedida conta igual a uma
    bem-sucedida -- o peso mede esforço/complexidade da atividade, não o
    resultado. Previsão de conclusão = data agendada da própria visita.
    Idempotente: se a atividade já gerou uma tarefa antes (reeditada), essa
    mesma tarefa é atualizada em vez de duplicada (status_atividades.tarefa_gerada_id)."""
    cur.execute("""
        SELECT sa.data, sa.n2_usuario_id, sa.tarefa_gerada_id, sa.subprojeto, COALESCE(c.nome, sa.site_nome, ''),
               GREATEST(0, COALESCE(EXTRACT(EPOCH FROM (
                   CASE WHEN sa.hora_termino < sa.hora_chegada
                        THEN (sa.hora_termino - sa.hora_chegada) + INTERVAL '24 hours'
                        ELSE sa.hora_termino - sa.hora_chegada END
               )), 0))::int
        FROM status_atividades sa LEFT JOIN clientes c ON c.id = sa.cliente_id
        WHERE sa.id = %s
    """, (aid,))
    row = cur.fetchone()
    if not row: return
    data_visita, n2_uid, tarefa_id, subprojeto, cliente_nome, segundos = row
    if not n2_uid: return  # sem N2 responsável definido -- nada a gerar
    cur.execute("""
        SELECT ta.id, ta.peso FROM tipos_atividade ta JOIN frentes f ON f.id = ta.frente_id
        WHERE f.area='Projetos' AND f.nome='N2' AND ta.nome='Atendimento em campo' AND ta.ativo=TRUE
    """)
    row_tipo = cur.fetchone()
    if not row_tipo: return  # catálogo sem esse tipo cadastrado -- não bloqueia o fluxo de campo
    tipo_id, peso = row_tipo
    descricao = f"Atendimento em campo — {subprojeto}" if subprojeto else "Atendimento em campo"
    prazo_status = "dentro" if (not data_visita or _hoje_sp() <= data_visita) else "fora"
    # Horas da visita = hora_termino - hora_chegada (fica 0 se algum dos dois
    # não foi preenchido, ex. quando o N2 pula a etapa "no local"/chegada).
    # Atividade que cruza a meia-noite (ex. chegada 22h, término 2h) tem
    # hora_termino < hora_chegada -- sem o CASE acima isso dava diferença
    # negativa e o GREATEST(0,...) zerava a duração inteira da visita
    # (achado real, 2026-07-30: atividade noturna ficava com 0h trabalhadas).
    if tarefa_id:
        cur.execute("""
            UPDATE tarefas SET descricao=%s, cliente=%s, status='concluido', tipo_atividade_id=%s, peso=%s,
                   segundos=%s, data_prazo=%s, concluido_em=NOW(), prazo_status=%s, atualizado_em=NOW()
            WHERE id=%s
        """, (descricao, cliente_nome, tipo_id, peso, segundos, data_visita, prazo_status, tarefa_id))
    else:
        cur.execute("""
            INSERT INTO tarefas (usuario_id, descricao, cliente, status, tipo_atividade_id, peso, segundos,
                                  natureza, data_prazo, concluido_em, prazo_status)
            VALUES (%s,%s,%s,'concluido',%s,%s,%s,'programada',%s,NOW(),%s) RETURNING id
        """, (n2_uid, descricao, cliente_nome, tipo_id, peso, segundos, data_visita, prazo_status))
        cur.execute("UPDATE status_atividades SET tarefa_gerada_id=%s WHERE id=%s", (cur.fetchone()[0], aid))

@router.patch("/api/status-campo/{aid}/status")
def atualizar_status_campo_status(aid: int, body: StatusCampoStatusModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or (sess["perfil"] not in ("admin", "gestor", "demo", "diretor") and not _eh_n2(sess) and not _eh_backoffice(sess)): raise HTTPException(status_code=403)
    if not _pode_ver_status_report(sess): raise HTTPException(status_code=403, detail="Status Report é restrito ao time de Projetos")
    if body.status not in STATUS_CAMPO_VALIDOS: raise HTTPException(status_code=400, detail="Status inválido")
    # "em_andamento" exige localização (primeiro passo do fluxo escalonado do
    # N2: deslocamento/no local -> chegada+acesso -> início+tipo); os status
    # terminais exigem confirmar a hora de saída (se houve material fica a
    # critério do N2, mas o campo precisa vir preenchido explicitamente).
    if body.status == "em_andamento" and body.localizacao not in LOCALIZACAO_VALIDOS:
        raise HTTPException(status_code=400, detail="Selecione a localização")
    # Progressão: só faz sentido dizer o que está sendo instalado/trocado/
    # removido depois que o acesso ao local foi confirmado (o próprio modal
    # só libera esses campos nesse ponto) -- e tipo sem dizer o quê é
    # inexistente na prática, então equipamento é obrigatório junto do tipo.
    if body.status == "em_andamento" and body.andamento_tipo in ANDAMENTO_TIPO_VALIDOS:
        if body.acesso != "com_acesso":
            raise HTTPException(status_code=400, detail="Confirme o acesso ao local antes de iniciar a atividade")
        if not (body.andamento_equipamento or "").strip():
            raise HTTPException(status_code=400, detail="Diga o que está sendo instalado/trocado/removido")
    if body.status in STATUS_CAMPO_TERMINAIS:
        if not body.hora_termino:
            raise HTTPException(status_code=400, detail="Informe a hora de saída para finalizar a atividade")
        if body.material_utilizado is None:
            raise HTTPException(status_code=400, detail="Informe se houve utilização de material")
        if body.material_utilizado and not any(m.descricao.strip() for m in body.materiais):
            raise HTTPException(status_code=400, detail="Informe ao menos um material utilizado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        # Backoffice não é "responsável" de nenhum despacho (não é N2) --
        # mudar status é liberado pra qualquer atividade, igual admin/gestor,
        # mas só aqui (não passa por _pode_gerenciar_status_campo, que também
        # governa reatribuir/editar/excluir -- esses continuam bloqueados
        # pelo check de perfil logo acima).
        if not (_pode_gerenciar_status_campo(sess, conn, aid) or _eh_backoffice(sess)):
            raise HTTPException(status_code=403, detail="Você só pode editar atividades onde é o N2 responsável")
        cur = conn.cursor()
        sets = ["status=%s", "atualizado_em=NOW()"]
        params = [body.status]
        if body.status in STATUS_CAMPO_TERMINAIS:
            material_ok = bool(body.material_utilizado)
            sets += ["hora_termino=%s", "material_utilizado=%s"]
            params += [body.hora_termino, material_ok]
            # Merge das particularidades ligadas ao equipamento (instalado/
            # removido/reconfigurado/posse) -- preserva 'reversa' e qualquer
            # outra tag que não seja dessas 4, só substitui o que veio do
            # modal de finalizar.
            equip_tags = [t for t in body.equipamento_status if t in
                          ('equipamento_instalado', 'equipamento_removido', 'equipamento_reconfigurado')]
            instalado = 'equipamento_instalado' in equip_tags
            removido = 'equipamento_removido' in equip_tags
            reconfigurado = 'equipamento_reconfigurado' in equip_tags
            cur.execute("SELECT particularidades FROM status_atividades WHERE id=%s", (aid,))
            atuais = (cur.fetchone() or [[]])[0] or []
            outras = [p for p in atuais if p not in
                      ('equipamento_instalado', 'equipamento_removido', 'equipamento_reconfigurado', 'equipamento_em_posse_do_cliente')]
            novas_particularidades = outras + equip_tags
            # Posse é por item agora (EquipamentoItem.posse) -- a particularidade
            # 'equipamento_em_posse_do_cliente' continua existindo como resumo
            # (pelo menos um item removido ficou com o cliente), pra quem ainda
            # lê essa tag pra exibir um badge, mas não é mais a fonte da verdade.
            if removido and any(it.posse == 'cliente' for it in body.equipamentos_removidos
                                 if it.partnumber.strip() or it.serial.strip()):
                novas_particularidades.append('equipamento_em_posse_do_cliente')
            sets += ["particularidades=%s", "contato_local_nome=%s", "contato_local_matricula=%s", "observacoes=%s"]
            params += [novas_particularidades,
                       body.contato_local_nome or "", body.contato_local_matricula or "",
                       body.observacoes or ""]
        params.append(aid)
        cur.execute(f"UPDATE status_atividades SET {', '.join(sets)} WHERE id=%s", params)
        if body.status in STATUS_CAMPO_TERMINAIS:
            # Lista de equipamentos é sempre substituída por completo (não
            # incremental) -- reflete exatamente o que veio do modal de
            # finalizar nesta confirmação, igual o resto do fluxo.
            cur.execute("DELETE FROM status_atividade_equipamentos WHERE atividade_id=%s", (aid,))
            itens = []
            if instalado:
                itens += [(aid, 'instalado', it.partnumber.strip(), it.serial.strip(), None)
                          for it in body.equipamentos_instalados if it.partnumber.strip() or it.serial.strip()]
            if removido:
                itens += [(aid, 'removido', it.partnumber.strip(), it.serial.strip(),
                           it.posse if it.posse in ('tecnico', 'cliente') else 'tecnico')
                          for it in body.equipamentos_removidos if it.partnumber.strip() or it.serial.strip()]
            if reconfigurado:
                itens += [(aid, 'reconfigurado', it.partnumber.strip(), it.serial.strip(), None)
                          for it in body.equipamentos_reconfigurados if it.partnumber.strip() or it.serial.strip()]
            if itens:
                cur.executemany(
                    "INSERT INTO status_atividade_equipamentos (atividade_id, tipo, partnumber, serial, posse) VALUES (%s,%s,%s,%s,%s)",
                    itens)
            # Lista de materiais também é sempre substituída por completo,
            # mesmo padrão dos equipamentos acima -- reflete exatamente o
            # que veio do modal de finalizar nesta confirmação.
            cur.execute("DELETE FROM status_atividade_materiais WHERE atividade_id=%s", (aid,))
            if material_ok:
                materiais_itens = [
                    (aid, m.descricao.strip(), m.quantidade,
                     m.unidade if m.unidade in MATERIAL_UNIDADE_VALIDOS else 'unidade', m.valor)
                    for m in body.materiais if m.descricao.strip()
                ]
                if materiais_itens:
                    cur.executemany(
                        "INSERT INTO status_atividade_materiais (atividade_id, descricao, quantidade, unidade, valor) VALUES (%s,%s,%s,%s,%s)",
                        materiais_itens)
            _gerar_ou_atualizar_tarefa_campo(cur, aid)
        if body.status == "em_andamento":
            _registrar_andamento(cur, aid, body.localizacao, body.acesso, body.andamento_descricao or "", sess,
                                 body.hora_chegada, body.hora_inicio_atividade, body.andamento_tipo,
                                 body.andamento_equipamento)
            # Preenchimento incremental de serial (2026-08-11): junto de uma
            # atualização de andamento, opcionalmente já grava um equipamento
            # (append, não substitui os que já existem -- diferente da
            # finalização, aqui não existe "lista completa" ainda). O que já
            # foi gravado aqui aparece pré-preenchido na tela de finalizar.
            item = body.andamento_equip_item
            if body.andamento_equip_tipo in ('instalado', 'removido', 'reconfigurado') and item and \
               (item.partnumber.strip() or item.serial.strip()):
                posse = item.posse if (body.andamento_equip_tipo == 'removido' and item.posse in ('tecnico', 'cliente')) else None
                cur.execute(
                    "INSERT INTO status_atividade_equipamentos (atividade_id, tipo, partnumber, serial, posse) VALUES (%s,%s,%s,%s,%s)",
                    (aid, body.andamento_equip_tipo, item.partnumber.strip(), item.serial.strip(), posse))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/status-campo/{aid}/andamento")
def listar_andamentos(aid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, localizacao, acesso, descricao, criado_por_nome, criado_em,
                   hora_chegada, hora_inicio_atividade, andamento_tipo, andamento_equipamento
            FROM status_atividade_andamentos WHERE atividade_id=%s ORDER BY criado_em ASC
        """, (aid,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "localizacao": r[1], "acesso": r[2], "descricao": r[3],
                 "criado_por_nome": r[4], "criado_em": str(r[5])[:16],
                 "hora_chegada": str(r[6])[:5] if r[6] else None,
                 "hora_inicio_atividade": str(r[7])[:5] if r[7] else None,
                 "andamento_tipo": r[8], "andamento_equipamento": r[9]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/status-campo/{aid}")
def deletar_status_campo(aid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or (sess["perfil"] not in ("admin", "gestor", "demo", "diretor") and not _eh_n2(sess)): raise HTTPException(status_code=403)
    if not _pode_ver_status_report(sess): raise HTTPException(status_code=403, detail="Status Report é restrito ao time de Projetos")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        if not _pode_gerenciar_status_campo(sess, conn, aid):
            raise HTTPException(status_code=403, detail="Você só pode excluir atividades onde é o N2 responsável")
        cur = conn.cursor()
        cur.execute("DELETE FROM status_atividades WHERE id=%s", (aid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/status-campo/usuarios/n2")
def listar_usuarios_n2(faiston_token: str = Cookie(None)):
    """Dropdown de N2 responsável -- restrito a cargo='n2' (2026-07-28, a
    pedido do usuário). Antes listava qualquer usuário ativo (a ideia era
    permitir admin/gestor atuando como N2 em campo também), mas na prática
    isso deixava a lista poluída com quem não é N2 de verdade."""
    sess = get_session(faiston_token)
    _exigir_status_report(sess)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, perfil FROM usuarios WHERE ativo=TRUE AND perfil='funcionario' AND cargo='n2' ORDER BY nome")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1], "perfil": r[2]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
