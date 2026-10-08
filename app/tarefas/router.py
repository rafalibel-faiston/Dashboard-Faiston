"""Tarefas: CRUD, timer no servidor e histórico de alterações.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import datetime
from typing import List, Optional
import logging

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.acesso import _exigir_area_com_projeto
from app.core.agenda import _bloqueio_ativo, _hoje_sp
from app.core.auth import get_session
from app.core.db import get_db
from app.notificacoes.router import criar_notificacao

logger = logging.getLogger("faiston")


router = APIRouter()


class TarefaModel(BaseModel):
    descricao: str
    cliente: str
    prioridade: str = "Media"
    status: str = "aberto"
    segundos: int = 0
    funcionario_id: Optional[int] = None
    projeto_id: Optional[int] = None
    data_prazo: Optional[str] = None
    data_agendamento: Optional[str] = None
    hora_prazo: Optional[str] = None
    tipo_atividade_id: Optional[int] = None
    natureza: Optional[str] = None
    justificativa_atraso: Optional[str] = None
    colaboradores: Optional[List[int]] = None


class AtualizarSegundos(BaseModel):
    segundos: int


# --- HISTÓRICO DE TAREFAS (resumo diário) ---
HIST_STATUS_LABEL = {"aberto": "Aberto", "em_andamento": "Em andamento",
                     "concluido": "Concluído", "revisao": "Em revisão"}
# Campos rastreados: (atributo, rótulo amigável). Ordem define exibição.
HIST_CAMPOS = [
    ("descricao",         "Descrição"),
    ("cliente",           "Cliente"),
    ("status",            "Status"),
    ("prioridade",        "Prioridade"),
    ("projeto_nome",      "Projeto"),
    ("data_prazo",        "Prazo"),
    ("hora_prazo",        "Horário do prazo"),
    ("data_agendamento",  "Agendamento"),
]

def _hist_fmt(campo: str, valor) -> str:
    """Formata um valor para exibição amigável no resumo."""
    if valor is None or valor == "":
        return "—"
    v = str(valor)
    if campo == "status":
        return HIST_STATUS_LABEL.get(v, v)
    if campo in ("data_prazo", "data_agendamento") and len(v) >= 10:
        d = v[:10]
        return f"{d[8:10]}/{d[5:7]}/{d[0:4]}"
    if campo == "hora_prazo":
        return v[:5]
    return v

def _snapshot_tarefa(cur, tid: int) -> dict:
    """Lê o estado atual de uma tarefa + nome do projeto/time do dono."""
    cur.execute("""SELECT t.descricao, t.cliente, t.status, t.prioridade,
                          COALESCE(p.nome,''), t.data_prazo, t.hora_prazo, t.data_agendamento,
                          t.projeto_id, COALESCE(u.time,'Projetos')
                   FROM tarefas t
                   LEFT JOIN projetos p ON p.id = t.projeto_id
                   LEFT JOIN usuarios u ON u.id = t.usuario_id
                   WHERE t.id = %s""", (tid,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"descricao": r[0], "cliente": r[1], "status": r[2], "prioridade": r[3],
            "projeto_nome": r[4], "data_prazo": str(r[5]) if r[5] else "",
            "hora_prazo": str(r[6])[:5] if r[6] else "", "data_agendamento": str(r[7]) if r[7] else "",
            "projeto_id": r[8], "time": r[9]}

def registrar_historico(conn, sess, tid: int, acao: str, mudancas: list, snap: dict):
    """Grava no histórico. `mudancas` = lista de (campo, antigo, novo).
    Usa SAVEPOINT para nunca abortar a transação principal."""
    if not mudancas:
        return
    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT sp_hist")
        for campo, antigo, novo in mudancas:
            cur.execute("""INSERT INTO tarefa_historico
                (tarefa_id, tarefa_desc, autor_id, autor_nome, acao, campo,
                 valor_antigo, valor_novo, projeto_id, projeto_nome, time_tarefa)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tid, snap.get("descricao", ""), sess["id"], sess["nome"], acao, campo,
                 (str(antigo) if antigo not in (None, "") else None),
                 (str(novo) if novo not in (None, "") else None),
                 snap.get("projeto_id"), snap.get("projeto_nome", ""),
                 snap.get("time", "Projetos")))
        cur.execute("RELEASE SAVEPOINT sp_hist")
    except Exception as e:
        try: cur.execute("ROLLBACK TO SAVEPOINT sp_hist")
        except Exception: pass
        print(f"[historico] falha ao registrar: {e}")
    finally:
        cur.close()

