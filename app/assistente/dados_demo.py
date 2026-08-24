"""Dados de demonstração pra ver o Faiston OPS "com cara de uso" —
usuários fictícios e tarefas variadas (feitas, em andamento, abertas),
espalhadas nos tipos de atividade que já existem neste ambiente. Fora do
fluxo normal do assistente, chamado só pelo botão de teste em
/assistente/documentos (admin).

Exceção deliberada à regra 1 do CLAUDE.md do assistente ("o assistente
só lê"): isto é ferramenta de demonstração/QA, não capacidade exposta a
pergunta de usuário -- nenhuma capacidade conversacional chama nada
daqui. Todo usuário criado tem login prefixado `demo.` e todo cliente
das tarefas começa com "Cliente Demo", o que torna `limpar()` seguro:
só mexe no que tem esses marcadores.
"""
import random
from typing import List, Optional

import bcrypt

from app.assistente.db import get_conn

_PREFIXO_LOGIN = "demo."
_CLIENTES_DEMO = ["Cliente Demo Norte", "Cliente Demo Sul", "Cliente Demo Leste"]
_NOMES = [
    ("Ana Beatriz Souza", "ana.souza"),
    ("Bruno Carvalho", "bruno.carvalho"),
    ("Camila Ferreira", "camila.ferreira"),
    ("Diego Martins", "diego.martins"),
    ("Elisa Ramos", "elisa.ramos"),
    ("Felipe Andrade", "felipe.andrade"),
]
_TIMES_VALIDOS = ["Projetos", "Logística", "Rede Credenciada", "Desenvolvimento"]
_CARGOS_VALIDOS = ["analista", "backoffice", "n2", "desenvolvedor"]
_SENHA_DEMO = "Demo@1234"


