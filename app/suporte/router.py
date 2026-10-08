"""Suporte: solicitações dos usuários e respostas da equipe dev.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Cookie, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import _is_dev, get_session
from app.core.db import get_db
from app.core.email import _brevo_send, _resolver_system_url, _shell_email

# --- corpo ---
router = APIRouter()


SUPORTE_CATEGORIA_VALIDAS = ('bug', 'duvida', 'melhoria')
SUPORTE_STATUS_VALIDOS = ('aberto', 'em_andamento', 'resolvido')
SUPORTE_ANEXO_MAX_CHARS = 7_000_000  # ~5MB de imagem original, já em base64 (~33% maior)

class SuporteSolicitacaoModel(BaseModel):
    titulo: str
    descricao: str
    categoria: str = "duvida"
    anexo_base64: Optional[str] = None
    anexo_nome: Optional[str] = None

SUPORTE_NOTIFICAR_EMAILS = ["vinicios75soares165@gmail.com", "rafael.libel@gmail.com"]

def _esc_html_email(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _suporte_home_path(perfil, cargo):
    """Mesmo mapeamento perfil/cargo → home usado no pós-login (login.html) e
    em _redirect_login_ou_home -- usado aqui pra montar, no e-mail, o link
    que leva quem abriu o chamado direto pra tela onde ele está (dashboard,
    funcionario ou n2), já logado."""
    if perfil in ("admin", "gestor", "demo", "diretor"):
        return "/dashboard"
    if cargo == "n2":
        return "/n2"
    return "/funcionario"

def _suporte_enviar_notificacao(titulo, descricao, categoria, autor_nome, anexo_base64, anexo_nome):
    """Dispara em background (não atrasa a resposta pra quem abriu a
    solicitação). Só na branch de teste por enquanto (2026-07-30, a pedido
    do usuário) -- destinatários fixos, não passa pela tela de admin."""
    categoria_label = {"bug": "Bug", "duvida": "Dúvida", "melhoria": "Pedido de melhoria"}.get(categoria, categoria)
    corpo = f"""
        <p style="color:#3D4152;font-size:14.5px;margin:0 0 14px;line-height:1.6"><strong>{_esc_html_email(autor_nome)}</strong> abriu uma solicitação de suporte.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
          <tr><td style="background:#F7F7FB;border:1px solid #E5E8F0;border-radius:12px;padding:16px 18px">
            <p style="margin:0 0 4px;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#9097AC">{categoria_label}</p>
            <p style="margin:0 0 10px;font-size:16px;font-weight:700;color:#0B0D1F">{_esc_html_email(titulo)}</p>
            <p style="margin:0;font-size:14px;color:#3D4152;white-space:pre-line">{_esc_html_email(descricao)}</p>
          </td></tr>
        </table>
        <p style="color:#8A8FA3;font-size:12.5px;margin:0;line-height:1.6">{"Print anexado a este e-mail." if anexo_base64 else "Sem anexo."} Veja e gerencie na Área de Dev &gt; Suporte.</p>
    """
    anexos = None
    if anexo_base64 and "," in anexo_base64:
        conteudo_b64 = anexo_base64.split(",", 1)[1]
        anexos = [{"content": conteudo_b64, "name": anexo_nome or "anexo.png"}]
    _brevo_send(SUPORTE_NOTIFICAR_EMAILS, f"🎫 Nova solicitação de suporte — {titulo}",
                _shell_email("Nova solicitação", categoria_label, corpo), anexos=anexos)

def _suporte_botao_email(link, texto):
    return f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
          <tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
            <a href="{link}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">{texto} &nbsp;&rarr;</a>
          </td></tr>
        </table>
    """

def _suporte_enviar_email_resposta(email, nome, titulo, mensagem, autor_nome, link):
    """Avisa quem abriu o chamado quando o suporte responde, com um link que
    já leva pra 'Minhas solicitações' pra responder de volta pelo sistema
    (2026-09-09) -- antes a resposta só aparecia se a pessoa voltasse a abrir
    o modal por conta própria, sem nenhum aviso."""
    if not email: return
    corpo = f"""
        <p style="color:#3D4152;font-size:14.5px;margin:0 0 14px;line-height:1.6">Olá, {_esc_html_email(nome)}. <strong>{_esc_html_email(autor_nome)}</strong> respondeu seu chamado de suporte.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
          <tr><td style="background:#F7F7FB;border:1px solid #E5E8F0;border-radius:12px;padding:16px 18px">
            <p style="margin:0 0 10px;font-size:16px;font-weight:700;color:#0B0D1F">{_esc_html_email(titulo)}</p>
            <p style="margin:0;font-size:14px;color:#3D4152;white-space:pre-line">{_esc_html_email(mensagem)}</p>
          </td></tr>
        </table>
        {_suporte_botao_email(link, "Ver e responder")}
        <p style="color:#8A8FA3;font-size:12.5px;margin:0;line-height:1.6">Responda direto pelo sistema: o botão acima já abre "Minhas solicitações" com este chamado. Se preferir entrar por conta própria, é só clicar em <strong>"Falar com o suporte"</strong> (ícone de boia no menu) e depois na aba <strong>"Minhas solicitações"</strong> — lá ficam todos os seus chamados e as respostas.</p>
    """
    _brevo_send(email, f"💬 Resposta no seu chamado — {titulo}",
                _shell_email("Resposta do suporte", titulo, corpo))

