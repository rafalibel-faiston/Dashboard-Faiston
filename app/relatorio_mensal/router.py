"""Relatório mensal (montagem, envio por e-mail e endpoints).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from calendar import monthrange
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os
import smtplib
import ssl

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import HTMLResponse

from app.core.auth import get_session
from app.core.db import get_db

# --- corpo ---
router = APIRouter()


def _coletar_dados_mes(ano: int, mes: int) -> dict:
    """Coleta KPIs, horas por funcionário e por cliente do mês especificado."""
    conn = get_db()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        inicio = date(ano, mes, 1).isoformat()
        fim    = date(ano, mes, monthrange(ano, mes)[1]).isoformat()

        # Total de tarefas e por status
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='concluido'    THEN 1 ELSE 0 END) AS concluidos,
                SUM(CASE WHEN status='em_andamento' THEN 1 ELSE 0 END) AS andamento,
                SUM(CASE WHEN status='aberto'       THEN 1 ELSE 0 END) AS abertos,
                COALESCE(SUM(segundos),0) AS total_seg
            FROM tarefas
            WHERE criado_em >= %s AND criado_em <= %s
        """, (inicio + " 00:00:00", fim + " 23:59:59"))
        row = cur.fetchone()
        total, concluidos, andamento, abertos, total_seg = row
        sla = round(concluidos / total * 100) if total else 0

        # Horas por funcionário (top 10)
        cur.execute("""
            SELECT u.nome, ROUND(SUM(t.segundos)/3600.0, 1) AS horas
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
            WHERE t.criado_em >= %s AND t.criado_em <= %s
            GROUP BY u.nome ORDER BY horas DESC LIMIT 10
        """, (inicio + " 00:00:00", fim + " 23:59:59"))
        por_func = [{"nome": r[0], "horas": float(r[1])} for r in cur.fetchall()]

        # Horas por cliente (top 8)
        cur.execute("""
            SELECT cliente, ROUND(SUM(segundos)/3600.0, 1) AS horas
            FROM tarefas
            WHERE criado_em >= %s AND criado_em <= %s AND cliente IS NOT NULL AND cliente != ''
            GROUP BY cliente ORDER BY horas DESC LIMIT 8
        """, (inicio + " 00:00:00", fim + " 23:59:59"))
        por_cliente = [{"nome": r[0], "horas": float(r[1])} for r in cur.fetchall()]

        cur.close(); conn.close()
        return {
            "ano": ano, "mes": mes,
            "total": total, "concluidos": concluidos,
            "andamento": andamento, "abertos": abertos,
            "total_horas": round(total_seg / 3600, 1),
            "sla": sla,
            "por_func": por_func,
            "por_cliente": por_cliente,
        }
    except Exception as e:
        print(f"Erro ao coletar dados relatório: {e}")
        return {}