def _hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def criar_usuarios(quantidade: int = 4) -> dict:
    """Cria até `quantidade` funcionários fictícios (login `demo.<nome>`,
    senha fixa Demo@1234 -- só pra login manual de teste, nunca enviada
    por e-mail de verdade). Pula quem já existe (reexecutar é seguro)."""
    quantidade = max(1, min(quantidade, len(_NOMES)))
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        criados = []
        ja_existiam = []
        for nome, base_login in _NOMES[:quantidade]:
            login = _PREFIXO_LOGIN + base_login
            cur.execute("SELECT id FROM usuarios WHERE usuario = %s", (login,))
            if cur.fetchone():
                ja_existiam.append(login)
                continue
            cargo = random.choice(_CARGOS_VALIDOS)
            time_ = random.choice(_TIMES_VALIDOS)
            cur.execute(
                """
                INSERT INTO usuarios (usuario, senha_hash, nome, perfil, ativo, email, time, cargo, primeiro_acesso)
                VALUES (%s, %s, %s, 'funcionario', true, %s, %s, %s, false)
                RETURNING id
                """,
                (login, _hash_senha(_SENHA_DEMO), nome, f"{login}@demo.faiston.teste", time_, cargo),
            )
            criados.append({"id": cur.fetchone()[0], "nome": nome, "usuario": login, "time": time_, "cargo": cargo})
        conn.commit()
        cur.close()
        conn.close()
        return {"criados": criados, "ja_existiam": ja_existiam}
    except Exception as e:
        print(f"[assistente/dados_demo] Erro criando usuários demo: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"erro": "falha_insercao", "detalhe": str(e)}


def criar_tarefas(usuario_ids: List[int], por_usuario: int = 7) -> dict:
    """Tarefas variadas pra cada usuario_id -- mistura de status (com
    peso pra 'concluido' ser o mais comum, como no uso real), tipo de
    atividade e cliente, espalhadas nos últimos 21 dias."""
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, peso FROM tipos_atividade WHERE ativo = true ORDER BY id")
        tipos = cur.fetchall()

        total = 0
        for usuario_id in usuario_ids:
            for _ in range(por_usuario):
                status = random.choices(
                    ["concluido", "em_andamento", "aberto"], weights=[45, 30, 25]
                )[0]
                cliente = random.choice(_CLIENTES_DEMO)
                dias_atras = random.randint(0, 21)
                if tipos:
                    tipo_id, tipo_nome, peso = random.choice(tipos)
                else:
                    tipo_id, tipo_nome, peso = None, "Atividade geral", 2
                descricao = f"{tipo_nome} — {cliente} (dado de demonstração)"
                segundos = int(peso) * 1800 if peso else 1800

                if status == "concluido":
                    duracao_dias = random.randint(0, min(dias_atras, 3))
                    cur.execute(
                        """
                        INSERT INTO tarefas
                            (usuario_id, descricao, cliente, status, segundos, tipo_atividade_id, peso,
                             criado_em, concluido_em, atualizado_em)
                        VALUES (
                            %s, %s, %s, 'concluido', %s, %s, %s,
                            NOW() - (%s || ' days')::interval,
                            NOW() - (%s || ' days')::interval,
                            NOW() - (%s || ' days')::interval
                        )
                        """,
                        (
                            usuario_id, descricao, cliente, segundos, tipo_id, peso,
                            dias_atras, max(dias_atras - duracao_dias, 0), max(dias_atras - duracao_dias, 0),
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO tarefas
                            (usuario_id, descricao, cliente, status, segundos, tipo_atividade_id, peso, criado_em)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() - (%s || ' days')::interval)
                        """,
                        (usuario_id, descricao, cliente, status, segundos, tipo_id, peso, dias_atras),
                    )
                total += 1

        conn.commit()
        cur.close()
        conn.close()
        return {"tarefas_criadas": total}
    except Exception as e:
        print(f"[assistente/dados_demo] Erro criando tarefas demo: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"erro": "falha_insercao", "detalhe": str(e)}


def popular(quantidade_usuarios: int = 4, tarefas_por_usuario: int = 7) -> dict:
    resultado_usuarios = criar_usuarios(quantidade_usuarios)
    if resultado_usuarios.get("erro"):
        return resultado_usuarios

    ids = [u["id"] for u in resultado_usuarios.get("criados", [])]
    if not ids:
        # ninguém novo criado (já existiam) -- ainda assim gera tarefa
        # pros usuários demo já existentes, pra reexecução não ficar sem efeito
        conn = get_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT id FROM usuarios WHERE usuario LIKE %s", (_PREFIXO_LOGIN + "%",))
                ids = [r[0] for r in cur.fetchall()]
                cur.close()
                conn.close()
            except Exception:
                pass

    resultado_tarefas = criar_tarefas(ids, tarefas_por_usuario) if ids else {"tarefas_criadas": 0}
    return {"usuarios": resultado_usuarios, "tarefas": resultado_tarefas}


def limpar() -> dict:
    """Remove as tarefas de demonstração (usuário demo. ou cliente
    "Cliente Demo*") e desativa (não apaga) os usuários demo -- evita
    lidar com toda referência a usuario_id espalhada pelo sistema
    (comentários, histórico, sessão, etc.), mesmo padrão de soft-delete
    que a coluna `ativo` já existe pra suportar."""
    conn = get_conn()
    if not conn:
        return {"erro": "banco_offline"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM tarefas
            WHERE cliente LIKE %s
               OR usuario_id IN (SELECT id FROM usuarios WHERE usuario LIKE %s)
            """,
            ("Cliente Demo%", _PREFIXO_LOGIN + "%"),
        )
        tarefas_removidas = cur.rowcount
        cur.execute("UPDATE usuarios SET ativo = false WHERE usuario LIKE %s", (_PREFIXO_LOGIN + "%",))
        usuarios_desativados = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {"sucesso": True, "tarefas_removidas": tarefas_removidas, "usuarios_desativados": usuarios_desativados}
    except Exception as e:
        print(f"[assistente/dados_demo] Erro limpando dados demo: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"erro": "falha_remocao"}
