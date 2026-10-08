"""Usuários e bloqueios de agenda.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import date
from typing import Optional
import secrets

from fastapi import APIRouter, BackgroundTasks, Cookie, HTTPException, Request
from pydantic import BaseModel
import psycopg2

from app.autenticacao.router import _gerar_token_redefinicao
from app.core.acesso import CARGO_VALIDOS, PERFIL_VALIDOS, _area_cargos_cur, _area_perfis_cur, _perfil_guia, times_validos
from app.core.auth import _senha_fraca, get_session, hash_senha
from app.core.db import get_db
from app.core.email import _resolver_system_url, enviar_email_acesso, enviar_email_boas_vindas

# --- corpo ---
router = APIRouter()


class NovoUsuario(BaseModel):
    usuario: str
    nome: str
    perfil: str
    email: str  # obrigatório (2026-08-10): é por ele que o acesso é enviado, não tem mais senha definida pelo admin
    time: str = "Projetos"
    cargo: str = ""  # só se aplica quando perfil == 'funcionario'


class AtualizarUsuario(BaseModel):
    nome: str
    perfil: str
    senha: str = ""
    ativo: bool = True
    email: str = ""
    time: str = "Projetos"
    cargo: str = ""  # só se aplica quando perfil == 'funcionario'


# --- USUÁRIOS ---
def _area_atual(sess: dict) -> str:
    """Área de quem está logado lida do cadastro, não da cópia gravada na
    sessão no login: renomear a área (Admin → Áreas) ou mover a pessoa por
    outro caminho não atualiza `sessoes`, e aí o gestor via/gravava a equipe
    da área antiga até deslogar."""
    fallback = sess.get("time") or "Projetos"
    conn = get_db()
    if not conn: return fallback
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (sess["id"],))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else fallback
    finally:
        conn.close()

@router.get("/api/usuarios")
def listar_usuarios(escopo: str = "", faiston_token: str = Cookie(None)):
    """Admin vê todo mundo (Admin → Usuários). Gestor vê só a própria área.
    `escopo=minha_area` (tela Minha Equipe) restringe à área de quem está
    logado para qualquer perfil -- inclusive admin/dev, que antes viam a
    empresa inteira na "Minha Equipe"."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] == "admin" and escopo != "minha_area":
            cur.execute("SELECT id, usuario, nome, perfil, ativo, criado_em, COALESCE(email,''), COALESCE(time,'Projetos'), COALESCE(primeiro_acesso, FALSE), ultimo_acesso, COALESCE(cargo,'') FROM usuarios WHERE ativo=TRUE ORDER BY criado_em DESC")
        else:
            cur.execute("SELECT id, usuario, nome, perfil, ativo, criado_em, COALESCE(email,''), COALESCE(time,'Projetos'), COALESCE(primeiro_acesso, FALSE), ultimo_acesso, COALESCE(cargo,'') FROM usuarios WHERE ativo=TRUE AND COALESCE(time,'Projetos')=%s ORDER BY criado_em DESC", (_area_atual(sess),))
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "usuario": r[1], "nome": r[2], "perfil": r[3], "ativo": r[4], "criado_em": str(r[5]), "email": r[6], "time": r[7],
                 "primeiro_acesso": bool(r[8]), "ultimo_acesso": str(r[9])[:19] if r[9] else None, "cargo": r[10]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/funcionarios")