def _suporte_notificar_devs_nova_mensagem(titulo, mensagem, autor_nome, link):
    """Avisa a equipe dev quando quem abriu o chamado responde de volta
    (2026-09-09) -- simetria com _suporte_enviar_notificacao, que já avisa a
    equipe na abertura."""
    corpo = f"""
        <p style="color:#3D4152;font-size:14.5px;margin:0 0 14px;line-height:1.6"><strong>{_esc_html_email(autor_nome)}</strong> respondeu no chamado de suporte.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
          <tr><td style="background:#F7F7FB;border:1px solid #E5E8F0;border-radius:12px;padding:16px 18px">
            <p style="margin:0 0 10px;font-size:16px;font-weight:700;color:#0B0D1F">{_esc_html_email(titulo)}</p>
            <p style="margin:0;font-size:14px;color:#3D4152;white-space:pre-line">{_esc_html_email(mensagem)}</p>
          </td></tr>
        </table>
        {_suporte_botao_email(link, "Ver na Área de Dev")}
    """
    _brevo_send(SUPORTE_NOTIFICAR_EMAILS, f"💬 Nova resposta no chamado — {titulo}",
                _shell_email("Resposta de quem abriu", titulo, corpo))

def _suporte_buscar_mensagens(cur, ids):
    """Busca as mensagens de uma leva de solicitações de uma vez só (evita
    N+1 nas telas de listagem) e devolve agrupado por solicitacao_id, da
    mais antiga pra mais nova."""
    if not ids: return {}
    cur.execute("""
        SELECT solicitacao_id, autor_nome, autor_tipo, mensagem, criado_em
        FROM suporte_mensagens WHERE solicitacao_id = ANY(%s) ORDER BY criado_em ASC
    """, (ids,))
    agrupado = {}
    for sid_, autor_nome, autor_tipo, mensagem, criado_em in cur.fetchall():
        agrupado.setdefault(sid_, []).append({
            "autor_nome": autor_nome, "autor_tipo": autor_tipo, "mensagem": mensagem,
            "criado_em": criado_em.strftime("%d/%m/%Y %H:%M") if criado_em else "",
        })
    return agrupado

@router.post("/api/suporte")
def criar_suporte(s: SuporteSolicitacaoModel, bg: BackgroundTasks, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    if not s.titulo.strip(): raise HTTPException(status_code=400, detail="Título obrigatório")
    if not s.descricao.strip(): raise HTTPException(status_code=400, detail="Descrição obrigatória")
    categoria = s.categoria if s.categoria in SUPORTE_CATEGORIA_VALIDAS else "duvida"
    if s.anexo_base64 and len(s.anexo_base64) > SUPORTE_ANEXO_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Anexo muito grande (máximo ~5MB)")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO suporte_solicitacoes
                (titulo, descricao, categoria, anexo_base64, anexo_nome, criado_por, criado_por_nome)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (s.titulo.strip()[:200], s.descricao.strip(), categoria,
              s.anexo_base64 or None, (s.anexo_nome or '').strip()[:200] or None,
              sess["id"], sess["nome"]))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        bg.add_task(_suporte_enviar_notificacao, s.titulo.strip()[:200], s.descricao.strip(), categoria,
                    sess["nome"], s.anexo_base64, s.anexo_nome)
        return {"sucesso": True, "id": new_id}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/dev-suporte")
