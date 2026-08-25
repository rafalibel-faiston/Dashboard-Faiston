"""Capacidade D — detectores. Regra 9 do CLAUDE.md do assistente: a
detecção de padrão nunca passa pelo modelo — contagem, mediana,
percentil e comparação são SQL puro. Este módulo não importa nada de
llm.py (garantido por teste em test_assistente_observar.py) — o modelo
só entra depois, em redacao.py, pra escrever o aviso em cima do que já
foi detectado aqui.

`atividade`, no sentido da especificação original, já existe neste
sistema em duas tabelas — `tarefas` (trabalho de projeto/backoffice) e
`status_atividades` (despacho técnico de campo) — em vez de criar uma
tabela genérica nova (regra: não duplicar o que já existe).

Assinatura (o que define "a mesma tarefa de novo") por detector — é o
parâmetro que mais afeta a qualidade da detecção, por isso documentado
aqui em vez de espalhado no código:

- `repeticao_identica`: tipo de atividade + cliente. Mesmo trabalho pro
  mesmo cliente, repetido — candidato a virar lote/automação.
- `retrabalho`: a tarefa em si (tarefa_id). A mesma tarefa editada muitas
  vezes é sinal de retrabalho, corrigindo algo que devia ter saído certo
  da primeira vez.
- `pendencia_parada`: tipo de atividade. Compara a idade da tarefa aberta
  com o histórico (percentil 90) do mesmo tipo — "isso já devia ter
  saído".

Cada função devolve uma lista de achados, cada achado com `peso` (um
número em segundos-equivalente, pra comparar achados de detectores
diferentes na hora de aplicar o orçamento de interrupção em job.py) e
`evidencia` (o que vai pro modelo redigir e pro campo JSONB da tabela).
"""
from typing import List, Optional

from app.assistente.db import get_conn


def repeticao_identica(dias: int = 5, minimo: int = 5) -> List[dict]:
    """Mesmo tipo de atividade pro mesmo cliente, N+ vezes na janela."""
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.usuario_id, ta.nome, t.cliente,
                   COUNT(*) AS vezes,
                   MIN(t.criado_em) AS primeira,
                   MAX(t.criado_em) AS ultima,
                   COALESCE(SUM(t.segundos), 0) AS tempo_total_s
            FROM tarefas t
            JOIN tipos_atividade ta ON ta.id = t.tipo_atividade_id
            WHERE t.criado_em > now() - (%s || ' days')::interval
              AND t.tipo_atividade_id IS NOT NULL
              AND t.cliente IS NOT NULL AND t.cliente != ''
            GROUP BY t.usuario_id, ta.nome, t.cliente
            HAVING COUNT(*) >= %s
            ORDER BY COUNT(*) DESC
            """,
            (dias, minimo),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/detectores] Erro em repeticao_identica: {e}")
        return []

    achados = []
    for usuario_id, tipo, cliente, vezes, primeira, ultima, tempo_total_s in linhas:
        achados.append({
            "usuario_id": usuario_id,
            "detector": "repeticao_identica",
            "assinatura": f"{tipo}::{cliente}",
            "peso": float(tempo_total_s),
            "evidencia": {
                "tipo": tipo,
                "cliente": cliente,
                "vezes": vezes,
                "janela_dias": dias,
                "primeira": primeira.isoformat() if primeira else None,
                "ultima": ultima.isoformat() if ultima else None,
                "tempo_total_s": tempo_total_s,
            },
        })
    return achados


def retrabalho(dias: int = 7, minimo: int = 4) -> List[dict]:
    """A mesma tarefa editada muitas vezes na janela — sinal de
    retrabalho (reabrindo pra corrigir algo)."""
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT h.autor_id, h.tarefa_id, h.tarefa_desc,
                   COUNT(*) AS vezes,
                   MIN(h.criado_em) AS primeira,
                   MAX(h.criado_em) AS ultima
            FROM tarefa_historico h
            WHERE h.criado_em > now() - (%s || ' days')::interval
              AND h.acao = 'editou'
              AND h.autor_id IS NOT NULL
              AND h.tarefa_id IS NOT NULL
            GROUP BY h.autor_id, h.tarefa_id, h.tarefa_desc
            HAVING COUNT(*) >= %s
            ORDER BY COUNT(*) DESC
            """,
            (dias, minimo),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/detectores] Erro em retrabalho: {e}")
        return []

    achados = []
    for usuario_id, tarefa_id, descricao, vezes, primeira, ultima in linhas:
        achados.append({
            "usuario_id": usuario_id,
            "detector": "retrabalho",
            "assinatura": f"tarefa::{tarefa_id}",
            # sem duração por edição registrada -- usa a contagem como
            # proxy de peso (30min-equivalente por edição, arbitrário mas
            # razoável pra comparar com os outros detectores).
            "peso": float(vezes * 1800),
            "evidencia": {
                "tarefa_id": tarefa_id,
                "descricao": descricao,
                "vezes": vezes,
                "janela_dias": dias,
                "primeira": primeira.isoformat() if primeira else None,
                "ultima": ultima.isoformat() if ultima else None,
            },
        })
    return achados