def listar_funcionarios(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] == "admin":
            cur.execute("SELECT id, nome, COALESCE(cargo,'') FROM usuarios WHERE perfil='funcionario' AND ativo=TRUE ORDER BY nome")
        else:
            cur.execute("SELECT id, nome, COALESCE(cargo,'') FROM usuarios WHERE perfil='funcionario' AND ativo=TRUE AND COALESCE(time,'Projetos')=%s ORDER BY nome", (sess.get("time","Projetos"),))
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id": r[0], "nome": r[1], "cargo": r[2]} for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/usuarios")
def criar_usuario(u: NovoUsuario, bg: BackgroundTasks, request: Request, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    is_gestor = sess["perfil"] in ("gestor", "demo")
    if is_gestor and u.perfil not in ("funcionario", "demo"):
        raise HTTPException(status_code=403, detail="Gestores só podem criar funcionários")
    if u.perfil not in PERFIL_VALIDOS: raise HTTPException(status_code=400, detail="Perfil inválido")
    if not (u.email or "").strip(): raise HTTPException(status_code=400, detail="Email é obrigatório — é por ele que a pessoa recebe o acesso.")
    time_val = _area_atual(sess) if is_gestor else (u.time if u.time in times_validos() else "Projetos")
    cargo_val = u.cargo if (u.perfil == "funcionario" and u.cargo in CARGO_VALIDOS) else ""
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        # Cargo e perfil habilitados por área (cadastro em /api/areas): o admin
        # liga em cada área só o que faz sentido nela.
        if u.perfil not in _area_perfis_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O perfil {u.perfil} não está habilitado na área {time_val}")
        if cargo_val and cargo_val not in _area_cargos_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O cargo {cargo_val} não está habilitado na área {time_val}")
        # Sem senha definida pelo admin (2026-08-10): a coluna senha_hash ainda
        # não aceita NULL, então preenche com uma senha aleatória que nunca é
        # exibida nem enviada -- a pessoa define a própria senha pelo link de
        # "definir senha" abaixo, reaproveitando o token do "esqueci minha senha".
        senha_temp = secrets.token_urlsafe(24)
        cur.execute("INSERT INTO usuarios (usuario, senha_hash, nome, perfil, email, primeiro_acesso, time, cargo) VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s) RETURNING id",
                    (u.usuario, hash_senha(senha_temp), u.nome, u.perfil, u.email, time_val, cargo_val))
        new_id = cur.fetchone()[0]
        token = _gerar_token_redefinicao(cur, new_id)
        conn.commit(); cur.close(); conn.close()
        system_url = _resolver_system_url(request)
        link = f"{system_url}/redefinir-senha?token={token}"
        bg.add_task(enviar_email_boas_vindas, u.email, u.nome, link)
        return {"sucesso": True, "id": new_id, "email_enviado": True}
    except psycopg2.errors.UniqueViolation: raise HTTPException(status_code=400, detail="Usuário já existe")
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/usuarios/{uid}")
def atualizar_usuario(uid: int, u: AtualizarUsuario, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    is_gestor = sess["perfil"] in ("gestor", "demo")
    if is_gestor:
        conn2 = get_db()
        if not conn2: raise HTTPException(status_code=500, detail="Banco offline")
        cur2 = conn2.cursor()
        cur2.execute("SELECT COALESCE(time,'Projetos'), perfil FROM usuarios WHERE id=%s", (uid,))
        row = cur2.fetchone(); cur2.close(); conn2.close()
        if not row or row[0] != _area_atual(sess):
            raise HTTPException(status_code=403, detail="Acesso negado — usuário não pertence ao seu time")
        if row[1] in ("admin", "gestor", "dev"):
            raise HTTPException(status_code=403, detail="Não é possível editar admins ou gestores")
        if u.perfil not in ("funcionario", "demo"):
            raise HTTPException(status_code=403, detail="Gestores só podem definir perfil funcionário/demo")
    if u.senha:
        erro = _senha_fraca(u.senha)
        if erro: raise HTTPException(status_code=400, detail=erro)
    time_val = _area_atual(sess) if is_gestor else (u.time if u.time in times_validos() else "Projetos")
    cargo_val = u.cargo if (u.perfil == "funcionario" and u.cargo in CARGO_VALIDOS) else ""
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT perfil, COALESCE(cargo,''), COALESCE(time,'Projetos') FROM usuarios WHERE id=%s", (uid,))
        atual = cur.fetchone()
        if not atual: raise HTTPException(status_code=404, detail="Usuário não encontrado")
        # Só checa o que mudou: alguém cadastrado antes de a área restringir
        # cargo/perfil continua editável (mudar o nome não pode dar 400) -- mas
        # trocar o perfil, o cargo ou a área passa pela regra da área de destino.
        if (u.perfil, time_val) != (atual[0], atual[2]) and u.perfil not in _area_perfis_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O perfil {u.perfil} não está habilitado na área {time_val}")
        if cargo_val and (cargo_val, time_val) != (atual[1], atual[2]) \
                and cargo_val not in _area_cargos_cur(cur, time_val):
            raise HTTPException(status_code=400,
                                detail=f"O cargo {cargo_val} não está habilitado na área {time_val}")
        if u.senha:
            cur.execute("UPDATE usuarios SET nome=%s, perfil=%s, ativo=%s, email=%s, senha_hash=%s, primeiro_acesso=TRUE, time=%s, cargo=%s WHERE id=%s",
                        (u.nome, u.perfil, u.ativo, u.email, hash_senha(u.senha), time_val, cargo_val, uid))
        else:
            cur.execute("UPDATE usuarios SET nome=%s, perfil=%s, ativo=%s, email=%s, time=%s, cargo=%s WHERE id=%s",
                        (u.nome, u.perfil, u.ativo, u.email, time_val, cargo_val, uid))
        # sessoes guarda uma cópia de nome/perfil/time/cargo tirada no login.
        # get_session lê do cadastro desde 2026-10-08, mas a cópia ainda
        # aparece na lista de "quem está online" -- mantém em dia.
        cur.execute("UPDATE sessoes SET nome=%s, perfil=%s, time_usuario=%s, cargo=%s WHERE usuario_id=%s",
                    (u.nome, u.perfil, time_val, cargo_val, uid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/usuarios/{uid}/reenviar-email")
def reenviar_email_acesso(uid: int, request: Request, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome, usuario, email, perfil, COALESCE(cargo,''), primeiro_acesso FROM usuarios WHERE id=%s AND ativo=TRUE", (uid,))
        row = cur.fetchone()
        if not row: cur.close(); conn.close(); raise HTTPException(status_code=404, detail="Usuário não encontrado")
        nome, usuario, email, perfil_u, cargo_u, primeiro_acesso = row
        if not email: cur.close(); conn.close(); raise HTTPException(status_code=400, detail="Este usuário não tem email cadastrado")
        if primeiro_acesso:
            # Nunca definiu a própria senha ainda -- reenvia o link de definição
            # (token novo), não o email antigo de "use a senha que já tem".
            token = _gerar_token_redefinicao(cur, uid)
            conn.commit(); cur.close(); conn.close()
            system_url = _resolver_system_url(request)
            link = f"{system_url}/redefinir-senha?token={token}"
            enviado = enviar_email_boas_vindas(email, nome, link)
        else:
            cur.close(); conn.close()
            enviado = enviar_email_acesso(email, nome, usuario, None, _perfil_guia(perfil_u, cargo_u), _resolver_system_url(request))
        if not enviado: raise HTTPException(status_code=500, detail="Falha ao enviar email — verifique as variáveis EMAIL_USER e EMAIL_APP_PASSWORD no servidor")
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/usuarios/{uid}")
def deletar_usuario(uid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403, detail="Acesso negado")
    if sess["perfil"] in ("gestor", "demo"):
        conn2 = get_db()
        if not conn2: raise HTTPException(status_code=500, detail="Banco offline")
        cur2 = conn2.cursor()
        cur2.execute("SELECT COALESCE(time,'Projetos'), perfil FROM usuarios WHERE id=%s AND usuario != 'admin'", (uid,))
        row = cur2.fetchone(); cur2.close(); conn2.close()
        if not row or row[0] != _area_atual(sess) or row[1] in ("admin", "gestor", "dev"):
            raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM usuarios WHERE id=%s AND usuario != 'admin'", (uid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

BLOQUEIO_TIPOS_VALIDOS = ('ferias', 'afastamento', 'recorrente')

class BloqueioModel(BaseModel):
    tipo: str  # 'ferias' | 'afastamento' | 'recorrente'
    data_inicio: Optional[str] = None
    data_fim: Optional[str] = None
    dia_semana: Optional[int] = None  # 0=segunda .. 6=domingo (igual date.weekday())
    hora_inicio: Optional[str] = None
    hora_fim: Optional[str] = None
    descricao: str = ""

@router.get("/api/usuarios/{uid}/bloqueios")
def listar_bloqueios(uid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, tipo, data_inicio, data_fim, dia_semana, hora_inicio, hora_fim, descricao
            FROM funcionario_bloqueios WHERE usuario_id=%s ORDER BY data_inicio NULLS LAST, dia_semana NULLS LAST
        """, (uid,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{
            "id": r[0], "tipo": r[1],
            "data_inicio": str(r[2]) if r[2] else None, "data_fim": str(r[3]) if r[3] else None,
            "dia_semana": r[4], "hora_inicio": str(r[5])[:5] if r[5] else None,
            "hora_fim": str(r[6])[:5] if r[6] else None, "descricao": r[7],
        } for r in rows]
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/usuarios/{uid}/bloqueios")
def criar_bloqueio(uid: int, b: BloqueioModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    if b.tipo not in BLOQUEIO_TIPOS_VALIDOS: raise HTTPException(status_code=400, detail="Tipo de bloqueio inválido")
    if b.tipo in ("ferias", "afastamento") and not (b.data_inicio and b.data_fim):
        raise HTTPException(status_code=400, detail="Informe data de início e fim")
    if b.tipo == "recorrente" and b.dia_semana is None:
        raise HTTPException(status_code=400, detail="Informe o dia da semana")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM usuarios WHERE id=%s", (uid,))
        if not cur.fetchone(): raise HTTPException(status_code=404, detail="Funcionário não encontrado")
        cur.execute("""
            INSERT INTO funcionario_bloqueios (usuario_id, tipo, data_inicio, data_fim, dia_semana, hora_inicio, hora_fim, descricao)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (uid, b.tipo, b.data_inicio or None, b.data_fim or None, b.dia_semana,
              b.hora_inicio or None, b.hora_fim or None, b.descricao))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/bloqueios/{bid}")
def deletar_bloqueio(bid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM funcionario_bloqueios WHERE id=%s", (bid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/bloqueios-ativos-hoje")
def bloqueios_ativos_hoje(faiston_token: str = Cookie(None)):
    """Mapa usuario_id -> bloqueio ativo agora (férias/afastamento vigente ou
    recorrência que cai no dia da semana de hoje) -- alimenta o badge nas
    listas de Bloqueios de Agenda (Minha Equipe / Gerenciar Usuários), pra
    o gestor ver quem está bloqueado sem precisar abrir pessoa por pessoa."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        hoje = date.today()
        cur.execute("""
            SELECT usuario_id, tipo, data_fim, dia_semana, hora_inicio, hora_fim, descricao
            FROM funcionario_bloqueios
            WHERE (tipo IN ('ferias','afastamento') AND data_inicio <= %s AND data_fim >= %s)
               OR (tipo = 'recorrente' AND dia_semana = %s)
        """, (hoje, hoje, hoje.weekday()))
        out = {}
        for uid, tipo, data_fim, dia_semana, hi, hf, descricao in cur.fetchall():
            atual = out.get(uid)
            if atual and atual["tipo"] in ("ferias", "afastamento"):
                continue  # férias/afastamento tem prioridade sobre recorrência no badge
            out[str(uid)] = {
                "tipo": tipo,
                "descricao": descricao or "",
                "data_fim": data_fim.isoformat() if data_fim else None,
                "hora_inicio": hi.strftime("%H:%M") if hi else None,
                "hora_fim": hf.strftime("%H:%M") if hf else None,
            }
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