def dev_listar_suporte(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, descricao, categoria, status, criado_por_nome, criado_em,
                   (anexo_base64 IS NOT NULL) AS tem_anexo
            FROM suporte_solicitacoes ORDER BY criado_em DESC
        """)
        rows = cur.fetchall()
        mensagens = _suporte_buscar_mensagens(cur, [r[0] for r in rows])
        out = [{
            "id": r[0], "titulo": r[1], "descricao": r[2], "categoria": r[3], "status": r[4],
            "criado_por_nome": r[5], "criado_em": r[6].strftime("%d/%m/%Y %H:%M") if r[6] else "",
            "tem_anexo": r[7], "mensagens": mensagens.get(r[0], []),
        } for r in rows]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class SuporteRespostaModel(BaseModel):
    resposta: str

@router.put("/api/dev-suporte/{sid}/resposta")
def dev_responder_suporte(sid: int, s: SuporteRespostaModel, bg: BackgroundTasks, request: Request,
                           faiston_token: str = Cookie(None)):
    """Resposta pra quem abriu a solicitação -- vira uma mensagem na thread
    (2026-09-09; antes era um campo único que se sobrescrevia a cada
    resposta) e dispara um e-mail avisando quem abriu, com link pra
    responder de volta pelo próprio sistema. Não muda o status sozinha --
    quem responde ainda decide separadamente se marca em_andamento/resolvido."""
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    resposta = s.resposta.strip()
    if not resposta: raise HTTPException(status_code=400, detail="Resposta vazia")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.titulo, u.email, u.nome, u.perfil, u.cargo
            FROM suporte_solicitacoes s LEFT JOIN usuarios u ON u.id = s.criado_por
            WHERE s.id=%s
        """, (sid,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="Solicitação não encontrada")
        titulo, email_dest, nome_dest, perfil_dest, cargo_dest = row
        cur.execute(
            "INSERT INTO suporte_mensagens (solicitacao_id, autor_id, autor_nome, autor_tipo, mensagem) VALUES (%s,%s,%s,'dev',%s)",
            (sid, sess["id"], sess["nome"], resposta))
        cur.execute("UPDATE suporte_solicitacoes SET atualizado_em=NOW() WHERE id=%s", (sid,))
        conn.commit(); cur.close(); conn.close()
        if email_dest:
            system_url = _resolver_system_url(request)
            link = f"{system_url}{_suporte_home_path(perfil_dest, cargo_dest)}?suporte={sid}"
            bg.add_task(_suporte_enviar_email_resposta, email_dest, nome_dest, titulo, resposta, sess["nome"], link)
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/suporte/minhas")
def listar_minhas_suporte(faiston_token: str = Cookie(None)):
    """Solicitações que EU abri, com a thread de mensagens se já tiver vindo
    resposta -- complemento do POST /api/suporte, que antes era só de ida
    (2026-08-11)."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, descricao, categoria, status, criado_em
            FROM suporte_solicitacoes WHERE criado_por=%s ORDER BY criado_em DESC
        """, (sess["id"],))
        rows = cur.fetchall()
        mensagens = _suporte_buscar_mensagens(cur, [r[0] for r in rows])
        out = [{
            "id": r[0], "titulo": r[1], "descricao": r[2], "categoria": r[3], "status": r[4],
            "criado_em": r[5].strftime("%d/%m/%Y %H:%M") if r[5] else "",
            "mensagens": mensagens.get(r[0], []),
        } for r in rows]
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class SuporteMensagemModel(BaseModel):
    mensagem: str

@router.post("/api/suporte/{sid}/mensagens")
def suporte_responder_usuario(sid: int, s: SuporteMensagemModel, bg: BackgroundTasks, request: Request,
                               faiston_token: str = Cookie(None)):
    """Quem abriu o chamado responde de volta pelo sistema (2026-09-09) --
    contrapartida de dev_responder_suporte, fecha o vai-e-vem que antes só
    existia numa direção (dev → usuário, sem volta). Só quem abriu pode
    responder aqui -- a equipe dev responde pelo endpoint acima."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    mensagem = s.mensagem.strip()
    if not mensagem: raise HTTPException(status_code=400, detail="Mensagem vazia")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT titulo, criado_por FROM suporte_solicitacoes WHERE id=%s", (sid,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="Solicitação não encontrada")
        titulo, criado_por = row
        if criado_por != sess["id"]:
            cur.close(); conn.close()
            raise HTTPException(status_code=403, detail="Essa solicitação não é sua")
        cur.execute(
            "INSERT INTO suporte_mensagens (solicitacao_id, autor_id, autor_nome, autor_tipo, mensagem) VALUES (%s,%s,%s,'usuario',%s)",
            (sid, sess["id"], sess["nome"], mensagem))
        cur.execute("UPDATE suporte_solicitacoes SET atualizado_em=NOW() WHERE id=%s", (sid,))
        conn.commit(); cur.close(); conn.close()
        system_url = _resolver_system_url(request)
        bg.add_task(_suporte_notificar_devs_nova_mensagem, titulo, mensagem, sess["nome"],
                    f"{system_url}/dashboard?go=areaDev&suporte={sid}")
        return {"sucesso": True}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/dev-suporte/{sid}/anexo")
def dev_ver_anexo_suporte(sid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT anexo_base64, anexo_nome FROM suporte_solicitacoes WHERE id=%s", (sid,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row or not row[0]: raise HTTPException(status_code=404, detail="Sem anexo")
        return {"anexo_base64": row[0], "anexo_nome": row[1]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class SuporteStatusModel(BaseModel):
    status: str

@router.put("/api/dev-suporte/{sid}/status")
def dev_atualizar_status_suporte(sid: int, s: SuporteStatusModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    if s.status not in SUPORTE_STATUS_VALIDOS: raise HTTPException(status_code=400, detail="Status inválido")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE suporte_solicitacoes SET status=%s, atualizado_em=NOW() WHERE id=%s", (s.status, sid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/dev-suporte/{sid}")
def dev_deletar_suporte(sid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not _is_dev(sess): raise HTTPException(status_code=403, detail="Acesso restrito à equipe dev")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM suporte_solicitacoes WHERE id = %s", (sid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