# --- TIMER DAS TAREFAS (no servidor) ---
# Teto de segundos por tarefa (10.000h) -- o mesmo MAX_SEGUNDOS do front, evita
# overflow do INTEGER com valor corrompido.
MAX_SEGUNDOS = 36_000_000
# Tolerância pra cliente antigo que devolve no PUT os segundos que leu: se o
# valor enviado bate com o total atual (± isso), não é edição manual de horas,
# e o timer que está rodando não é zerado.
_TOLERANCIA_ECO_SEGUNDOS = 120

def _sql_decorrido(prefixo: str = "") -> str:
    """Segundos inteiros desde que o timer foi iniciado (0 se pausado)."""
    ti = f"{prefixo}timer_inicio"
    return f"COALESCE(GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (NOW() - {ti}))))::int, 0)"

def _estado_timer(cur, tid: int) -> dict:
    """Estado da tarefa depois de uma ação de timer/status, pro front
    sincronizar sem precisar recarregar a lista inteira."""
    cur.execute(f"""SELECT status, segundos, timer_inicio IS NOT NULL, {_sql_decorrido()}, concluido_em
                    FROM tarefas WHERE id=%s""", (tid,))
    r = cur.fetchone()
    if not r:
        return {"id": tid}
    return {"id": tid, "status": r[0], "segundos": r[1] or 0, "timer_ativo": bool(r[2]),
            "timer_decorrido": r[3] if r[2] else 0,
            "concluido_em": str(r[4]) if r[4] else None}

def _tarefa_do_usuario_para_update(cur, tid: int, usuario_id: int):
    """Trava a linha da tarefa (FOR UPDATE) e devolve (status, segundos,
    timer_rodando, decorrido). 404 se a tarefa não existe ou não é do usuário --
    antes o UPDATE simplesmente não achava a linha e o endpoint respondia
    'sucesso', a tela mostrava a tarefa concluída e ela 'voltava' no reload."""
    cur.execute(f"""SELECT status, segundos, timer_inicio IS NOT NULL, {_sql_decorrido()}
                    FROM tarefas WHERE id=%s AND usuario_id=%s FOR UPDATE""", (tid, usuario_id))
    r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada, ou você não é o responsável por ela. Recarregue a página.")
    return r[0], r[1] or 0, bool(r[2]), r[3] or 0

