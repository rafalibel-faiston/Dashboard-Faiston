"""Novidades do sistema.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from typing import List

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app.core.auth import get_session
from app.core.db import get_db
from app.core.paginas import _HTML_SEM_CACHE


router = APIRouter()


# --- NOVIDADES DO SISTEMA (2026-09-28) ---
# Página /novidades com o histórico de atualizações do Faiston OPS + aviso
# (popup) na primeira vez que a pessoa abre o sistema depois de uma novidade
# nova. "Já vi" fica no banco por usuário (novidades_vistas), não no
# localStorage -- vale entre computadores e navegadores diferentes.
# O histórico abaixo é a carga inicial (ON CONFLICT pelo slug, não
# sobrescreve edição); novidades novas o admin publica pela própria página.
NOVIDADES_SEED = [
    ("2026-10-07-tour-gestor", "2026-10-07 10:00", "novidade", "Gestores",
     "Tour guiado para gestores",
     "Gestor novo ganha um tour na própria tela no primeiro acesso: como criar tarefas para o time e como funciona a operação do N2.",
     ["Rever o tour e abrir o Guia de uso ficam no menu Gestão, sempre à mão.",
      "N2 e Cronograma de Atividades voltaram ao menu.",
      "O Guia de uso ganhou a seção Operação do N2, com escala, atividades de campo e Status Report."]),
    ("2026-09-28-timer-varias-tarefas", "2026-09-28 09:01", "novidade", "Todos",
     "Timer em várias tarefas ao mesmo tempo",
     "Agora dá pra deixar o timer rodando em mais de uma tarefa ao mesmo tempo, e ele não para mais quando você recarrega ou fecha a página.",
     ["Clique em Iniciar em quantas tarefas precisar: iniciar uma não pausa as outras.",
      "O tempo continua contando mesmo com a aba fechada ou o computador trocado.",
      "No topo da tela aparece quantas tarefas estão rodando e o tempo somado."]),
    ("2026-09-28-estabilidade-tarefas", "2026-09-28 09:00", "correcao", "Todos",
     "Sistema mais estável: fim das travadas e das tarefas sumindo",
     "Corrigimos os problemas que faziam o sistema travar, a tarefa sumir ou não deixar finalizar.",
     ["O sistema não fica mais travado esperando: se algo demorar, aparece uma mensagem pra tentar de novo.",
      "Concluir, arrastar ou iniciar uma tarefa não apaga mais o projeto nem o horário do prazo dela.",
      "Editar tarefa com ajudante não quebra mais o quadro.",
      "A tarefa concluída aparece na hora no grupo 'Hoje' da coluna Concluído.",
      "Arrastar uma tarefa atrasada pra Concluído agora pede o motivo do atraso, em vez de dar erro."]),
    ("2026-09-09-suporte-conversa", "2026-09-09 09:00", "melhoria", "Todos",
     "Suporte virou conversa",
     "A resposta do time de desenvolvimento às suas solicitações de suporte agora chega por e-mail, e você responde pelo próprio sistema.",
     ["Acompanhe tudo em Suporte → Minhas solicitações.",
      "Cada solicitação vira uma conversa, com o histórico de mensagens."]),
    ("2026-08-27-sinal-carga", "2026-08-27 09:00", "novidade", "Todos",
     "Sua carga de trabalho no quadro",
     "O quadro mostra a sua carga agora (Tranquila, Pesada, Atolada), somando o peso das tarefas em aberto e em andamento.",
     ["Tarefas atrasadas pesam mais na conta.",
      "O líder enxerga a mesma sinalização no quadro dele, pra ajudar a combinar prioridades."]),
    ("2026-08-26-assistente-ops", "2026-08-26 09:00", "novidade", "Todos",
     "Chegou o Assistente OPS",
     "O ícone roxo no canto da tela é o Assistente OPS: pergunte sobre as suas tarefas, ache um carimbo de atendimento ou peça o resumo da sua semana.",
     ["Acha carimbos e mostra suas tarefas em linguagem natural.",
      "Resume a sua semana e explica procedimentos.",
      "Faz um check-in rápido no primeiro acesso do dia."]),
    ("2026-08-23-projeto-cliente-opcional", "2026-08-23 09:00", "melhoria", "Gestores",
     "Cadastro de projeto mais simples",
     "O cliente virou campo opcional no cadastro de projeto, e cada time tem os próprios projetos.",
     []),
    ("2026-08-21-alerta-fim-expediente", "2026-08-21 09:00", "novidade", "Todos",
     "Alerta de pendências no fim do expediente",
     "No fim do dia você recebe um e-mail e uma notificação com as tarefas que ainda estão pendentes, com botão direto pro seu quadro.",
     ["A exportação de tarefas agora traz data e hora separadas, o peso e o motivo do atraso."]),
    ("2026-08-17-analista-atribui", "2026-08-17 09:00", "melhoria", "Analistas",
     "Analista pode atribuir tarefa ao Backoffice",
     "Ao criar uma tarefa, o analista pode escolher um assistente de Backoffice do mesmo time como responsável.",
     []),
    ("2026-08-11-cadastro-tecnicos", "2026-08-11 09:00", "novidade", "N2",
     "Cadastro de técnicos no N2",
     "Busca de técnicos por UF e botão 'Ver dados do técnico' direto na atividade.",
     ["Os motivos de atraso aparecem ao clicar em 'Fora do prazo' no gráfico de Aderência."]),
    ("2026-08-10-acesso-por-email", "2026-08-10 09:00", "melhoria", "Todos",
     "Acesso pelo e-mail e 'Esqueci minha senha'",
     "Cada pessoa define a própria senha pelo link que chega no e-mail, e dá pra redefinir sozinho em 'Esqueci minha senha'.",
     []),
    ("2026-08-03-concluidas-por-dia", "2026-08-03 09:00", "melhoria", "Todos",
     "Tarefas concluídas separadas por dia",
     "A coluna Concluído do Kanban agrupa as tarefas pelo dia em que foram concluídas.",
     ["Gestores ganharam o resumo completo por pessoa e por dia.",
      "O Status Report ganhou a visão em Kanban."]),
    ("2026-07-30-suporte-tour-n2", "2026-07-30 09:00", "novidade", "Todos",
     "Solicitação de suporte e tour guiado",
     "O botão de boia (Suporte) abre uma solicitação direto pro time de desenvolvimento. O N2 ganhou um tour guiado da tela.",
     ["Senhas mais fortes e mais proteção no login."]),
    ("2026-07-29-carimbo-encerramento", "2026-07-29 09:00", "melhoria", "Todos",
     "Carimbo de encerramento ao clicar",
     "Clicar numa atividade finalizada (no Kanban ou no Histórico) mostra o carimbo de encerramento.",
     []),
    ("2026-07-23-comentarios-drawer", "2026-07-23 09:00", "melhoria", "Todos",
     "Comentários em painel lateral",
     "Os comentários de cada tarefa abrem num painel ao lado, sem sair do quadro.",
     []),
    ("2026-07-20-status-campo", "2026-07-20 09:00", "novidade", "Projetos",
     "Módulo Status de Campo",
     "Acompanhamento dos despachos técnicos (ARCOS, ZAMP, Câmeras IP) com andamento e encerramento.",
     []),
    ("2026-06-22-aba-projetos", "2026-06-22 09:00", "novidade", "Todos",
     "Aba Projetos no seu quadro e guia por perfil",
     "Quem é responsável por projeto acompanha tudo pela aba Projetos, e cada perfil tem um guia de uso próprio no primeiro acesso.",
     []),
]
_NOVIDADES_CATEGORIAS = ("novidade", "melhoria", "correcao")
# Quem nunca abriu a página (ou é novo) só recebe o aviso das novidades
# recentes -- não o histórico inteiro de uma vez.
_NOVIDADES_JANELA_DIAS = 14

def _migrar_novidades():
    conn = get_db()
    if not conn: return
    try:
        import json as _json
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS novidades (
                id SERIAL PRIMARY KEY,
                slug VARCHAR(120) UNIQUE,
                titulo VARCHAR(200) NOT NULL,
                resumo TEXT NOT NULL DEFAULT '',
                itens JSONB NOT NULL DEFAULT '[]'::jsonb,
                categoria VARCHAR(20) NOT NULL DEFAULT 'novidade',
                publico VARCHAR(60) NOT NULL DEFAULT 'Todos',
                publicado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                autor_nome VARCHAR(120) NOT NULL DEFAULT '',
                ativo BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS novidades_vistas (
                usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id) ON DELETE CASCADE,
                visto_ate TIMESTAMP NOT NULL
            )
        """)
        for slug, quando, cat, publico, titulo, resumo, itens in NOVIDADES_SEED:
            cur.execute("""INSERT INTO novidades (slug, titulo, resumo, itens, categoria, publico, publicado_em, autor_nome)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'Time de Desenvolvimento')
                           ON CONFLICT (slug) DO NOTHING""",
                        (slug, titulo, resumo, _json.dumps(itens, ensure_ascii=False), cat, publico, quando))
        conn.commit(); cur.close()
    except Exception as e:
        print(f"Erro migração novidades: {e}")
    finally:
        conn.close()