MESES_PT = ["", "Janeiro","Fevereiro","Março","Abril","Maio","Junho",
            "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]


def _gerar_html_relatorio(d: dict) -> str:
    if not d:
        return "<p>Sem dados disponíveis.</p>"

    mes_nome = MESES_PT[d["mes"]]
    max_h_func    = max((f["horas"] for f in d["por_func"]),    default=1)
    max_h_cliente = max((c["horas"] for c in d["por_cliente"]), default=1)

    def barra(pct, cor):
        return f'<div style="height:8px;border-radius:4px;background:#1E1E2E;margin-top:4px"><div style="height:8px;border-radius:4px;background:{cor};width:{pct}%"></div></div>'

    linhas_func = "".join(f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1E1E2E;color:#C8CBE0;font-size:14px">{f['nome'].split()[0]}</td>
          <td style="padding:10px 0;border-bottom:1px solid #1E1E2E;width:55%">
            {barra(round(f['horas']/max_h_func*100), 'linear-gradient(90deg,#5B2EE0,#06D7E6)')}
          </td>
          <td style="padding:10px 0;border-bottom:1px solid #1E1E2E;text-align:right;color:#06D7E6;font-weight:700;font-size:14px">{f['horas']}h</td>
        </tr>""" for f in d["por_func"])

    linhas_cliente = "".join(f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1E1E2E;color:#C8CBE0;font-size:14px">{c['nome']}</td>
          <td style="padding:10px 0;border-bottom:1px solid #1E1E2E;width:50%">
            {barra(round(c['horas']/max_h_cliente*100), 'linear-gradient(90deg,#B826C9,#EC4899)')}
          </td>
          <td style="padding:10px 0;border-bottom:1px solid #1E1E2E;text-align:right;color:#EC4899;font-weight:700;font-size:14px">{c['horas']}h</td>
        </tr>""" for c in d["por_cliente"])

    sla_cor = "#06D7E6" if d["sla"] >= 92 else "#EC4899"

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Relatório Mensal · Faiston OPS · {mes_nome} {d['ano']}</title></head>
<body style="margin:0;padding:0;background:#07070F;font-family:'Helvetica Neue',Arial,sans-serif;color:#E2E8F0">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#07070F;padding:32px 0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0" style="background:#0D0D1A;border:1px solid #1E1E2E;border-radius:16px;overflow:hidden">

  <!-- HEADER -->
  <tr><td style="background:linear-gradient(135deg,#5B2EE0,#B826C9,#06D7E6);padding:2px 0"></td></tr>
  <tr><td style="padding:32px 40px 24px;border-bottom:1px solid #1E1E2E">
    <table width="100%"><tr>
      <td>
        <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#5B2EE0;margin-bottom:6px">Relatório Operacional</div>
        <div style="font-size:28px;font-weight:800;color:#FFFFFF;letter-spacing:-0.5px">FAISTON <span style="background:linear-gradient(90deg,#5B2EE0,#06D7E6);-webkit-background-clip:text;-webkit-text-fill-color:transparent">OPS</span></div>
        <div style="font-size:14px;color:#5E647A;margin-top:4px">{mes_nome} {d['ano']} · Gerado automaticamente</div>
      </td>
      <td align="right" style="vertical-align:top">
        <div style="display:inline-block;background:rgba(6,215,230,0.08);border:1px solid rgba(6,215,230,0.25);border-radius:8px;padding:8px 16px;font-size:12px;font-weight:700;color:#06D7E6">SLA {d['sla']}% <span style="color:{'#06D7E6' if d['sla']>=92 else '#EC4899'}">{'✓ Meta' if d['sla']>=92 else '✗ Abaixo'}</span></div>
      </td>
    </tr></table>
  </td></tr>

  <!-- KPIs -->
  <tr><td style="padding:28px 40px;border-bottom:1px solid #1E1E2E">
    <table width="100%" cellspacing="0" cellpadding="0"><tr>
      <td align="center" style="padding:16px;background:#12121F;border-radius:12px;border:1px solid #1E1E2E">
        <div style="font-size:32px;font-weight:900;color:#06D7E6">{d['total_horas']}h</div>
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#5E647A;margin-top:4px">Horas Apontadas</div>
      </td>
      <td width="12"></td>
      <td align="center" style="padding:16px;background:#12121F;border-radius:12px;border:1px solid #1E1E2E">
        <div style="font-size:32px;font-weight:900;color:#5B2EE0">{d['total']}</div>
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#5E647A;margin-top:4px">Total de Tickets</div>
      </td>
      <td width="12"></td>
      <td align="center" style="padding:16px;background:#12121F;border-radius:12px;border:1px solid #1E1E2E">
        <div style="font-size:32px;font-weight:900;color:#B826C9">{d['concluidos']}</div>
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#5E647A;margin-top:4px">Concluídos</div>
      </td>
      <td width="12"></td>
      <td align="center" style="padding:16px;background:#12121F;border-radius:12px;border:1px solid #1E1E2E">
        <div style="font-size:32px;font-weight:900;color:{sla_cor}">{d['sla']}%</div>
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#5E647A;margin-top:4px">SLA</div>
      </td>
    </tr></table>
  </td></tr>

  <!-- EQUIPE -->
  <tr><td style="padding:28px 40px;border-bottom:1px solid #1E1E2E">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#5E647A;margin-bottom:16px">Equipe · Horas por Funcionário</div>
    <table width="100%" cellspacing="0" cellpadding="0">{linhas_func}</table>
  </td></tr>

  <!-- CLIENTES -->
  <tr><td style="padding:28px 40px;border-bottom:1px solid #1E1E2E">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#5E647A;margin-bottom:16px">Clientes · Distribuição de Horas</div>
    <table width="100%" cellspacing="0" cellpadding="0">{linhas_cliente}</table>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="padding:24px 40px;text-align:center">
    <div style="font-size:11px;color:#3A3A55">Relatório gerado automaticamente pelo <strong style="color:#5B2EE0">Faiston OPS</strong> · rafael.libel@faiston.com</div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def _enviar_email(html: str, mes_nome: str, ano: int):
    email_to = os.environ.get("EMAIL_RELATORIO", "rafael.libel@faiston.com")
    subject  = f"Relatório Faiston OPS · {mes_nome} {ano}"

    # ── Brevo (API HTTP — funciona no Railway) ───────────────────────
    import urllib.request, json as _json
    brevo_key = os.environ.get("BREVO_API_KEY", "")
    email_user = os.environ.get("EMAIL_USER", "")
    if brevo_key and email_user:
        payload = _json.dumps({
            "sender": {"name": "Faiston OPS", "email": email_user},
            "to": [{"email": email_to}],
            "subject": subject,
            "htmlContent": html,
        }).encode()
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"api-key": brevo_key, "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        print(f"Brevo OK — id {result.get('messageId')} — {mes_nome}/{ano} → {email_to}")
        return

    # ── Fallback SMTP (Gmail) ─────────────────────────────────────────
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        raise ValueError("Configure RESEND_API_KEY (recomendado) ou SMTP_USER + SMTP_PASS")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Faiston OPS <{smtp_user}>"
    msg["To"]      = email_to
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(smtp_host, 465, context=ctx, timeout=15) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, email_to, msg.as_string())
    except Exception as e1:
        print(f"SMTP_SSL 465 falhou: {e1} — tentando STARTTLS 587")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.ehlo(); s.starttls(context=ctx); s.ehlo()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, email_to, msg.as_string())
    print(f"SMTP OK — Relatório {mes_nome}/{ano} enviado para {email_to}")


