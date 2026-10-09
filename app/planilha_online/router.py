"""Sincronização de planilha online (OneDrive / SharePoint) dos projetos.

Saiu do main.py na refatoração de 2026-10-08, sem mudança de comportamento.
"""
from datetime import date

import openpyxl
from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from app.core.auth import get_session
from app.core.db import get_db
from app.financeiro.router import _detect_header_row, _download_planilha, _ensure_financeiro_tables, _parse_sheet


router = APIRouter()


# ─────────────────────────────────────────────
#  PLANILHA ONLINE (OneDrive / SharePoint)
# ─────────────────────────────────────────────

class PlanilhaConfigModel(BaseModel):
    url: str
    mapeamento: dict
    replace_on_sync: bool = False


def _apply_mapeamento(headers, rows, mapeamento):
    """Convert raw sheet rows to LancamentoImportItem dicts using saved mapping."""
    from datetime import date as _date
    today = _date.today().isoformat()
    def col_idx(col_name):
        if not col_name:
            return None
        try:
            return headers.index(col_name)
        except ValueError:
            return None

    idx_desc  = col_idx(mapeamento.get("col_descricao"))
    idx_valor = col_idx(mapeamento.get("col_valor"))
    idx_data  = col_idx(mapeamento.get("col_data"))
    idx_cat   = col_idx(mapeamento.get("col_categoria"))
    idx_loc   = col_idx(mapeamento.get("col_localidade"))
    idx_tec   = col_idx(mapeamento.get("col_tecnico"))

    if idx_desc is None or idx_valor is None:
        raise ValueError("Mapeamento incompleto: coluna de descrição ou valor não encontrada nos cabeçalhos.")

    result = []
    for row in rows:
        def get(i): return row[i].strip() if i is not None and i < len(row) else ""
        raw_val = get(idx_valor).replace("R$","").replace(".","").replace(",",".").strip()
        try:
            val = float(raw_val)
        except Exception:
            continue
        if val == 0:
            continue
        result.append({
            "descricao":  get(idx_desc)[:200] or "—",
            "valor":       val,
            "data_lancamento": get(idx_data)[:10] if idx_data is not None else today,
            "categoria":  get(idx_cat)[:50] if idx_cat is not None else "Outros",
            "localidade": get(idx_loc)[:150] if idx_loc is not None else "",
            "tecnico":    get(idx_tec)[:150] if idx_tec is not None else "",
        })
    return result

@router.put("/api/projetos/{pid}/planilha-config")
def salvar_planilha_config(pid: int, body: PlanilhaConfigModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        import json
        cur = conn.cursor()
        _ensure_financeiro_tables(cur)
        cur.execute(
            "UPDATE projetos SET planilha_url=%s, planilha_mapeamento=%s, planilha_replace=%s WHERE id=%s",
            (body.url, json.dumps(body.mapeamento), body.replace_on_sync, pid)
        )
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/projetos/{pid}/testar-planilha")
def testar_planilha(pid: int, body: dict, faiston_token: str = Cookie(None)):
    """Download the file from stored/given URL and return its headers for mapping."""
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    url = body.get("url", "")
    if not url: raise HTTPException(status_code=400, detail="URL não informada")
    try:
        content = _download_planilha(url)
        import io as _io
        wb = openpyxl.load_workbook(_io.BytesIO(content), data_only=True)
        usable = []
        for sname in wb.sheetnames:
            ws = wb[sname]
            rows_peek = list(ws.iter_rows(min_row=1, max_row=10, values_only=True))
            hi = _detect_header_row(rows_peek)
            hrow = rows_peek[hi] if rows_peek else []
            if sum(1 for c in hrow if c and isinstance(c, str) and c.strip()) >= 2:
                usable.append(sname)
        if not usable: raise ValueError("Nenhuma aba com dados encontrada")
        ws = wb[usable[0]]
        headers, rows = _parse_sheet(ws)
        return {"sheets": usable, "headers": headers, "sample": rows[:3]}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/projetos/{pid}/sincronizar")
def sincronizar_planilha(pid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] not in ("admin", "gestor", "demo"): raise HTTPException(status_code=403)
    conn = get_db()
    if not conn: raise HTTPException(status_code=500)
    try:
        import io as _io
        cur = conn.cursor()
        _ensure_financeiro_tables(cur)
        cur.execute("SELECT planilha_url, planilha_mapeamento, planilha_replace FROM projetos WHERE id=%s AND ativo=TRUE", (pid,))
        row = cur.fetchone()
        if not row or not row[0]:
            raise HTTPException(status_code=400, detail="Planilha não configurada para este projeto")
        url, mapeamento, replace = row[0], row[1] or {}, bool(row[2])

        content = _download_planilha(url)
        wb = openpyxl.load_workbook(_io.BytesIO(content), data_only=True)
        sheet_name = mapeamento.get("sheet_name")
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        headers, rows = _parse_sheet(ws)
        lancamentos = _apply_mapeamento(headers, rows, mapeamento)

        if not lancamentos:
            raise HTTPException(status_code=400, detail="Nenhum lançamento encontrado com o mapeamento configurado")

        today = date.today().isoformat()
        # Sync sempre substitui — planilha é a fonte da verdade
        cur.execute("DELETE FROM lancamentos WHERE projeto_id=%s", (pid,))
        for l in lancamentos:
            cur.execute(
                "INSERT INTO lancamentos (projeto_id, descricao, categoria, valor, data_lancamento, localidade, tecnico) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (pid, l["descricao"], l["categoria"], l["valor"],
                 l["data_lancamento"] or today, l["localidade"], l["tecnico"])
            )
        cur.execute("UPDATE projetos SET planilha_sync_em=NOW() WHERE id=%s", (pid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "importados": len(lancamentos), "replace": replace}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
