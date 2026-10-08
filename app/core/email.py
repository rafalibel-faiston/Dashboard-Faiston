"""Envio de e-mail (Brevo) e o layout padrão dos e-mails do Faiston OPS.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
import os

from fastapi import Request

# --- corpo ---
# --- EMAIL ---
def _resolver_system_url(request: Request = None) -> str:
    """URL base do sistema pra montar links de e-mail (definir senha, acesso etc.).

    SYSTEM_URL explícita tem prioridade (ex.: fixar um domínio custom em
    produção). Sem ela, usa o host que respondeu esta requisição -- assim
    cada ambiente (teste, produção, local) manda o link de si mesmo, em vez
    de sempre cair no domínio de produção hardcoded por engano. Esse domínio
    só entra como último recurso, quando nem uma coisa nem outra existe
    (chamada fora de um request, ex. script avulso).
    """
    configurado = (os.environ.get("SYSTEM_URL") or "").strip()
    if configurado:
        return configurado.rstrip("/")
    if request is not None:
        # Lê os headers do proxy do Railway na mão (mesmo padrão de
        # _login_ip) em vez de confiar em request.url/base_url: sem
        # --proxy-headers no uvicorn, esses refletem o scheme/host internos
        # (http em vez do https externo), não o que o navegador realmente usa.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        if host:
            return f"{scheme}://{host}".rstrip("/")
    return "https://dashboard-faiston-production.up.railway.app"


def enviar_email_acesso(destinatario: str, nome: str, usuario: str, senha, perfil_guia: str = "", system_url: str = "") -> bool:
    system_url = (system_url or _resolver_system_url()).rstrip("/")
    if not destinatario:
        return False
    # O guia carrega o perfil na URL porque o e-mail costuma ser aberto antes
    # do primeiro login: sem cookie de sessão, /ajuda cairia no fallback
    # anônimo e mostraria as abas de todos os perfis.
    url_ajuda = f"{system_url}/ajuda?perfil={perfil_guia}" if perfil_guia else f"{system_url}/ajuda"
    try:
        perfil_map = {"admin": "Admin", "gestor": "Gestor", "funcionario": "Funcionário", "diretor": "Diretor", "dev": "Dev"}
        # Botões em tabela (renderizam bem no Outlook/Gmail). bgcolor garante cor sólida onde gradiente não funciona.
        btn = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 10px">
          <tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
            <a href="{system_url}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">Acessar o Sistema &nbsp;&rarr;</a>
          </td></tr>
        </table>"""
        btn_ajuda = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 4px">
          <tr><td align="center" bgcolor="#ffffff" style="border-radius:12px;border:2px solid #E5E8F0">
            <a href="{url_ajuda}" style="display:block;color:#5B2EE0;text-decoration:none;padding:13px 24px;font-weight:700;font-size:14px;border-radius:12px">📖 Ver Guia de Uso</a>
          </td></tr>
        </table>"""
        ajuda = f"<a href='{url_ajuda}' style='color:#5B2EE0;font-weight:600'>Guia de Uso</a>"
        if senha is None:
            bloco_senha = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px">
              <tr><td style="background:#FFF8E6;border:1px solid #FFD166;border-radius:12px;padding:16px 18px">
                <p style="color:#B8860B;font-size:13px;font-weight:700;margin:0 0 5px">🔑 Senha não alterada</p>
                <p style="color:#7A6020;font-size:13px;margin:0;line-height:1.6">Use a senha que você já cadastrou no sistema. Caso não lembre, entre em contato com o administrador para redefinir.</p>
              </td></tr>
            </table>"""
        else:
            bloco_senha = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px">
              <tr><td style="background:#F7F8FC;border:1px solid #ECEEF6;border-radius:14px;padding:4px 0">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr><td style="padding:16px 22px 10px">
                    <p style="color:#9097AC;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin:0 0 5px">👤 Usuário</p>
                    <p style="color:#0B0D1F;font-size:17px;font-weight:700;font-family:'Courier New',monospace;margin:0">{usuario}</p>
                  </td></tr>
                  <tr><td style="padding:0 22px"><div style="height:1px;background:#E5E8F0"></div></td></tr>
                  <tr><td style="padding:12px 22px 18px">
                    <p style="color:#9097AC;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin:0 0 6px">🔑 Senha temporária</p>
                    <p style="color:#5B2EE0;font-size:22px;font-weight:900;font-family:'Courier New',monospace;letter-spacing:3px;margin:0">{senha}</p>
                  </td></tr>
                </table>
              </td></tr>
            </table>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px">
              <tr><td style="background:#FFF8E6;border:1px solid #FFD166;border-radius:12px;padding:14px 18px">
                <p style="color:#B8860B;font-size:13px;font-weight:700;margin:0 0 4px">⚠️ Troca de senha obrigatória</p>
                <p style="color:#7A6020;font-size:13px;margin:0;line-height:1.6">No primeiro acesso, o sistema pedirá que você crie uma senha pessoal.</p>
              </td></tr>
            </table>"""
        html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light only"></head>
<body style="margin:0;padding:0;background:#EEF0F8;-webkit-font-smoothing:antialiased">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EEF0F8;padding:32px 12px">
    <tr><td align="center">
      <table role="presentation" width="540" cellpadding="0" cellspacing="0" style="max-width:540px;width:100%;font-family:'Segoe UI',Arial,sans-serif">

        <!-- Header -->
        <tr><td bgcolor="#5B2EE0" style="border-radius:18px 18px 0 0;background:linear-gradient(135deg,#5B2EE0 0%,#B826C9 55%,#EC4899 100%);padding:38px 32px;text-align:center">
          <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 14px">
            <tr><td style="width:52px;height:52px;background:rgba(255,255,255,0.18);border-radius:14px;text-align:center;vertical-align:middle;font-size:26px">🛰️</td></tr>
          </table>
          <h1 style="color:#ffffff;margin:0;font-size:27px;font-weight:800;letter-spacing:-0.5px">Faiston OPS</h1>
          <p style="color:rgba(255,255,255,0.85);margin:6px 0 0;font-size:13px;letter-spacing:0.5px">TORRE DE CONTROLE</p>
        </td></tr>

        <!-- Body -->
        <tr><td bgcolor="#ffffff" style="background:#ffffff;padding:32px 32px 28px;border-left:1px solid #E5E8F0;border-right:1px solid #E5E8F0">
          <p style="color:#0B0D1F;font-size:19px;font-weight:700;margin:0 0 8px">Olá, {nome}! 👋</p>
          <p style="color:#5E647A;font-size:14px;margin:0 0 24px;line-height:1.65">Seu acesso ao <strong style="color:#0B0D1F">Faiston OPS</strong> foi criado com sucesso. Abaixo estão suas credenciais de entrada:</p>
          {bloco_senha}
          {btn}
          {btn_ajuda}
        </td></tr>

        <!-- Primeiros passos -->
        <tr><td bgcolor="#ffffff" style="background:#ffffff;padding:0 32px 32px;border-left:1px solid #E5E8F0;border-right:1px solid #E5E8F0">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #EEF0F6;padding-top:8px">
            <tr><td style="padding-top:20px">
              <p style="color:#9097AC;font-size:11px;margin:0 0 14px;font-weight:700;text-transform:uppercase;letter-spacing:1px">✨ Primeiros passos</p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td style="padding-bottom:12px;vertical-align:top;width:30px"><span style="display:inline-block;width:24px;height:24px;background:#5B2EE0;color:#fff;border-radius:50%;text-align:center;line-height:24px;font-size:12px;font-weight:800">1</span></td>
                    <td style="padding-bottom:12px;color:#5E647A;font-size:13px;line-height:1.5">Acesse o sistema com o usuário e senha acima</td></tr>
                <tr><td style="padding-bottom:12px;vertical-align:top"><span style="display:inline-block;width:24px;height:24px;background:#5B2EE0;color:#fff;border-radius:50%;text-align:center;line-height:24px;font-size:12px;font-weight:800">2</span></td>
                    <td style="padding-bottom:12px;color:#5E647A;font-size:13px;line-height:1.5">Crie sua senha pessoal (mínimo 6 caracteres)</td></tr>
                <tr><td style="vertical-align:top"><span style="display:inline-block;width:24px;height:24px;background:#5B2EE0;color:#fff;border-radius:50%;text-align:center;line-height:24px;font-size:12px;font-weight:800">3</span></td>
                    <td style="color:#5E647A;font-size:13px;line-height:1.5">Explore o {ajuda} para conhecer todas as funcionalidades</td></tr>
              </table>
            </td></tr>
          </table>
        </td></tr>

        <!-- Footer -->
        <tr><td bgcolor="#ffffff" style="background:#ffffff;border-radius:0 0 18px 18px;border:1px solid #E5E8F0;border-top:none;padding:20px 32px;text-align:center">
          <p style="color:#9097AC;font-size:11px;margin:0;line-height:1.6">Faiston OPS · Torre de Controle<br>Este é um email automático, por favor não responda.</p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body></html>"""
        subject = "🛰️ Seu acesso ao Faiston OPS"

        import urllib.request, json as _json

        # Brevo (API HTTP — funciona no Railway)
        brevo_key = os.environ.get("BREVO_API_KEY", "")
        email_user = os.environ.get("EMAIL_USER", "")
        if brevo_key and email_user:
            payload = _json.dumps({
                "sender": {"name": "Faiston OPS", "email": email_user},
                "to": [{"email": destinatario}],
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
            print(f"[email-acesso] Brevo OK — id {result.get('messageId')} → {destinatario}")
            return True

        print("[email-acesso] Nenhuma configuração de email encontrada (BREVO_API_KEY + EMAIL_USER)")
        return False
    except Exception as e:
        print(f"[email-acesso] Falha ao enviar para {destinatario}: {e}")
        return False


def enviar_email_boas_vindas(destinatario: str, nome: str, link: str) -> bool:
    """Email de conta nova (2026-08-10): sem senha temporária -- a pessoa define
    a própria senha pelo link de token (mesmo mecanismo do "esqueci minha senha").
    Reaproveitado também por reenviar_email_acesso quando a conta ainda não
    completou o primeiro acesso."""
    corpo = f"""
        <p style="color:#3D4152;font-size:14.5px;margin:0 0 18px;line-height:1.6">Olá, {nome}. Seu acesso ao Faiston OPS foi criado — falta só você definir sua senha pra começar a usar.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px">
          <tr><td align="center" bgcolor="#5B2EE0" style="border-radius:12px;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
            <a href="{link}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">Definir minha senha &nbsp;&rarr;</a>
          </td></tr>
        </table>
        <p style="color:#8A8FA3;font-size:12.5px;margin:0;line-height:1.6">Esse link expira em <strong>30 minutos</strong> e só funciona uma vez. Se expirar antes de você usar, use "Esqueci minha senha" na tela de login com seu email pra receber um novo.</p>
    """
    return _brevo_send(destinatario, "Bem-vindo ao Faiston OPS — defina sua senha",
                        _shell_email("Bem-vindo!", "Defina sua senha de acesso", corpo))


def _brevo_send(destinatario, subject: str, html: str, anexos: list = None) -> bool:
    """Envia um e-mail HTML via API do Brevo. Reaproveitado pelo resumo diário,
    reset de senha e notificação de suporte. `destinatario` aceita uma string
    ou uma lista de e-mails (todos no mesmo `to`, visíveis entre si -- uso
    interno de equipe, não notificação a cliente). `anexos` é opcional:
    lista de {"content": base64_sem_prefixo, "name": nome_arquivo}."""
    destinatarios = [destinatario] if isinstance(destinatario, str) else list(destinatario or [])
    destinatarios = [d for d in destinatarios if d]
    if not destinatarios:
        return False
    import urllib.request, json as _json
    brevo_key = os.environ.get("BREVO_API_KEY", "")
    email_user = os.environ.get("EMAIL_USER", "")
    if not (brevo_key and email_user):
        print("[email] BREVO_API_KEY/EMAIL_USER não configurados")
        return False
    try:
        body = {
            "sender": {"name": "Faiston OPS", "email": email_user},
            "to": [{"email": d} for d in destinatarios],
            "subject": subject,
            "htmlContent": html,
        }
        if anexos:
            body["attachment"] = anexos
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"api-key": brevo_key, "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        print(f"[email] Brevo OK — id {result.get('messageId')} → {', '.join(destinatarios)}")
        return True
    except Exception as e:
        print(f"[email] Falha ao enviar para {', '.join(destinatarios)}: {e}")
        return False


def _shell_email(titulo: str, subtitulo: str, corpo_html: str, rodape: str = None) -> str:
    """Envelope visual padrão (header da marca + footer) para e-mails do OPS.
    `rodape` troca a linha final -- o texto padrão fala de "resumo de fim de
    expediente", que não faz sentido em e-mail de aviso pontual."""
    rodape = rodape or "Resumo automático de fim de expediente · por favor não responda."
    return f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light only"></head>
<body style="margin:0;padding:0;background:#0E0B1F;-webkit-font-smoothing:antialiased;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0">{subtitulo}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EEF0F8;padding:36px 14px"><tr><td align="center">
    <table role="presentation" width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif">

      <!-- Header -->
      <tr><td bgcolor="#4C1FBF" style="border-radius:20px 20px 0 0;background:#4C1FBF;background:linear-gradient(135deg,#3A1B9E 0%,#6D28D9 52%,#9D24B8 100%);padding:30px 34px 28px">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="vertical-align:middle">
            <span style="display:inline-block;width:34px;height:34px;background:rgba(255,255,255,0.16);border-radius:9px;text-align:center;line-height:34px;font-size:17px;vertical-align:middle">🛰️</span>
            <span style="color:#fff;font-size:13px;font-weight:800;letter-spacing:1.6px;vertical-align:middle;margin-left:11px">FAISTON OPS</span>
          </td>
          <td align="right" style="vertical-align:middle"><span style="color:rgba(255,255,255,0.62);font-size:10.5px;font-weight:700;letter-spacing:1.3px">TORRE DE CONTROLE</span></td>
        </tr></table>
        <h1 style="color:#fff;margin:22px 0 0;font-size:26px;font-weight:800;letter-spacing:-0.6px">{titulo}</h1>
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:13px"><tr>
          <td style="background:rgba(255,255,255,0.15);border-radius:8px;padding:6px 13px">
            <span style="color:#fff;font-size:12.5px;font-weight:600;letter-spacing:.2px">{subtitulo}</span></td></tr></table>
      </td></tr>

      <!-- Body -->
      <tr><td bgcolor="#ffffff" style="background:#fff;padding:28px 30px 24px;border-left:1px solid #E7E9F2;border-right:1px solid #E7E9F2">{corpo_html}</td></tr>

      <!-- Footer -->
      <tr><td bgcolor="#ffffff" style="background:#fff;border-radius:0 0 20px 20px;border:1px solid #E7E9F2;border-top:none;padding:8px 30px 26px;text-align:center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="border-top:1px solid #EEF0F6;padding-top:20px;text-align:center">
          <span style="display:inline-block;width:28px;height:28px;background:#F2EEFE;border-radius:8px;text-align:center;line-height:28px;font-size:14px">🛰️</span>
          <p style="color:#6B7280;font-size:12px;font-weight:700;margin:9px 0 3px;letter-spacing:.2px">Faiston OPS · Torre de Controle</p>
          <p style="color:#AEB3C2;font-size:11px;margin:0;line-height:1.5">{rodape}</p>
        </td></tr></table>
      </td></tr>

    </table></td></tr></table></body></html>"""