def _job_relatorio_mensal():
    hoje = date.today()
    # Mês anterior
    primeiro_do_mes = hoje.replace(day=1)
    mes_ant = primeiro_do_mes - timedelta(days=1)
    d = _coletar_dados_mes(mes_ant.year, mes_ant.month)
    if not d:
        print("Relatório mensal: sem dados para enviar")
        return
    html = _gerar_html_relatorio(d)
    try:
        _enviar_email(html, MESES_PT[mes_ant.month], mes_ant.year)
    except Exception as e:
        print(f"Erro ao enviar relatório: {e}")


# ─── Endpoints de relatório ───

@router.get("/api/relatorio-mensal/preview", response_class=HTMLResponse)
def preview_relatorio(mes: int = 0, ano: int = 0, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"):
        raise HTTPException(status_code=403)
    hoje = date.today()
    if not mes:
        # Tenta mês anterior; se vazio, usa mês atual
        ref = (hoje.replace(day=1) - timedelta(days=1))
        d = _coletar_dados_mes(ref.year, ref.month)
        if not d or d.get("total", 0) == 0:
            mes, ano = hoje.month, hoje.year
        else:
            mes, ano = ref.month, ref.year
    if not ano:
        ano = hoje.year
    d = _coletar_dados_mes(ano, mes)
    return HTMLResponse(_gerar_html_relatorio(d))


@router.post("/api/relatorio-mensal/enviar")
def enviar_relatorio_manual(mes: int = 0, ano: int = 0, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"):
        raise HTTPException(status_code=403)
    hoje = date.today()
    if not mes:
        # Tenta mês anterior; se vazio, usa mês atual
        ref = (hoje.replace(day=1) - timedelta(days=1))
        d_test = _coletar_dados_mes(ref.year, ref.month)
        if not d_test or d_test.get("total", 0) == 0:
            mes, ano = hoje.month, hoje.year
        else:
            mes, ano = ref.month, ref.year
    if not ano:
        ano = hoje.year
    d = _coletar_dados_mes(ano, mes)
    if not d:
        raise HTTPException(status_code=404, detail="Sem dados para o período")
    html = _gerar_html_relatorio(d)
    try:
        _enviar_email(html, MESES_PT[mes], ano)
        return {"sucesso": True, "mensagem": f"Relatório {MESES_PT[mes]}/{ano} enviado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
#  GESTÃO DE PROJETOS