def pendencia_parada(dias_historico: int = 90, minimo_amostras: int = 5) -> List[dict]:
    """Tarefa aberta/em andamento há mais tempo que o percentil 90
    histórico de conclusão do mesmo tipo — passou do normal pra aquele
    tipo de trabalho. Usa percentil, nunca média (duração de tarefa tem
    cauda longa)."""
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH normal AS (
                SELECT tipo_atividade_id,
                       percentile_cont(0.9) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (concluido_em - criado_em))
                       ) AS p90_segundos
                FROM tarefas
                WHERE status = 'concluido'
                  AND concluido_em IS NOT NULL
                  AND tipo_atividade_id IS NOT NULL
                  AND criado_em > now() - (%s || ' days')::interval
                GROUP BY tipo_atividade_id
                HAVING COUNT(*) >= %s
            )
            SELECT t.usuario_id, t.id, t.descricao, ta.nome,
                   EXTRACT(EPOCH FROM (now() - t.criado_em)) AS parada_ha_s,
                   n.p90_segundos
            FROM tarefas t
            JOIN normal n ON n.tipo_atividade_id = t.tipo_atividade_id
            JOIN tipos_atividade ta ON ta.id = t.tipo_atividade_id
            WHERE t.status IN ('aberto', 'em_andamento')
              AND EXTRACT(EPOCH FROM (now() - t.criado_em)) > n.p90_segundos
            ORDER BY parada_ha_s DESC
            """,
            (dias_historico, minimo_amostras),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[assistente/detectores] Erro em pendencia_parada: {e}")
        return []

    achados = []
    for usuario_id, tarefa_id, descricao, tipo, parada_ha_s, p90_segundos in linhas:
        atraso_s = float(parada_ha_s) - float(p90_segundos)
        achados.append({
            "usuario_id": usuario_id,
            "detector": "pendencia_parada",
            "assinatura": f"tipo::{tipo}",
            "peso": atraso_s,
            "evidencia": {
                "tarefa_id": tarefa_id,
                "descricao": descricao,
                "tipo": tipo,
                "parada_ha_dias": round(float(parada_ha_s) / 86400, 1),
                "normal_dias": round(float(p90_segundos) / 86400, 1),
            },
        })
    return achados


def rodar_todos(usuario_id: Optional[int] = None) -> List[dict]:
    """Roda os três detectores. `usuario_id` filtra o resultado (não a
    query) — útil pra depuração manual de uma pessoa específica; o job
    diário roda sem filtro, pra todo mundo de uma vez."""
    achados = repeticao_identica() + retrabalho() + pendencia_parada()
    if usuario_id is not None:
        achados = [a for a in achados if a["usuario_id"] == usuario_id]
    return achados
