"""Capacidade A — achar. Catálogo fixo de consultas expostas ao modelo
como ferramentas (function calling). Regra do CLAUDE.md do assistente:
nada de NL→SQL aberto — o modelo só escolhe QUAL função chamar e COM QUE
argumento; o backend valida o argumento e executa uma query 100% fixa e
parametrizada. Nenhuma string SQL é montada a partir de saída do modelo.

Comece pelas 5 consultas que mais aparecem no log das fases anteriores,
diz a especificação — este catálogo não teve acesso a esse log real
(ambiente de desenvolvimento sem banco), então as quatro abaixo foram
escolhidas pelo domínio (o que qualquer pessoa da operação perguntaria),
não por dado de uso real. Ajustar depois de olhar assistente_log de
verdade é esperado.
"""
from datetime import date
from typing import Optional

from app.assistente.db import get_conn

STATUS_TAREFA_VALIDOS = ("aberto", "em_andamento", "concluido")

_ROTULO_STATUS = {
    "aberto": "aberta(s)",
    "em_andamento": "em andamento",
    "concluido": "concluída(s)",
}


def minhas_tarefas(sess: dict, status: Optional[str] = None) -> dict:
    """Quantas tarefas a pessoa que perguntou tem, por status. Sempre
    sobre quem perguntou — nunca recebe usuario_id de fora. É assim que
    esta consulta cumpre a regra 5 (roda com a permissão de quem
    perguntou) sem precisar de checagem extra: ela literalmente não sabe
    consultar outra pessoa."""
    if status is not None and status not in STATUS_TAREFA_VALIDOS:
        raise ValueError(f"status inválido: {status!r}")
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        if status:
            cur.execute(
                "SELECT COUNT(*) FROM tarefas WHERE usuario_id = %s AND status = %s",
                (sess["id"], status),
            )
            resultado = {"status": status, "total": cur.fetchone()[0]}
        else:
            cur.execute(
                "SELECT status, COUNT(*) FROM tarefas WHERE usuario_id = %s GROUP BY status",
                (sess["id"],),
            )
            resultado = {"por_status": {r[0]: r[1] for r in cur.fetchall()}}
        cur.close()
        conn.close()
        return resultado
    except Exception as e:
        print(f"[assistente/catalogo] Erro em minhas_tarefas: {e}")
        return {"erro": "falha_consulta"}


def tarefas_por_cliente(sess: dict, cliente: str) -> dict:
    """Tarefas em aberto/andamento de um cliente específico."""
    cliente = (cliente or "").strip()
    if not cliente:
        raise ValueError("cliente vazio")
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, COUNT(*) FROM tarefas
            WHERE cliente ILIKE %s AND status != 'concluido'
            GROUP BY status
            """,
            (cliente,),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
        if not linhas:
            return {"cliente": cliente, "encontrado": False}
        return {"cliente": cliente, "encontrado": True, "por_status": {r[0]: r[1] for r in linhas}}
    except Exception as e:
        print(f"[assistente/catalogo] Erro em tarefas_por_cliente: {e}")
        return {"erro": "falha_consulta"}


def escala_n2_do_dia(sess: dict, data: str) -> dict:
    """Quem está de plantão N2 numa data (YYYY-MM-DD)."""
    try:
        data_convertida = date.fromisoformat(data)
    except (ValueError, TypeError):
        raise ValueError(f"data inválida: {data!r} (use YYYY-MM-DD)")
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT u.nome, e.horario_entrada, e.modalidade, e.atribuicao
            FROM escala_n2 e JOIN usuarios u ON u.id = e.n2_usuario_id
            WHERE e.data = %s
            ORDER BY e.horario_entrada
            """,
            (data_convertida,),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
        return {
            "data": data_convertida.isoformat(),
            "escala": [
                {
                    "nome": r[0],
                    "horario_entrada": r[1].strftime("%H:%M") if r[1] else None,
                    "modalidade": r[2],
                    "atribuicao": r[3],
                }
                for r in linhas
            ],
        }
    except Exception as e:
        print(f"[assistente/catalogo] Erro em escala_n2_do_dia: {e}")
        return {"erro": "falha_consulta"}


def atividades_campo_pendentes(sess: dict, dias: int = 7) -> dict:
    """status_atividades que não estão concluídas/canceladas há mais de N
    dias — pendência de despacho técnico esquecida."""
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        raise ValueError(f"dias inválido: {dias!r}")
    if not (1 <= dias <= 90):
        raise ValueError("dias precisa estar entre 1 e 90")
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT sa.id, c.nome, sa.site_nome, sa.status, sa.data
            FROM status_atividades sa
            LEFT JOIN clientes c ON c.id = sa.cliente_id
            WHERE sa.status NOT IN ('concluido', 'cancelado')
              AND sa.data < CURRENT_DATE - (%s || ' days')::interval
            ORDER BY sa.data
            LIMIT 20
            """,
            (dias,),
        )
        linhas = cur.fetchall()
        cur.close()
        conn.close()
        return {
            "dias": dias,
            "total": len(linhas),
            "atividades": [
                {
                    "id": r[0],
                    "cliente": r[1],
                    "site": r[2],
                    "status": r[3],
                    "data": r[4].isoformat() if r[4] else None,
                }
                for r in linhas
            ],
        }
    except Exception as e:
        print(f"[assistente/catalogo] Erro em atividades_campo_pendentes: {e}")
        return {"erro": "falha_consulta"}


CATALOGO = {
    "minhas_tarefas": minhas_tarefas,
    "tarefas_por_cliente": tarefas_por_cliente,
    "escala_n2_do_dia": escala_n2_do_dia,
    "atividades_campo_pendentes": atividades_campo_pendentes,
}

FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "minhas_tarefas",
            "description": (
                "Quantas tarefas a pessoa que está perguntando tem no Faiston OPS, "
                "por status (aberto, em_andamento, concluido). Sempre sobre quem "
                "pergunta, nunca sobre outra pessoa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(STATUS_TAREFA_VALIDOS),
                        "description": "Filtra por um status específico. Omitir pra trazer a contagem de todos.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tarefas_por_cliente",
            "description": "Quantas tarefas em aberto ou em andamento um cliente específico tem no Faiston OPS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente": {"type": "string", "description": "Nome do cliente, como aparece no cadastro."}
                },
                "required": ["cliente"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escala_n2_do_dia",
            "description": "Quem está escalado no plantão N2 (suporte de campo) numa data específica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data no formato YYYY-MM-DD."}
                },
                "required": ["data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "atividades_campo_pendentes",
            "description": (
                "Atividades de despacho técnico de campo que estão paradas "
                "(não concluídas nem canceladas) há mais de N dias."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dias": {
                        "type": "integer",
                        "description": "Quantidade mínima de dias parada. Padrão 7.",
                        "minimum": 1,
                        "maximum": 90,
                    }
                },
                "required": [],
            },
        },
    },
]


def executar(nome_funcao: str, argumentos: dict, sess: dict):
    """Único ponto de execução: valida que a função existe no catálogo
    (nunca roda nada que o modelo inventou) e repassa os argumentos.
    ValueError de validação dentro da função sobe pra quem chamou tratar
    como 'não entendi' — nunca vira erro 500 pro usuário."""
    funcao = CATALOGO.get(nome_funcao)
    if funcao is None:
        raise ValueError(f"função não existe no catálogo: {nome_funcao!r}")
    return funcao(sess, **argumentos)
