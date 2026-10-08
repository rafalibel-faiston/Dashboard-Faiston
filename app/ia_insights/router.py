"""IA Insights.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
import os

from fastapi import APIRouter, Cookie, HTTPException

from app.core.auth import get_session
from app.core.db import get_db
from app.notificacoes.router import criar_notificacao


router = APIRouter()


# --- IA INSIGHTS ---
@router.post("/api/ia/insights")
def gerar_insights_ia(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"):
        raise HTTPException(status_code=403, detail="Acesso negado")

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY não configurada")

    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")

    try:
        cur = conn.cursor()

        # Coleta dados para análise
        cur.execute("""
            SELECT u.nome,
                COUNT(t.id) as total,
                SUM(CASE WHEN t.status='aberto' THEN 1 ELSE 0 END) as abertos,
                SUM(CASE WHEN t.status='em_andamento' THEN 1 ELSE 0 END) as andamento,
                SUM(CASE WHEN t.status='concluido' THEN 1 ELSE 0 END) as concluidos,
                ROUND(AVG(t.segundos)/3600.0, 1) as media_horas
            FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
            WHERE u.perfil = 'funcionario' AND u.ativo = TRUE
            GROUP BY u.nome ORDER BY total DESC
        """)
        por_func = cur.fetchall()

        cur.execute("""
            SELECT cliente, COUNT(*) as total,
                SUM(CASE WHEN status='aberto' THEN 1 ELSE 0 END) as abertos,
                SUM(CASE WHEN status='concluido' THEN 1 ELSE 0 END) as concluidos
            FROM tarefas GROUP BY cliente ORDER BY total DESC LIMIT 10
        """)
        por_cliente = cur.fetchall()

        cur.execute("""
            SELECT prioridade, COUNT(*) FROM tarefas WHERE status != 'concluido'
            GROUP BY prioridade
        """)
        por_prioridade = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute("SELECT COUNT(*) FROM tarefas WHERE status='aberto' AND criado_em < NOW() - INTERVAL '7 days'")
        tickets_antigos = cur.fetchone()[0]

        cur.close(); conn.close()

        # Monta contexto para IA
        func_lines = "\n".join([
            f"- {r[0]}: {r[1]} tickets total, {r[2]} abertos, {r[3]} em andamento, {r[4]} concluídos, média {r[5]}h por ticket"
            for r in por_func
        ]) or "Nenhum dado"

        cliente_lines = "\n".join([
            f"- {r[0]}: {r[1]} tickets, {r[2]} abertos, {r[3]} concluídos"
            for r in por_cliente
        ]) or "Nenhum dado"

        criticos = por_prioridade.get('Critica', 0)
        altos = por_prioridade.get('Alta', 0)
        medios = por_prioridade.get('Media', 0)
        prompt = f"""Você é um assistente de operações da empresa Faiston. Analise APENAS os dados abaixo e gere exatamente 3 insights em português. Use somente os números fornecidos, não invente valores.

DADOS POR FUNCIONÁRIO (nome: total tickets, abertos, em andamento, concluídos):
{func_lines}

DADOS POR CLIENTE (cliente: total tickets, abertos, concluídos):
{cliente_lines}

RESUMO GERAL:
- Tickets abertos há mais de 7 dias (atrasados): {tickets_antigos}
- Tickets com prioridade CRÍTICA ainda abertos/em andamento: {criticos}
- Tickets com prioridade ALTA ainda abertos/em andamento: {altos}
- Tickets com prioridade MÉDIA ainda abertos/em andamento: {medios}

REGRAS:
- Use apenas os números acima, nunca some categorias diferentes
- Use os nomes reais dos funcionários
- Cada insight em uma linha, começando com emoji
- Máximo 130 caracteres por insight
- Foque nos problemas mais graves primeiro"""

        import json as _json, http.client, ssl
        body_json = _json.dumps({
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.7
        })
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection("api.groq.com", timeout=25, context=ctx)
        conn.request("POST", "/openai/v1/chat/completions", body=body_json, headers={
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json",
            "User-Agent": "python-httpx/0.27",
            "Accept": "application/json",
        })
        resp = conn.getresponse()
        resp_body = resp.read().decode()
        conn.close()
        if resp.status != 200:
            print(f"[ia/insights] Groq {resp.status}: {resp_body}")
            raise HTTPException(status_code=500, detail=f"Groq {resp.status}: {resp_body}")
        result = _json.loads(resp_body)

        texto = result["choices"][0]["message"]["content"].strip()
        insights = [l.strip() for l in texto.split("\n") if l.strip()][:3]

        # Salva como notificações
        conn2 = get_db()
        if conn2:
            for insight in insights:
                criar_notificacao(conn2, "ia_insight", insight)
            conn2.commit()
            conn2.close()

        return {"insights": insights}

    except Exception as e:
        import traceback
        print(f"[ia/insights] ERRO: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
