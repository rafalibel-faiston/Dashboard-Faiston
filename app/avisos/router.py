"""Avisos globais (banner de novidade e e-mail do Assistente OPS).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from fastapi import APIRouter, BackgroundTasks, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.auth import get_session
from app.core.db import get_db
from app.core.email import _brevo_send, _resolver_system_url, _shell_email


router = APIRouter()


# --- AVISOS GLOBAIS (banner de novidade, ex.: lançamento do Assistente OPS) ---
# Não usa a tabela `notificacoes` de propósito: aquela tem um cap global de
# 200 linhas (pensado pra feed de atividade tipo "tarefa criada"), e criar
# uma linha por usuário pra um aviso de massa evictaria notificações reais
# de gente que não tem nada a ver com o aviso. Um aviso global é 1 linha só
# em `configuracoes` (mesmo padrão já usado por outras configs), lido por
# todo mundo; "visto" é rastreado no navegador (localStorage), não no banco.
_AVISO_OPS_CHAVE = "aviso_assistente_ops"

@router.get("/api/avisos-ops")
def get_aviso_ops(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        import json as _json
        cur = conn.cursor()
        cur.execute("SELECT valor FROM configuracoes WHERE chave=%s", (_AVISO_OPS_CHAVE,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return _json.loads(row[0]) if row else None
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _corpo_email_aviso_ops(link_absoluto: str) -> str:
    return f"""
        <p style="color:#3D4152;font-size:14.5px;margin:0 0 18px;line-height:1.6">Tem novidade no Faiston OPS: o <strong>Assistente OPS</strong> já está no ar. É o ícone roxo flutuante no canto da tela — pergunte sobre as suas tarefas, um carimbo de atendimento ou o resumo da sua semana.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
          <tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
            <a href="{link_absoluto}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">Conhecer o Assistente OPS &nbsp;&rarr;</a>
          </td></tr>
        </table>
        <p style="color:#8A8FA3;font-size:12.5px;margin:0;line-height:1.6">O link leva direto pra página de Ajuda, com o assistente já aberto pra você testar.</p>
    """

def _enviar_emails_aviso_ops(system_url: str):
    """Roda em background (BackgroundTasks) -- dispara depois da resposta ao
    admin, pra não travar o request esperando um e-mail por pessoa. Mesmo
    padrão do alerta de pendências de fim de dia: um e-mail por pessoa
    (nunca todo mundo no mesmo `to`, isso vazaria o email de cada um pra
    todo o resto da empresa). Não inclui perfil='demo' (conta fictícia)."""
    conn = get_db()
    if not conn:
        print("[aviso-ops] banco offline, e-mails não enviados")
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM usuarios WHERE ativo=TRUE AND perfil != 'demo' AND COALESCE(email,'') != ''")
        emails = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()
        link_absoluto = f"{system_url.rstrip('/')}/ajuda?destaque=assistente"
        html = _shell_email("Novidade no Faiston OPS", "Conheça o Assistente OPS",
                            _corpo_email_aviso_ops(link_absoluto),
                            rodape="Aviso automático do Faiston OPS · por favor não responda.")
        enviados = sum(1 for email in emails if _brevo_send(email, "🤖 Novidade no Faiston OPS — conheça o Assistente OPS", html))
        print(f"[aviso-ops] {enviados}/{len(emails)} e-mail(s) enviado(s)")
    except Exception as e:
        print(f"[aviso-ops] erro ao enviar e-mails: {e}")

@router.get("/api/avisos-ops/previa")
def previa_aviso_ops(request: Request, faiston_token: str = Cookie(None)):
    """Renderiza o e-mail de aviso exatamente como ele sai, pra conferir antes
    de disparar pra todo mundo -- só abre a página, não envia nada."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    link_absoluto = f"{_resolver_system_url(request).rstrip('/')}/ajuda?destaque=assistente"
    html = _shell_email("Novidade no Faiston OPS", "Conheça o Assistente OPS",
                        _corpo_email_aviso_ops(link_absoluto),
                        rodape="Aviso automático do Faiston OPS · por favor não responda.")
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})

@router.post("/api/avisos-ops/disparar")
def disparar_aviso_ops(bg: BackgroundTasks, request: Request, faiston_token: str = Cookie(None)):
    """Dispara (ou redispara) o aviso de novidade do Assistente OPS pra todo
    mundo -- reexecutar atualiza `disparado_em`, o que faz o banner aparecer
    de novo até pra quem já tinha fechado (localStorage compara por esse
    valor, não por ter visto uma vez na vida). Também dispara um e-mail
    (em background) pra quem tem endereço cadastrado -- o banner só alcança
    quem abrir o sistema, o e-mail alcança todo mundo mesmo sem logar."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        import json as _json
        from datetime import datetime as _dt
        valor = _json.dumps({
            "mensagem": "🤖 Novidade: conheça o Assistente OPS! Ele acha carimbo, mostra suas tarefas e resume a sua semana.",
            "link": "/ajuda?destaque=assistente",
            "disparado_em": _dt.utcnow().isoformat(),
        })
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO configuracoes (chave, valor, atualizado_em) VALUES (%s, %s, NOW())
               ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor, atualizado_em=NOW()""",
            (_AVISO_OPS_CHAVE, valor)
        )
        conn.commit(); cur.close(); conn.close()
        bg.add_task(_enviar_emails_aviso_ops, _resolver_system_url(request))
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
