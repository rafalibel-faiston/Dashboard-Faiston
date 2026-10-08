"""Carga de trabalho da equipe (quem está atolado).

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.agenda import _bloqueio_ativo, _hoje_sp
from app.core.auth import get_session
from app.core.db import get_db

# --- corpo ---
router = APIRouter()


# ── Carga de trabalho: quem está atolado ────────────────────────────
# O número de tarefas sozinho engana: dez acionamentos (peso 1) não pesam
# como quatro faturamentos (peso 4). Por isso a medida é a soma do peso das
# tarefas que a pessoa tem NA MÃO agora (aberto + em andamento), com as
# atrasadas e as que vencem já contando mais -- é isso que faz a pessoa
# "sentir" a carga. Tarefa concluída sai da conta: mede fila, não produção.
CARGA_PESO_PADRAO = 2          # tarefa sem peso carimbado (anterior à régua)
CARGA_MULT_ATRASADA = 1.5      # já passou do prazo
CARGA_MULT_VENCE_JA = 1.25     # vence hoje ou amanhã
CARGA_JANELA_VENCE_JA = 1      # dias à frente que contam como "vence já"
CARGA_LIMITES_PADRAO = {"moderado": 10, "pesado": 18, "atolado": 28, "atrasadas_atolado": 3}
CARGA_NIVEIS = {
    "tranquilo": {"rotulo": "Tranquilo",  "cor": "#0E9F6E", "icone": "ph-smiley"},
    "moderado":  {"rotulo": "Fluindo",    "cor": "#06D7E6", "icone": "ph-gauge"},
    "pesado":    {"rotulo": "Carga alta", "cor": "#F59E0B", "icone": "ph-warning"},
    "atolado":   {"rotulo": "Atolado",    "cor": "#EF4444", "icone": "ph-fire"},
}
CARGA_NIVEL_ORDEM = ["tranquilo", "moderado", "pesado", "atolado"]


def _carga_limites(cur, time_nome: str) -> dict:
    """Régua de pontos por nível. Cada time calibra a sua (assim como o peso
    das atividades já é por área); sem calibração, vale o padrão."""
    import json as _json
    limites = dict(CARGA_LIMITES_PADRAO)
    try:
        cur.execute("SELECT valor FROM configuracoes WHERE chave=%s",
                    (f"carga_limites_{time_nome or 'Projetos'}",))
        row = cur.fetchone()
        if row:
            salvo = _json.loads(row[0])
            for chave in limites:
                valor = salvo.get(chave)
                if isinstance(valor, (int, float)) and valor > 0:
                    limites[chave] = int(valor)
    except Exception:
        pass
    return limites


def _carga_nivel(pontos: float, atrasadas: int, limites: dict) -> str:
    """Atrasada demais também atola, mesmo que a soma dos pesos não assuste:
    três tarefas vencidas na mão já são um problema pro gestor olhar."""
    if pontos >= limites["atolado"] or atrasadas >= limites["atrasadas_atolado"]:
        return "atolado"
    if pontos >= limites["pesado"]:
        return "pesado"
    if pontos >= limites["moderado"]:
        return "moderado"
    return "tranquilo"


def calcular_carga_equipe(cur, time_filter: str = None, usuario_id: int = None) -> dict:
    """Fila aberta de cada pessoa, já classificada. `time_filter` restringe ao
    time (gestor vê só o seu); `usuario_id` restringe a uma pessoa (funcionário
    consultando a própria carga)."""
    hoje = _hoje_sp()
    limite_vence_ja = hoje + timedelta(days=CARGA_JANELA_VENCE_JA)

    q = """SELECT u.id, u.nome, COALESCE(u.time,'Projetos'), COALESCE(u.cargo,''), u.perfil,
                  t.id, t.status, t.peso, t.data_prazo, t.descricao, t.cliente
           FROM usuarios u
           LEFT JOIN tarefas t ON t.usuario_id = u.id AND t.status IN ('aberto','em_andamento')
           WHERE u.ativo = TRUE AND u.perfil <> 'demo'"""
    params = []
    if time_filter:
        q += " AND COALESCE(u.time,'Projetos') = %s"; params.append(time_filter)
    if usuario_id:
        q += " AND u.id = %s"; params.append(usuario_id)
    cur.execute(q, tuple(params))

    pessoas = {}
    for uid, nome, time_u, cargo, perfil, tid, status, peso, prazo, desc, cliente in cur.fetchall():
        p = pessoas.get(uid)
        if p is None:
            p = pessoas[uid] = {
                "usuario_id": uid, "nome": nome, "time": time_u, "cargo": cargo,
                "perfil": perfil, "tarefas": 0, "em_andamento": 0, "abertas": 0,
                "peso_total": 0, "pontos": 0.0, "atrasadas": 0, "vence_ja": 0,
                "destaques": [],
            }
        if tid is None:
            continue
        peso = int(peso or CARGA_PESO_PADRAO)
        atrasada = bool(prazo and prazo < hoje)
        vence_ja = bool(prazo and not atrasada and prazo <= limite_vence_ja)
        mult = CARGA_MULT_ATRASADA if atrasada else (CARGA_MULT_VENCE_JA if vence_ja else 1.0)
        p["tarefas"] += 1
        p["em_andamento" if status == "em_andamento" else "abertas"] += 1
        p["peso_total"] += peso
        p["pontos"] += peso * mult
        if atrasada: p["atrasadas"] += 1
        if vence_ja: p["vence_ja"] += 1
        p["destaques"].append({
            "id": tid, "descricao": desc, "cliente": cliente, "peso": peso,
            "status": status, "data_prazo": str(prazo) if prazo else None,
            "atrasada": atrasada, "vence_ja": vence_ja,
        })

    limites_por_time = {}
    equipe = []
    for p in pessoas.values():
        # Quem não tem nada na fila só aparece se for funcionário -- é útil pro
        # gestor ver quem está livre pra receber. Gestor/admin sem tarefa
        # nenhuma na mão não é "pessoa da fila", é só quem olha o quadro.
        if p["tarefas"] == 0 and p["perfil"] != "funcionario":
            continue
        limites = limites_por_time.get(p["time"])
        if limites is None:
            limites = limites_por_time[p["time"]] = _carga_limites(cur, p["time"])
        p["pontos"] = round(p["pontos"], 1)
        p["nivel"] = _carga_nivel(p["pontos"], p["atrasadas"], limites)
        p["rotulo"] = CARGA_NIVEIS[p["nivel"]]["rotulo"]
        p["cor"] = CARGA_NIVEIS[p["nivel"]]["cor"]
        p["limite_atolado"] = limites["atolado"]
        p["peso_medio"] = round(p["peso_total"] / p["tarefas"], 1) if p["tarefas"] else 0
        p["indisponivel"] = _bloqueio_ativo(cur, p["usuario_id"], hoje.isoformat())
        # As mais pesadas primeiro (atrasada na frente): é o que o gestor
        # precisa ver pra decidir o que redistribuir.
        p["destaques"] = sorted(
            p["destaques"], key=lambda t: (not t["atrasada"], -t["peso"]))[:5]
        p.pop("perfil", None)
        equipe.append(p)

    equipe.sort(key=lambda p: (-p["pontos"], -p["tarefas"], (p["nome"] or "").lower()))
    sinalizados = [p for p in equipe if p["nivel"] in ("pesado", "atolado")]
    com_fila = [p for p in equipe if p["tarefas"] > 0]
    return {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "limites": _carga_limites(cur, time_filter or "Projetos"),
        "niveis": CARGA_NIVEIS,
        "equipe": equipe,
        "resumo": {
            "pessoas": len(equipe),
            "atolados": sum(1 for p in equipe if p["nivel"] == "atolado"),
            "pesados": sum(1 for p in equipe if p["nivel"] == "pesado"),
            "livres": sum(1 for p in equipe if p["tarefas"] == 0),
            "tarefas_abertas": sum(p["tarefas"] for p in equipe),
            "pontos_medios": round(sum(p["pontos"] for p in com_fila) / len(com_fila), 1) if com_fila else 0,
            "nomes_sinalizados": [p["nome"] for p in sinalizados],
        },
    }


@router.get("/api/carga-equipe")
def carga_equipe(faiston_token: str = Cookie(None)):
    """Sinalização de sobrecarga do kanban: quanto cada pessoa tem na fila e
    quem está atolado. Gestor vê o próprio time, admin/diretor vê tudo,
    funcionário vê só a própria carga."""
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] in ("admin", "diretor"):
            dados = calcular_carga_equipe(cur)
        elif sess["perfil"] in ("gestor", "demo"):
            dados = calcular_carga_equipe(cur, time_filter=sess.get("time", "Projetos"))
        else:
            dados = calcular_carga_equipe(cur, time_filter=sess.get("time", "Projetos"),
                                          usuario_id=sess["id"])
        cur.close(); conn.close()
        return dados
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


class CargaLimitesModel(BaseModel):
    moderado: int
    pesado: int
    atolado: int
    atrasadas_atolado: int = CARGA_LIMITES_PADRAO["atrasadas_atolado"]


@router.get("/api/config/carga-limites")
def get_carga_limites(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        limites = _carga_limites(cur, sess.get("time", "Projetos"))
        cur.close(); conn.close()
        return {"time": sess.get("time", "Projetos"), "limites": limites,
                "padrao": CARGA_LIMITES_PADRAO}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/config/carga-limites")
def salvar_carga_limites(body: CargaLimitesModel, faiston_token: str = Cookie(None)):
    """Calibra a régua de sobrecarga do time. Cada operação tem um ritmo --
    o que é 'atolado' no Backoffice não é o mesmo do N2."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "diretor"):
        raise HTTPException(status_code=403, detail="Sem permissão")
    if not (0 < body.moderado < body.pesado < body.atolado):
        raise HTTPException(status_code=400,
                            detail="Os limites precisam ser crescentes: moderado < pesado < atolado")
    if body.atrasadas_atolado < 1:
        raise HTTPException(status_code=400, detail="Atrasadas para atolado precisa ser pelo menos 1")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        import json as _json
        cur = conn.cursor()
        valor = _json.dumps({"moderado": body.moderado, "pesado": body.pesado,
                             "atolado": body.atolado,
                             "atrasadas_atolado": body.atrasadas_atolado})
        cur.execute("""INSERT INTO configuracoes (chave, valor, atualizado_em) VALUES (%s, %s, NOW())
                       ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor, atualizado_em=NOW()""",
                    (f"carga_limites_{sess.get('time','Projetos')}", valor))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
