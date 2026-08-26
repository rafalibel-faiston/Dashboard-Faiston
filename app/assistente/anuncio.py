"""Anúncio por e-mail do assistente OPS pra equipe.

Fora do fluxo conversacional: nada aqui é chamado por capacidade do
assistente, só pela tela de admin (`/assistente/documentos`). É a única
parte do módulo que manda coisa pra fora do sistema, então o desenho é
conservador de propósito:

- a lista de destinatários é limitada a quem REALMENTE vê o widget
  (perfis do piloto). Avisar quem não tem acesso só gera frustração;
- o texto é montado a partir do que está de fato ligado no momento do
  envio (`estado_das_capacidades`) -- sem base de procedimentos indexada,
  o e-mail não promete "tire dúvidas de procedimento", porque isso ainda
  responderia "não encontrei na base";
- envio real exige confirmação explícita no router, e fica registrado
  pra não disparar duas vezes sem querer.
"""
import os
from datetime import datetime
from typing import List, Optional

from app.assistente.db import get_conn


def estado_das_capacidades() -> dict:
    """O que está pronto pra ser usado AGORA. Alimenta o corpo do e-mail
    pra ele não anunciar capacidade que ainda responderia vazio."""
    estado = {
        "documentos": 0,
        "etapas_onboarding": 0,
        "carimbos": 0,
        "observar_ligado": os.environ.get("ASSISTENTE_OBSERVAR_ENABLED", "0") == "1",
    }
    conn = get_conn()
    if not conn:
        return estado
    try:
        cur = conn.cursor()
        for chave, sql in (
            ("documentos", "SELECT COUNT(*) FROM documento WHERE ativo = true"),
            ("etapas_onboarding",
             "SELECT COUNT(*) FROM documento WHERE ordem_onboarding IS NOT NULL AND ativo = true"),
            ("carimbos", "SELECT COUNT(*) FROM carimbos"),
        ):
            try:
                cur.execute(sql)
                estado[chave] = cur.fetchone()[0]
            except Exception:
                # tabela pode não existir ainda neste ambiente -- conta 0 e
                # segue, sem derrubar o resto do diagnóstico
                conn.rollback()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/anuncio] Erro lendo estado das capacidades: {e}")
    return estado


