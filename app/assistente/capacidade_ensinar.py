"""Capacidade E — ensinar (onboarding guiado, "modo professor"). Leva a
pessoa nova pelos documentos marcados como trilha de onboarding
(`documento.ordem_onboarding`, definido pelo admin em
`/assistente/documentos`), uma etapa de cada vez, num tom didático.

Reaproveita a base de procedimentos que já existe (Fase 3) em vez de
criar um sistema de conteúdo novo -- qualquer documento já indexado pode
virar uma etapa, só marcando a posição dele na trilha. O progresso por
pessoa (`onboarding_progresso`) é o que não tinha equivalente: sempre
retoma de onde a pessoa parou, nunca reinicia sozinho.
"""
from typing import Optional

from app.assistente.db import get_conn


def total_etapas() -> int:
    conn = get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM documento WHERE ordem_onboarding IS NOT NULL AND ativo = true"
        )
        total = cur.fetchone()[0]
        cur.close()
        conn.close()
        return total
    except Exception as e:
        print(f"[assistente/ensinar] Erro contando etapas: {e}")
        return 0


def etapa_por_ordem(ordem: int) -> Optional[dict]:
    """Documento daquela posição na trilha. Não existe texto bruto salvo
    (Fase 3 só guarda os blocos já quebrados pra embedding) -- pra
    ensinar interessa o conteúdo inteiro, então concatena os blocos na
    ordem original; a pequena sobreposição entre blocos vizinhos é
    redundância inofensiva pra leitura, não pra busca."""
    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, titulo FROM documento WHERE ordem_onboarding = %s AND ativo = true",
            (ordem,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return None
        documento_id, titulo = row
        cur.execute(
            "SELECT texto FROM documento_chunk WHERE documento_id = %s ORDER BY ordem",
            (documento_id,),
        )
        blocos = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {
            "documento_id": documento_id,
            "titulo": titulo,
            "ordem": ordem,
            "texto": "\n\n".join(blocos),
        }
    except Exception as e:
        print(f"[assistente/ensinar] Erro buscando etapa {ordem}: {e}")
        return None


def progresso_atual(usuario_id: int) -> Optional[dict]:
    """None significa "nunca começou" -- diferente de {"etapa_atual": 1,
    "concluido": False}, que é o estado de quem já começou e está na
    primeira etapa."""
    conn = get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT etapa_atual, concluido_em FROM onboarding_progresso WHERE usuario_id = %s",
            (usuario_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {"etapa_atual": row[0], "concluido": row[1] is not None}
    except Exception as e:
        print(f"[assistente/ensinar] Erro lendo progresso: {e}")
        return None


def iniciar_ou_retomar(usuario_id: int) -> dict:
    """Cria o progresso na etapa 1 se a pessoa nunca começou; se já tem
    progresso (mesmo já concluído), retoma dali -- nunca reinicia por
    cima de um onboarding em andamento ou já feito."""
    existente = progresso_atual(usuario_id)
    if existente is not None:
        return existente
    conn = get_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO onboarding_progresso (usuario_id, etapa_atual) VALUES (%s, 1) "
                "ON CONFLICT (usuario_id) DO NOTHING",
                (usuario_id,),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"[assistente/ensinar] Erro iniciando progresso: {e}")
    return {"etapa_atual": 1, "concluido": False}


def avancar(usuario_id: int) -> dict:
    """Avança uma etapa. Sem próxima etapa na trilha, marca concluído em
    vez de apontar pra uma etapa que não existe."""
    atual = progresso_atual(usuario_id)
    etapa_atual = atual["etapa_atual"] if atual else 1
    total = total_etapas()
    proxima = etapa_atual + 1
    concluido = total > 0 and proxima > total
    etapa_final = min(proxima, total) if total else proxima

    conn = get_conn()
    if not conn:
        return {"etapa_atual": etapa_final, "concluido": concluido}
    try:
        cur = conn.cursor()
        if concluido:
            cur.execute(
                "INSERT INTO onboarding_progresso (usuario_id, etapa_atual, concluido_em) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (usuario_id) DO UPDATE SET etapa_atual = %s, atualizado_em = now(), concluido_em = now()",
                (usuario_id, etapa_final, etapa_final),
            )
        else:
            cur.execute(
                "INSERT INTO onboarding_progresso (usuario_id, etapa_atual) VALUES (%s, %s) "
                "ON CONFLICT (usuario_id) DO UPDATE SET etapa_atual = %s, atualizado_em = now()",
                (usuario_id, etapa_final, etapa_final),
            )
        conn.commit()
        cur.close()
        conn.close()
        return {"etapa_atual": etapa_final, "concluido": concluido}
    except Exception as e:
        print(f"[assistente/ensinar] Erro avançando progresso: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return {"etapa_atual": etapa_atual, "concluido": False}
