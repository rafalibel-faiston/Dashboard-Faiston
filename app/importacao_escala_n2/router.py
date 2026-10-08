"""Importação da planilha de cronograma para a Escala N2.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import date, datetime, timedelta
import io

from fastapi import APIRouter, Cookie, File, HTTPException, UploadFile

from app.core.agenda import _bloqueio_ativo, _dias_bloqueados_periodo
from app.core.auth import get_session
from app.core.db import get_db
from app.importacao_cronograma.router import _SC_IMPORT_HEADERS, _sc_find_sheet_and_header, _sc_mapear_status, _sc_norm_header, _sc_norm_nome
from app.status_campo.router import STATUS_CAMPO_TERMINAIS

# --- corpo ---
router = APIRouter()


# ── Importação da mesma planilha, agora pra Escala N2 ────────────────────
# TECNICO (instalador de campo) e N2 (suporte remoto) são papéis sem
# relação nenhuma entre si (esclarecido pelo usuário, 2026-07-30) -- casar
# por nome nunca fazia sentido pra esse tipo de planilha (a versão antiga
# deste endpoint fazia isso). Passa a fazer as duas coisas de uma vez: (1)
# cria as atividades no Cronograma exatamente como o import de lá (mesmo
# casamento de cliente por nome), e (2) distribui os N2 ativos por
# rodízio entre as atividades recém-criadas sem N2 -- até 3 do mesmo
# cliente/data por N2, mesma regra do Gerador de Escala manual (Painel
# N2) -- gravando o vínculo em status_atividades e o plantão em escala_n2.
@router.post("/api/escala-n2/importar-planilha")
async def importar_planilha_escala_n2(file: UploadFile = File(...), faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    global _OPENPYXL_OK, openpyxl
    if not _OPENPYXL_OK:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl", "-q"])
        import openpyxl as _ox; openpyxl = _ox; _OPENPYXL_OK = True
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        content = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        achado = _sc_find_sheet_and_header(wb)
        if not achado:
            raise HTTPException(status_code=400,
                detail="Nenhuma aba reconhecida (esperado colunas como CLIENTE, STATUS ATIVIDADE, TECNICO etc.)")
        sname, hi, _ = achado
        rows = list(wb[sname].iter_rows(values_only=True))
        headers = rows[hi]
        col = {}
        for idx, h in enumerate(headers):
            key = _SC_IMPORT_HEADERS.get(_sc_norm_header(h))
            if key and key not in col: col[key] = idx

        cur = conn.cursor()
        cur.execute("SELECT id, nome FROM clientes WHERE ativo = TRUE")
        clientes_rows = cur.fetchall()
        clientes_norm = [(_sc_norm_nome(nome), cid) for cid, nome in clientes_rows]
        cliente_nome_por_id = {cid: nome for cid, nome in clientes_rows}

        def buscar_cliente(cliente_raw, projeto_raw):
            for cand in (cliente_raw, projeto_raw, f"{cliente_raw} {projeto_raw}".strip()):
                nn = _sc_norm_nome(cand)
                if not nn: continue
                for norm_nome, cid in clientes_norm:
                    if norm_nome and (nn == norm_nome or nn in norm_nome or norm_nome in nn):
                        return cid
            return None

        cur.execute("SELECT id, nome FROM usuarios WHERE ativo=TRUE AND perfil='funcionario' AND cargo='n2' ORDER BY nome")
        n2_ativos = cur.fetchall()
        if not n2_ativos:
            raise HTTPException(status_code=400, detail="Nenhum N2 ativo cadastrado -- não é possível gerar escala.")

        def get(r, field):
            idx = col.get(field)
            v = r[idx] if idx is not None and idx < len(r) else None
            return v

        def corta(v, tam):
            return str(v or '').strip()[:tam]

        importadas = 0
        puladas_sem_cliente = {}
        puladas_sem_status = 0
        puladas_sem_data = 0
        puladas_erro = 0
        criadas = []  # (id, data_iso, cliente_id, horario_val) -- só as escaláveis (não terminais)
        for r in rows[hi + 1:]:
            cliente_raw = str(get(r, 'cliente') or '').strip()
            projeto_raw = str(get(r, 'projeto') or '').strip()
            if not cliente_raw and not projeto_raw:
                continue
            data_raw = get(r, 'data')
            if isinstance(data_raw, datetime): data_val = data_raw.date().isoformat()
            elif isinstance(data_raw, date): data_val = data_raw.isoformat()
            else: data_val = None
            if not data_val:
                puladas_sem_data += 1
                continue
            cid = buscar_cliente(cliente_raw, projeto_raw)
            if not cid:
                chave = f"{cliente_raw} {projeto_raw}".strip()
                puladas_sem_cliente[chave] = puladas_sem_cliente.get(chave, 0) + 1
                continue
            status_mapeado = _sc_mapear_status(get(r, 'status_raw'))
            if not status_mapeado:
                puladas_sem_status += 1
                continue
            horario_raw = get(r, 'horario_agendado')
            horario_val = horario_raw.strftime('%H:%M') if hasattr(horario_raw, 'strftime') else None
            cur.execute("SAVEPOINT linha_escala_import")
            try:
                cur.execute("""
                    INSERT INTO status_atividades
                        (cliente_id, data, horario_agendado, tecnico, n2_responsavel,
                         site_nome, endereco, cidade, uf, subprojeto, ticket, status, observacoes, criado_por)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (cid, data_val, horario_val, corta(get(r, 'tecnico'), 150),
                      None, corta(get(r, 'site'), 150),
                      str(get(r, 'endereco') or '').strip(), corta(get(r, 'cidade'), 100),
                      corta(get(r, 'uf'), 2), corta(get(r, 'subprojeto'), 150),
                      corta(get(r, 'ticket'), 100), status_mapeado,
                      str(get(r, 'observacoes') or '').strip(), sess["id"]))
                new_id = cur.fetchone()[0]
                cur.execute("RELEASE SAVEPOINT linha_escala_import")
                importadas += 1
                if status_mapeado not in STATUS_CAMPO_TERMINAIS:
                    criadas.append((new_id, data_val, cid, horario_val))
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT linha_escala_import")
                puladas_erro += 1

        # Distribui N2 por rodízio entre as atividades recém-criadas ainda
        # sem N2, agrupadas por (data, cliente) e em blocos de até 3 -- não
        # tenta escalar o que já entrou concluído/cancelado/etc. como
        # histórico (não faz sentido gerar plantão pro passado).
        grupos = {}
        for aid, data_val, cid, horario_val in criadas:
            grupos.setdefault((data_val, cid), []).append((aid, horario_val))

        # Regra de negócio: Kleber nunca atende Arcos Dourados (instrução
        # explícita do usuário, 2026-07-30) -- pula pro próximo N2 do
        # rodízio quando o cliente do bloco for esse.
        def _n2_bloqueado_pro_cliente(nome_n2, cliente_nome_norm):
            return cliente_nome_norm == 'ARCOS DOURADOS' and _sc_norm_nome(nome_n2) == 'KLEBER'

        cursor_n2 = 0
        dia_atual = None
        escalas_criadas = 0
        blocos_sem_n2 = 0
        for (data_val, cid), itens in sorted(grupos.items()):
            # reseta o rodízio a cada dia novo -- garante que todo N2
            # disponível receba atividade no dia, em vez de sempre
            # concentrar nos primeiros da lista quando o dia anterior
            # deixou o cursor no meio (achado real, 2026-07-30).
            if data_val != dia_atual:
                dia_atual = data_val
                cursor_n2 = 0
            itens.sort(key=lambda x: (x[1] is None, x[1] or ''))
            cliente_nome_norm = _sc_norm_nome(cliente_nome_por_id.get(cid, ''))
            for i in range(0, len(itens), 3):
                bloco = itens[i:i + 3]
                bloco_ids = [b[0] for b in bloco]
                horario_bloco = bloco[0][1]
                n2_id = n2_nome = None
                for offset in range(len(n2_ativos)):
                    idx = (cursor_n2 + offset) % len(n2_ativos)
                    cand_id, cand_nome = n2_ativos[idx]
                    if _n2_bloqueado_pro_cliente(cand_nome, cliente_nome_norm):
                        continue
                    if _bloqueio_ativo(cur, cand_id, data_val, horario_bloco):
                        continue
                    n2_id, n2_nome = cand_id, cand_nome
                    cursor_n2 = idx + 1  # próximo bloco continua depois deste, sem desalinhar
                    break
                if not n2_id:
                    blocos_sem_n2 += 1
                    cursor_n2 += 1
                    continue
                cur.execute("""
                    UPDATE status_atividades SET n2_usuario_id=%s, n2_responsavel=%s, atualizado_em=NOW()
                    WHERE id = ANY(%s)
                """, (n2_id, n2_nome, bloco_ids))
                hora_num = int(horario_bloco[:2]) if horario_bloco else None
                modalidade = 'home' if (hora_num is not None and (hora_num < 8 or hora_num >= 18)) else 'presencial'
                cliente_nome = cliente_nome_por_id.get(cid, '?')
                atribuicao = f"{cliente_nome} ({len(bloco)} atividade{'s' if len(bloco) > 1 else ''})"[:200]
                cur.execute("""
                    INSERT INTO escala_n2 (data, n2_usuario_id, horario_entrada, modalidade, atribuicao)
                    VALUES (%s,%s,%s,%s,%s)
                """, (data_val, n2_id, horario_bloco, modalidade, atribuicao))
                escalas_criadas += 1

        conn.commit(); cur.close(); conn.close()
        return {
            "sucesso": True, "importadas": importadas, "aba_usada": sname,
            "puladas_erro": puladas_erro,
            "puladas_sem_cliente": [{"nome": k, "ocorrencias": v}
                                     for k, v in sorted(puladas_sem_cliente.items(), key=lambda x: -x[1])],
            "puladas_sem_status": puladas_sem_status, "puladas_sem_data": puladas_sem_data,
            "escalas_criadas": escalas_criadas, "blocos_sem_n2_disponivel": blocos_sem_n2,
        }
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao importar planilha: {str(e)}")

