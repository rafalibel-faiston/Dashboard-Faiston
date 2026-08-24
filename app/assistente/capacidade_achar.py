"""Capacidade A — achar. O modelo escolhe qual função do catálogo chamar
e com que argumento (function calling); o backend valida e executa. O
texto final é montado por código, não pelo modelo — regra 3 do CLAUDE.md
do assistente (o modelo nunca calcula/reafirma número): aqui ele nem
chega a tentar, o número vai direto do resultado da consulta pro texto.
"""
import json
from pathlib import Path
from typing import Optional, Tuple

from app.assistente.catalogo import FERRAMENTAS, executar
from app.assistente.llm import completar_com_ferramentas

_DIR = Path(__file__).resolve().parent
_PROMPT_ACHAR = (_DIR / "prompts" / "sistema_achar.md").read_text(encoding="utf-8")

_ROTULO_STATUS = {
    "aberto": "aberta(s)",
    "em_andamento": "em andamento",
    "concluido": "concluída(s)",
}


async def identificar_e_executar(pergunta: str, sess: dict) -> Optional[Tuple[str, dict, object]]:
    """Devolve (nome_funcao, resultado, usage) se o modelo escolheu uma
    ferramenta (válida ou não — `nome_funcao` vira `"_invalido"` se a
    função não existe ou os argumentos não validam) e `None` se o modelo
    não chamou nenhuma ferramenta — a pergunta não é do tipo "achar", o
    router tenta outra capacidade. `ModeloIndisponivel` sobe direto pra
    quem chamou tratar (mesma falha de qualquer outra chamada ao modelo)."""
    mensagens = [
        {"role": "system", "content": _PROMPT_ACHAR},
        {"role": "user", "content": pergunta},
    ]
    resp = await completar_com_ferramentas(mensagens, FERRAMENTAS)
    escolha = resp.choices[0].message
    tool_calls = getattr(escolha, "tool_calls", None)
    if not tool_calls:
        return None

    chamada = tool_calls[0]
    nome_funcao = chamada.function.name
    try:
        argumentos = json.loads(chamada.function.arguments or "{}")
        if not isinstance(argumentos, dict):
            raise ValueError("argumentos não são um objeto JSON")
    except (json.JSONDecodeError, ValueError):
        return ("_invalido", {}, resp.usage)

    try:
        resultado = executar(nome_funcao, argumentos, sess)
    except (ValueError, TypeError):
        return ("_invalido", {}, resp.usage)

    return (nome_funcao, resultado, resp.usage)


def formatar_resposta(nome_funcao: str, resultado: dict) -> str:
    """Texto final, montado por código — nunca pelo modelo."""
    if nome_funcao == "_invalido":
        return "Não consegui entender qual informação você precisa."

    if resultado.get("erro") in ("banco_offline", "falha_consulta"):
        return "Não consegui acessar os dados agora. Tente de novo em instantes."

    if nome_funcao == "minhas_tarefas":
        if "status" in resultado:
            rotulo = _ROTULO_STATUS.get(resultado["status"], resultado["status"])
            return f'Você tem {resultado["total"]} tarefa(s) {rotulo}.'
        por_status = resultado.get("por_status") or {}
        if not por_status:
            return "Você não tem nenhuma tarefa registrada."
        partes = [f"{v} {_ROTULO_STATUS.get(k, k)}" for k, v in por_status.items()]
        return "Suas tarefas: " + ", ".join(partes) + "."

    if nome_funcao == "tarefas_por_cliente":
        if not resultado.get("encontrado"):
            return f'Não encontrei tarefas em aberto para o cliente "{resultado.get("cliente", "")}".'
        por_status = resultado.get("por_status") or {}
        partes = [f"{v} {_ROTULO_STATUS.get(k, k)}" for k, v in por_status.items()]
        return f'{resultado["cliente"]} tem: ' + ", ".join(partes) + "."

    if nome_funcao == "escala_n2_do_dia":
        escala = resultado.get("escala") or []
        if not escala:
            return f'Não há ninguém escalado no N2 em {resultado.get("data", "")}.'
        linhas = [
            f"- {item['nome']}"
            + (f" ({item['horario_entrada']}, {item['modalidade']})" if item.get("horario_entrada") else "")
            for item in escala
        ]
        return f'Escala N2 de {resultado["data"]}:\n' + "\n".join(linhas)

    if nome_funcao == "atividades_campo_pendentes":
        atividades = resultado.get("atividades") or []
        if not atividades:
            return f'Nenhuma atividade de campo parada há mais de {resultado.get("dias", "?")} dias.'
        linhas = [
            f"- {a['cliente'] or 'cliente não informado'} · {a['site'] or 's/ site'} · "
            f"{a['status']} · desde {a['data']}"
            for a in atividades
        ]
        return (
            f'{resultado["total"]} atividade(s) de campo parada(s) há mais de '
            f'{resultado["dias"]} dias:\n' + "\n".join(linhas)
        )

    return "Não consegui montar a resposta."