_migrar_novidades()

def _visto_ate(cur, uid):
    cur.execute("SELECT visto_ate FROM novidades_vistas WHERE usuario_id=%s", (uid,))
    r = cur.fetchone()
    if r: return r[0]
    cur.execute("SELECT NOW()::timestamp - %s * INTERVAL '1 day'", (_NOVIDADES_JANELA_DIAS,))
    return cur.fetchone()[0]

def _novidade_dict(r, visto_ate):
    return {"id": r[0], "titulo": r[1], "resumo": r[2], "itens": r[3] or [], "categoria": r[4],
            "publico": r[5], "publicado_em": str(r[6]), "autor_nome": r[7],
            "nova": bool(visto_ate and r[6] > visto_ate)}

_NOVIDADES_SEL = "SELECT id, titulo, resumo, itens, categoria, publico, publicado_em, autor_nome FROM novidades WHERE ativo=TRUE"

class NovidadeModel(BaseModel):
    titulo: str
    resumo: str = ""
    itens: List[str] = []
    categoria: str = "novidade"
    publico: str = "Todos"

@router.get("/api/novidades")
def listar_novidades(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    cur = conn.cursor()
    visto = _visto_ate(cur, sess["id"])
    cur.execute(_NOVIDADES_SEL + " ORDER BY publicado_em DESC, id DESC")
    itens = [_novidade_dict(r, visto) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"novidades": itens, "pode_publicar": sess["perfil"] == "admin"}

@router.get("/api/novidades/nao-vistas")
def novidades_nao_vistas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    cur = conn.cursor()
    visto = _visto_ate(cur, sess["id"])
    cur.execute(_NOVIDADES_SEL + " AND publicado_em > %s ORDER BY publicado_em DESC, id DESC", (visto,))
    itens = [_novidade_dict(r, visto) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"total": len(itens), "itens": itens[:3]}

@router.post("/api/novidades/vistas")
def marcar_novidades_vistas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    cur = conn.cursor()
    cur.execute("""INSERT INTO novidades_vistas (usuario_id, visto_ate) VALUES (%s, NOW())
                   ON CONFLICT (usuario_id) DO UPDATE SET visto_ate = NOW()""", (sess["id"],))
    conn.commit(); cur.close(); conn.close()
    return {"sucesso": True}

@router.post("/api/novidades")
def publicar_novidade(n: NovidadeModel, faiston_token: str = Cookie(None)):
    """Admin/dev publica uma novidade: ela entra no topo da página e aparece
    no aviso de todo mundo que ainda não viu."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Apenas admin publica novidades")
    titulo = (n.titulo or "").strip()
    if not titulo: raise HTTPException(status_code=400, detail="Informe o título")
    if n.categoria not in _NOVIDADES_CATEGORIAS: raise HTTPException(status_code=400, detail="Categoria inválida")
    import json as _json
    itens = [i.strip() for i in (n.itens or []) if i and i.strip()]
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    cur = conn.cursor()
    cur.execute("""INSERT INTO novidades (titulo, resumo, itens, categoria, publico, autor_nome)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (titulo[:200], (n.resumo or "").strip(), _json.dumps(itens, ensure_ascii=False),
                 n.categoria, (n.publico or "Todos").strip()[:60] or "Todos", sess["nome"]))
    nid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return {"sucesso": True, "id": nid}

@router.delete("/api/novidades/{nid}")
def remover_novidade(nid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin": raise HTTPException(status_code=403, detail="Apenas admin remove novidades")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    cur = conn.cursor()
    cur.execute("UPDATE novidades SET ativo=FALSE WHERE id=%s", (nid,))
    conn.commit(); cur.close(); conn.close()
    return {"sucesso": True}

@router.get("/novidades")
def novidades_page(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: return RedirectResponse("/")
    return FileResponse("static/novidades.html", headers=_HTML_SEM_CACHE)