@router.get("/api/painel-n2/resumo")
def painel_n2_resumo(faiston_token: str = Cookie(None)):
    """Lista de usuários N2 com atividades concluídas e horas trabalhadas
    (hora_chegada -> hora_termino) na semana e no mês corrente -- substitui
    o antigo resumo total/concluído/pendente, que não dava visão de carga
    de trabalho por período."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo", "diretor"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome, ativo FROM usuarios WHERE perfil='funcionario' AND cargo='n2' ORDER BY ativo DESC, nome")
        n2s = cur.fetchall()
        out = []
        for uid, nome, ativo in n2s:
            # Atividade que cruza a meia-noite (chegada 22h, término 2h) tem
            # hora_termino < hora_chegada -- o CASE trata isso como "terminou
            # no dia seguinte" em vez de excluir a visita da soma de horas
            # (achado real, 2026-07-30: visita noturna sumia do total).
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='concluido' AND data >= date_trunc('week', CURRENT_DATE)::date),
                    COUNT(*) FILTER (WHERE status='concluido' AND data >= date_trunc('month', CURRENT_DATE)::date),
                    COALESCE(SUM(EXTRACT(EPOCH FROM (
                        CASE WHEN hora_termino < hora_chegada
                             THEN (hora_termino - hora_chegada) + INTERVAL '24 hours'
                             ELSE hora_termino - hora_chegada END
                    )) / 3600.0)
                        FILTER (WHERE status='concluido' AND hora_chegada IS NOT NULL AND hora_termino IS NOT NULL
                                AND data >= date_trunc('week', CURRENT_DATE)::date), 0),
                    COALESCE(SUM(EXTRACT(EPOCH FROM (
                        CASE WHEN hora_termino < hora_chegada
                             THEN (hora_termino - hora_chegada) + INTERVAL '24 hours'
                             ELSE hora_termino - hora_chegada END
                    )) / 3600.0)
                        FILTER (WHERE status='concluido' AND hora_chegada IS NOT NULL AND hora_termino IS NOT NULL
                                AND data >= date_trunc('month', CURRENT_DATE)::date), 0)
                FROM status_atividades WHERE n2_usuario_id = %s
            """, (uid,))
            ativ_semana, ativ_mes, horas_semana, horas_mes = cur.fetchone()
            # Dias trabalhados = dias do período menos férias/afastamento/
            # recorrência -- produtividade mais justa, com obs quando a
            # pessoa ficou fora de parte do período (ponto 2 do feedback).
            hoje = date.today()
            inicio_semana = hoje - timedelta(days=hoje.weekday())
            inicio_mes = hoje.replace(day=1)
            dias_bloq_semana, _ = _dias_bloqueados_periodo(cur, uid, str(inicio_semana), str(hoje))
            dias_bloq_mes, notas_mes = _dias_bloqueados_periodo(cur, uid, str(inicio_mes), str(hoje))
            dias_trab_semana = max(1, (hoje - inicio_semana).days + 1 - dias_bloq_semana)
            dias_trab_mes = max(1, (hoje - inicio_mes).days + 1 - dias_bloq_mes)
            item = {
                "id": uid, "nome": nome, "ativo": ativo,
                "atividades_semana": ativ_semana, "atividades_mes": ativ_mes,
                "horas_semana": round(float(horas_semana), 1), "horas_mes": round(float(horas_mes), 1),
                "dias_trabalhados_semana": dias_trab_semana, "dias_trabalhados_mes": dias_trab_mes,
                "horas_por_dia_mes": round(float(horas_mes) / dias_trab_mes, 2),
            }
            if dias_bloq_mes:
                item["obs"] = f"{dias_bloq_mes} dia(s) fora no mês ({', '.join(sorted(set(notas_mes)))}) — produtividade tende a ser menor"
            out.append(item)
        cur.close(); conn.close()
        return out
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