def consolidar_timers():
    """Job de 1 em 1 minuto: passa o tempo dos timers rodando pra `segundos`
    (e anda o timer_inicio junto, então o total não muda). Mantém métricas,
    relatórios e o quadro do gestor em dia mesmo com a aba do funcionário
    fechada. Cada linha é recalculada com os próprios valores atuais, então
    uma pausa concorrente nunca conta tempo em dobro."""
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE tarefas
               SET segundos = LEAST(COALESCE(segundos, 0) + {_sql_decorrido()}, {MAX_SEGUNDOS}),
                   timer_inicio = timer_inicio + {_sql_decorrido()} * INTERVAL '1 second'
             WHERE timer_inicio IS NOT NULL AND timer_inicio <= NOW() - INTERVAL '1 second'
        """)
        conn.commit(); cur.close()
    except Exception as e:
        logger.error(f"[timer] falha ao consolidar timers: {e}")
    finally:
        conn.close()

# --- TAREFAS ---
@router.get("/api/tarefas")
def listar_tarefas(view: str = "", faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # As colunas/tabela que eram criadas aqui ("migração silenciosa") já
        # vêm do setup_banco. ALTER TABLE pega lock exclusivo na tabela tarefas
        # mesmo com IF NOT EXISTS; rodando a cada listagem (várias por minuto,
        # por usuário), qualquer transação mais lenta enfileirava o sistema
        # inteiro atrás dele -- era a causa do "o sistema trava e não faz mais nada".
        base_sel = f"""SELECT t.id, t.descricao, t.cliente, t.prioridade, t.status, t.segundos,
                             t.criado_em, u.nome, t.projeto_id, COALESCE(p.nome,'') AS projeto_nome,
                             t.data_prazo, t.data_agendamento, t.usuario_id, t.hora_prazo,
                             t.tipo_atividade_id, t.peso, t.natureza,
                             t.concluido_em, t.prazo_status, t.justificativa_atraso,
                             t.timer_inicio IS NOT NULL, {_sql_decorrido('t.')}
                      FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
                      LEFT JOIN projetos p ON p.id = t.projeto_id"""
        if view == "func":
            # Quadro pessoal (funcionario.html / embed do gestor): qualquer perfil vê
            # apenas as próprias tarefas + aquelas em que é colaborador (ajudando).
            cur.execute(base_sel + """ WHERE t.usuario_id = %s
                OR t.id IN (SELECT tarefa_id FROM tarefa_colaboradores WHERE usuario_id = %s)
                ORDER BY t.criado_em DESC""", (sess["id"], sess["id"]))
        elif sess["perfil"] == "admin":
            cur.execute(base_sel + " ORDER BY t.criado_em DESC")
        elif sess["perfil"] in ("gestor", "demo"):
            cur.execute(base_sel + " WHERE COALESCE(u.time,'Projetos')=%s ORDER BY t.criado_em DESC", (sess.get("time","Projetos"),))
        else:
            # Funcionário vê as próprias tarefas + aquelas em que é colaborador (ajudando)
            cur.execute(base_sel + """ WHERE t.usuario_id = %s
                OR t.id IN (SELECT tarefa_id FROM tarefa_colaboradores WHERE usuario_id = %s)
                ORDER BY t.criado_em DESC""", (sess["id"], sess["id"]))
        rows = cur.fetchall()

        # Colaboradores por tarefa
        ids = [r[0] for r in rows]
        colab_map = {}
        if ids:
            cur.execute("""SELECT tc.tarefa_id, u.id, u.nome
                           FROM tarefa_colaboradores tc JOIN usuarios u ON u.id = tc.usuario_id
                           WHERE tc.tarefa_id = ANY(%s) ORDER BY u.nome""", (ids,))
            for tid_, uid_, unome_ in cur.fetchall():
                colab_map.setdefault(tid_, []).append({"id": uid_, "nome": unome_})
        cur.close(); conn.close()
        return [{"id": r[0], "descricao": r[1], "cliente": r[2], "prioridade": r[3],
                 "status": r[4], "segundos": r[5], "criado_em": str(r[6]), "funcionario": r[7],
                 "projeto_id": r[8], "projeto_nome": r[9],
                 "data_prazo": str(r[10]) if r[10] else None,
                 "data_agendamento": str(r[11]) if r[11] else None,
                 "usuario_id": r[12],
                 "hora_prazo": str(r[13])[:5] if r[13] else None,
                "tipo_atividade_id": r[14], "peso": r[15], "natureza": r[16],
                 "concluido_em": str(r[17]) if r[17] else None,
                 "prazo_status": r[18], "justificativa_atraso": r[19],
                 "timer_ativo": bool(r[20]), "timer_decorrido": r[21] if r[20] else 0,
                 "sou_colaborador": (r[12] != sess["id"]) and any(c["id"] == sess["id"] for c in colab_map.get(r[0], [])),
                 "colaboradores": colab_map.get(r[0], [])} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _sync_colaboradores(cur, tarefa_id: int, owner_id: int, colaboradores) -> list:
    """Substitui a lista de colaboradores. Retorna IDs dos novos colaboradores adicionados."""
    cur.execute("SELECT usuario_id FROM tarefa_colaboradores WHERE tarefa_id = %s", (tarefa_id,))
    existing = {r[0] for r in cur.fetchall()}
    cur.execute("DELETE FROM tarefa_colaboradores WHERE tarefa_id = %s", (tarefa_id,))
    ids = [int(c) for c in (colaboradores or []) if int(c) != owner_id]
    novos = []
    for uid in dict.fromkeys(ids):  # remove duplicados preservando ordem
        cur.execute("SELECT id FROM usuarios WHERE id=%s AND ativo=TRUE", (uid,))
        if cur.fetchone():
            cur.execute("INSERT INTO tarefa_colaboradores (tarefa_id, usuario_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (tarefa_id, uid))
            if uid not in existing:
                novos.append(uid)
    return novos

@router.post("/api/tarefas")
def criar_tarefa(t: TarefaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Gestor/admin pode atribuir a outro funcionário via funcionario_id
        uid = sess["id"]
        if t.funcionario_id and sess["perfil"] in ("admin", "gestor", "demo"):
            cur.execute("SELECT id FROM usuarios WHERE id=%s AND ativo=TRUE", (t.funcionario_id,))
            if cur.fetchone():
                uid = t.funcionario_id
        # Analista pode atribuir tarefa a um assistente de Backoffice do mesmo
        # time (2026-08-17) -- só nessa direção, não pra qualquer funcionário.
        elif (t.funcionario_id and t.funcionario_id != sess["id"]
              and sess["perfil"] == "funcionario" and sess.get("cargo") == "analista"):
            cur.execute("""
                SELECT id FROM usuarios WHERE id=%s AND ativo=TRUE AND perfil='funcionario'
                  AND cargo='backoffice' AND COALESCE(time,'Projetos')=%s
            """, (t.funcionario_id, sess.get("time", "Projetos")))
            if cur.fetchone():
                uid = t.funcionario_id
        data_checar = t.data_agendamento or t.data_prazo
        bloqueio = _bloqueio_ativo(cur, uid, data_checar, t.hora_prazo)
        if bloqueio:
            raise HTTPException(status_code=400, detail=f"Funcionário indisponível nesta data: {bloqueio}")
        # Área sem projeto (cadastro em /api/areas): a tarefa é sempre demanda
        # avulsa. Olha o time do DONO da tarefa (uid), não o de quem cria --
        # gestor de outra área atribuindo tarefa cai na mesma regra.
        if t.projeto_id:
            cur.execute("SELECT COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (uid,))
            row_area = cur.fetchone()
            if row_area:
                _exigir_area_com_projeto(cur, row_area[0])
        # 2A: campo ainda opcional. Quando vier preenchido, o peso da régua é
        # copiado pra tarefa. A obrigatoriedade entra no 2B, junto com o campo
        # no modal -- senão o frontend antigo pararia de criar tarefa.
        if not t.tipo_atividade_id:
            raise HTTPException(status_code=400, detail="Informe o tipo de atividade")
        if not t.data_prazo:
            raise HTTPException(status_code=400, detail="Informe a previsão de conclusão")
        peso = None
        if t.tipo_atividade_id:
            cur.execute("SELECT peso FROM tipos_atividade WHERE id=%s AND ativo=TRUE", (t.tipo_atividade_id,))
            row_tipo = cur.fetchone()
            if not row_tipo:
                raise HTTPException(status_code=400, detail="Tipo de atividade inválido ou desativado")
            peso = row_tipo[0]
        natureza = t.natureza if t.natureza in ("programada", "urgente") else "programada"
        cur.execute(
                "INSERT INTO tarefas (usuario_id, descricao, cliente, prioridade, status, segundos, projeto_id, data_prazo, data_agendamento, hora_prazo, tipo_atividade_id, peso, natureza, timer_inicio) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN NOW() END) RETURNING id",
            (uid, t.descricao, t.cliente, t.prioridade, t.status, max(0, min(t.segundos or 0, MAX_SEGUNDOS)), t.projeto_id or None,
             t.data_prazo or None, t.data_agendamento or None, t.hora_prazo or None,
             t.tipo_atividade_id or None, peso, natureza,
             t.status == "em_andamento")   # já nasce em andamento: timer ligado
        )
        new_id = cur.fetchone()[0]
        novos_helpers = _sync_colaboradores(cur, new_id, uid, t.colaboradores or [])
        snap_new = _snapshot_tarefa(cur, new_id)
        registrar_historico(conn, sess, new_id, "criou",
                            [("tarefa", None, t.descricao)], snap_new)
        criar_notificacao(conn, "nova_tarefa", f"🆕 {sess['nome']} criou uma tarefa: {t.descricao[:50]} [{t.cliente}]", sess["id"])
        for hid in novos_helpers:
            criar_notificacao(conn, "ajudante_adicionado",
                f"🤝 {sess['nome']} adicionou você para ajudar em: {t.descricao[:50]} [{t.cliente}]",
                sess["id"], destinatario_id=hid)
        estado = _estado_timer(cur, new_id)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/tarefas/{tid}")
def atualizar_tarefa(tid: int, t: TarefaModel, faiston_token: str = Cookie(None)):
    """Atualiza SÓ os campos que vieram no corpo. Antes o UPDATE gravava todos:
    ações rápidas do quadro (concluir, arrastar, iniciar timer) mandam só
    descrição/cliente/status, e o resto ia como NULL -- a tarefa perdia o
    projeto e o horário do prazo e 'sumia' das visões por projeto."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        enviados = t.model_fields_set
        status_antigo, seg_antigo, rodando, decorrido = _tarefa_do_usuario_para_update(cur, tid, sess["id"])
        snap_old = _snapshot_tarefa(cur, tid)
        status_novo = t.status if "status" in enviados else status_antigo
        # Mesma regra da criação -- o UPDATE abaixo é escopado em usuario_id =
        # sess["id"], então o dono da tarefa é quem está editando.
        if t.projeto_id:
            _exigir_area_com_projeto(cur, sess.get("time", "Projetos"))
        peso_upd = None
        if t.tipo_atividade_id:
            cur.execute("SELECT peso FROM tipos_atividade WHERE id=%s AND ativo=TRUE", (t.tipo_atividade_id,))
            row_tipo = cur.fetchone()
            if not row_tipo:
                raise HTTPException(status_code=400, detail="Tipo de atividade inválido ou desativado")
            peso_upd = row_tipo[0]
        natureza_upd = t.natureza if t.natureza in ("programada", "urgente") else None
        # Só na transição para 'concluido' -- reeditar tarefa já concluída não
        # recalcula, senão o histórico mudaria sozinho. Comparação por DATA:
        # concluir no dia previsto conta como dentro do prazo.
        concluindo = status_novo == "concluido" and status_antigo != "concluido"
        iniciando = status_novo == "em_andamento" and status_antigo != "em_andamento"
        prazo_status = None
        justificativa = (t.justificativa_atraso or "").strip()
        if concluindo:
            prazo_ref = (t.data_prazo if "data_prazo" in enviados else None) or snap_old.get("data_prazo") or ""
            if prazo_ref:
                prazo_d = datetime.strptime(str(prazo_ref)[:10], "%Y-%m-%d").date()
                prazo_status = "dentro" if _hoje_sp() <= prazo_d else "fora"
            else:
                prazo_status = "sem_prazo"
            if prazo_status == "fora" and not justificativa:
                    raise HTTPException(status_code=400, detail="Tarefa concluída fora do prazo: informe a justificativa do atraso")

        sets, params = [], []
        for campo in ("descricao", "cliente", "prioridade", "status"):
            if campo in enviados:
                sets.append(f"{campo}=%s"); params.append(getattr(t, campo))
        for campo in ("projeto_id", "data_prazo", "data_agendamento", "hora_prazo"):
            if campo in enviados:
                sets.append(f"{campo}=%s"); params.append(getattr(t, campo) or None)
        if t.tipo_atividade_id:
            sets.append("tipo_atividade_id=%s"); params.append(t.tipo_atividade_id)
            sets.append("peso=%s"); params.append(peso_upd)
        if natureza_upd:
            sets.append("natureza=%s"); params.append(natureza_upd)

        # Timer: `segundos` só é gravado quando é edição manual de horas. Um
        # cliente que apenas devolve o valor que leu (quadro antigo, tela do
        # N2) não pode zerar o tempo que o timer contou nesse meio-tempo.
        edita_segundos = "segundos" in enviados
        if edita_segundos and rodando and abs((t.segundos or 0) - (seg_antigo + decorrido)) <= _TOLERANCIA_ECO_SEGUNDOS:
            edita_segundos = False
        if edita_segundos:
            sets.append("segundos=%s"); params.append(max(0, min(t.segundos or 0, MAX_SEGUNDOS)))
        if rodando and status_novo != "em_andamento":
            # Saiu de 'em andamento' (concluiu, voltou pra aberto): fecha o timer.
            if not edita_segundos:
                sets.append(f"segundos=LEAST(COALESCE(segundos,0) + {_sql_decorrido()}, {MAX_SEGUNDOS})")
            sets.append("timer_inicio=NULL")
        elif rodando and edita_segundos:
            sets.append("timer_inicio=NOW()")   # horas corrigidas à mão: conta a partir delas
        elif not rodando and iniciando:
            sets.append("timer_inicio=NOW()")   # entrou em 'em andamento': começa a contar
        sets.append("atualizado_em=NOW()")
        cur.execute(f"UPDATE tarefas SET {', '.join(sets)} WHERE id=%s", params + [tid])
        if concluindo:
            cur.execute("UPDATE tarefas SET concluido_em=NOW(), prazo_status=%s, justificativa_atraso=%s WHERE id=%s",
                        (prazo_status, justificativa, tid))
        # Registra no histórico cada campo que mudou (compara antes × depois)
        if snap_old:
            snap_new = _snapshot_tarefa(cur, tid)
            mudancas = []
            for campo, _label in HIST_CAMPOS:
                antes, depois = snap_old.get(campo, ""), snap_new.get(campo, "")
                if str(antes or "") != str(depois or ""):
                    mudancas.append((campo, antes, depois))
            registrar_historico(conn, sess, tid, "editou", mudancas, snap_new)
        # Só o dono atualiza colaboradores, e apenas quando a lista é enviada explicitamente
        novos_helpers = []
        if t.colaboradores is not None:
            novos_helpers = _sync_colaboradores(cur, tid, sess["id"], t.colaboradores)
        if concluindo:
            criar_notificacao(conn, "tarefa_concluida", f"✅ {sess['nome']} concluiu: {t.descricao[:50]} [{t.cliente}]", sess["id"])
        elif iniciando:
            criar_notificacao(conn, "tarefa_iniciada", f"▶️ {sess['nome']} iniciou: {t.descricao[:50]} [{t.cliente}]", sess["id"])
        for hid in novos_helpers:
            criar_notificacao(conn, "ajudante_adicionado",
                f"🤝 {sess['nome']} adicionou você para ajudar em: {t.descricao[:50]} [{t.cliente}]",
                sess["id"], destinatario_id=hid)
        estado = _estado_timer(cur, tid)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tarefas/{tid}/timer/iniciar")