def destinatarios(perfis_piloto) -> List[dict]:
    """Só quem enxerga o widget: ativo, com e-mail, e num perfil do
    piloto. `perfis_piloto` vem do router (mesma env var que o gate de
    acesso usa), pra lista e acesso nunca divergirem."""
    perfis = sorted(p for p in (perfis_piloto or set()) if p)
    if not perfis:
        return []
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, nome, email, perfil
            FROM usuarios
            WHERE ativo = TRUE
              AND COALESCE(email, '') <> ''
              AND perfil = ANY(%s)
            ORDER BY nome
            """,
            (perfis,),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
        return [{"id": r[0], "nome": r[1], "email": r[2], "perfil": r[3]} for r in linhas]
    except Exception as e:
        print(f"[assistente/anuncio] Erro montando destinatários: {e}")
        return []


def _primeiro_nome(nome: str) -> str:
    return (nome or "").strip().split(" ")[0] or "olá"


def montar_corpo(nome: str, estado: dict, sistema_url: str) -> str:
    """Corpo HTML do e-mail. Tabelas + estilo inline porque cliente de
    e-mail (Outlook principalmente) ignora CSS externo e flex/grid."""
    url = (sistema_url or "").rstrip("/") or "https://ops.faiston.com.br"

    itens = [
        ("📊", "Resumo da sua semana",
         "Quantas tarefas você fechou, quanto tempo levou e o que ficou em aberto. "
         "É só pedir “resumo da semana”."),
        ("🔎", "Suas tarefas, sem procurar",
         "“Quantas tarefas eu tenho em aberto?”, “o que tem pendente no cliente X?” — "
         "ele responde na hora, olhando só o que é seu."),
        ("☀️", "Um alô no começo do dia",
         "Na primeira vez que você entra no sistema, ele resume o que você fechou ontem "
         "e pergunta o plano de hoje. Se você responder, ele lembra disso amanhã."),
    ]
    if estado.get("carimbos", 0) > 0:
        itens.append(("📋", "Seus carimbos na mão",
                      "“Me manda o carimbo de encerramento” e ele acha o texto pronto "
                      "que a equipe já cadastrou, do jeitinho que está lá."))
    if estado.get("documentos", 0) > 0:
        itens.append(("📚", "Dúvida de procedimento",
                      "Pergunte como se faz alguma coisa e ele responde com base nos "
                      "procedimentos internos, mostrando de onde tirou a resposta."))
    if estado.get("etapas_onboarding", 0) > 0:
        itens.append(("🎓", "Onboarding guiado",
                      "Chegou agora no time? Peça “quero começar o onboarding” e ele "
                      "ensina passo a passo, no seu ritmo."))

    linhas_itens = "".join(
        f"""
        <tr><td style="padding:0 0 16px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td width="34" style="vertical-align:top;font-size:19px;line-height:1.35">{icone}</td>
            <td style="vertical-align:top">
              <div style="color:#151720;font-size:14.5px;font-weight:700;margin:0 0 3px">{titulo}</div>
              <div style="color:#3f4661;font-size:13.5px;line-height:1.6">{texto}</div>
            </td>
          </tr></table>
        </td></tr>"""
        for icone, titulo, texto in itens
    )

    return f"""
        <p style="color:#151720;font-size:15px;margin:0 0 6px;line-height:1.6">
          Oi, {_primeiro_nome(nome)}!
        </p>
        <p style="color:#3f4661;font-size:14.5px;margin:0 0 22px;line-height:1.65">
          O Faiston OPS ganhou um assistente. Ele fica num botão redondo no canto
          inferior direito de qualquer tela — clique nele (ou aperte <strong>Alt+A</strong>)
          e pergunte o que quiser, com suas palavras mesmo.
        </p>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="background:#f6f7fb;border:1px solid #e6e9f4;border-radius:14px;padding:20px 20px 6px;margin:0 0 22px">
          <tr><td>
            <div style="color:#7a839c;font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;margin:0 0 14px">
              O que ele já faz por você
            </div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{linhas_itens}</table>
          </td></tr>
        </table>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px">
          <tr><td align="center" bgcolor="#5B2EE0"
                  style="border-radius:12px;background:#5B2EE0;background:linear-gradient(135deg,#5B2EE0,#B826C9)">
            <a href="{url}" style="display:block;color:#ffffff;text-decoration:none;padding:15px 24px;font-weight:700;font-size:15px;border-radius:12px">
              Abrir o Faiston OPS &nbsp;&rarr;
            </a>
          </td></tr>
        </table>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-left:3px solid #B826C9;background:#faf7ff;border-radius:0 10px 10px 0;padding:14px 16px;margin:0 0 6px">
          <tr><td>
            <div style="color:#151720;font-size:13px;font-weight:700;margin:0 0 4px">Só pra deixar claro</div>
            <div style="color:#3f4661;font-size:13px;line-height:1.6">
              Ele enxerga <strong>só o que é seu</strong> — suas tarefas, seus números.
              Ninguém vê os seus pelo assistente, e você não vê os de ninguém.
              Ele também não cria nem muda nada no sistema: só lê e responde.
            </div>
          </td></tr>
        </table>

        <p style="color:#7a839c;font-size:12.5px;margin:18px 0 0;line-height:1.6">
          Ainda estamos ajustando. Se ele errar, responder estranho ou faltar alguma
          coisa que te ajudaria, use o 👍/👎 embaixo da resposta ou fale com a gente —
          é assim que ele melhora.
        </p>
    """


def registrar_envio(quantidade: int, por_usuario_id: int) -> None:
    """Deixa rastro de que o anúncio já saiu, pra tela de admin avisar
    antes de alguém disparar de novo."""
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS assistente_anuncio_envio (
                id           BIGSERIAL PRIMARY KEY,
                enviado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
                quantidade   INTEGER NOT NULL,
                por_usuario  INTEGER REFERENCES usuarios(id)
            )
            """
        )
        cur.execute(
            "INSERT INTO assistente_anuncio_envio (quantidade, por_usuario) VALUES (%s, %s)",
            (quantidade, por_usuario_id),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/anuncio] Erro registrando envio: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


def ultimo_envio() -> Optional[dict]:
    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.enviado_em, e.quantidade, COALESCE(u.nome, '')
            FROM assistente_anuncio_envio e
            LEFT JOIN usuarios u ON u.id = e.por_usuario
            ORDER BY e.enviado_em DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {"enviado_em": row[0].isoformat() if row[0] else None,
                "quantidade": row[1], "por": row[2]}
    except Exception:
        # tabela ainda não existe = nunca enviou
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None