def iniciar_timer_tarefa(tid: int, faiston_token: str = Cookie(None)):
    """Liga o timer desta tarefa sem mexer nos timers das outras (várias
    tarefas podem contar tempo ao mesmo tempo). Idempotente: clicar duas
    vezes, ou repetir a chamada depois de uma falha de rede, não reinicia
    a contagem."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        status_antigo, _seg, _rodando, _dec = _tarefa_do_usuario_para_update(cur, tid, sess["id"])
        if status_antigo == "concluido":
            raise HTTPException(status_code=400, detail="Tarefa já concluída. Reabra a tarefa antes de iniciar o timer.")
        snap_old = _snapshot_tarefa(cur, tid)
        cur.execute("""UPDATE tarefas SET timer_inicio = COALESCE(timer_inicio, NOW()),
                              status='em_andamento', atualizado_em=NOW() WHERE id=%s""", (tid,))
        if status_antigo != "em_andamento" and snap_old:
            snap_new = _snapshot_tarefa(cur, tid)
            registrar_historico(conn, sess, tid, "editou", [("status", status_antigo, "em_andamento")], snap_new)
            criar_notificacao(conn, "tarefa_iniciada",
                f"▶️ {sess['nome']} iniciou: {(snap_old.get('descricao') or '')[:50]} [{snap_old.get('cliente') or ''}]", sess["id"])
        estado = _estado_timer(cur, tid)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tarefas/{tid}/timer/pausar")
def pausar_timer_tarefa(tid: int, faiston_token: str = Cookie(None)):
    """Pausa o timer desta tarefa e soma a sessão em `segundos`. A tarefa
    continua 'em andamento'. Idempotente: pausar o que já está pausado é no-op."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        _tarefa_do_usuario_para_update(cur, tid, sess["id"])
        cur.execute(f"""UPDATE tarefas
                           SET segundos = LEAST(COALESCE(segundos,0) + {_sql_decorrido()}, {MAX_SEGUNDOS}),
                               timer_inicio = NULL, atualizado_em = NOW()
                         WHERE id=%s AND timer_inicio IS NOT NULL""", (tid,))
        estado = _estado_timer(cur, tid)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, **estado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.patch("/api/tarefas/{tid}/segundos")
def atualizar_segundos(tid: int, body: AtualizarSegundos, faiston_token: str = Cookie(None)):
    """Legado: era o 'checkpoint' do timer que rodava no navegador (a cada 30s
    e ao fechar a aba). O quadro novo não usa mais -- fica pra aba antiga
    ainda aberta. Nunca diminui o tempo já contado, e se o timer está rodando
    no servidor ele continua a partir do valor gravado."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        seg = max(0, min(body.segundos or 0, MAX_SEGUNDOS))
        cur.execute(f"""UPDATE tarefas
                           SET segundos = GREATEST(%s, COALESCE(segundos,0) + {_sql_decorrido()}),
                               timer_inicio = CASE WHEN timer_inicio IS NULL THEN NULL ELSE NOW() END,
                               atualizado_em = NOW()
                         WHERE id=%s AND usuario_id=%s""", (seg, tid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/tarefas/{tid}")
def deletar_tarefa(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        snap = _snapshot_tarefa(cur, tid)
        if sess["perfil"] in ("admin", "gestor", "demo"):
            cur.execute("DELETE FROM tarefas WHERE id=%s", (tid,))
        else:
            cur.execute("DELETE FROM tarefas WHERE id=%s AND usuario_id=%s", (tid, sess["id"]))
        if cur.rowcount and snap:
            registrar_historico(conn, sess, tid, "excluiu",
                                [("tarefa", snap.get("descricao"), None)], snap)
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
