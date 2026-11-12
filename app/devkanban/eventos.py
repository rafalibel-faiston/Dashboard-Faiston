"""Eventos do quadro: webhooks de SAÍDA (assinados com HMAC-SHA256) e sincronização
do prazo com o calendário do Outlook.

`emitir()` nunca bloqueia nem derruba a ação do usuário: roda em thread, e qualquer
falha só vira log + o "último status" do hook. Webhooks de ENTRADA genéricos
(n8n/TiFlux/Hub) NÃO emitem eventos de saída -- evita laço (saída -> n8n -> entrada
-> saída...). Só o webhook do GitHub emite, porque o GitHub não chama de volta.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import requests

from app.devkanban import db as dkdb
from app.devkanban import foco, graph

EVENTOS = ("tarefa.criada", "tarefa.atualizada", "tarefa.movida", "tarefa.concluida", "tarefa.excluida")
# Em produção os dois ficam desligados: só https e só endereços públicos.
PERMITIR_HTTP = os.environ.get("DEV_HOOKS_PERMITIR_HTTP") == "1"
PERMITIR_REDE_PRIVADA = os.environ.get("DEV_HOOKS_PERMITIR_REDE_PRIVADA") == "1"


def url_segura(url: str) -> tuple[bool, str]:
    """Barra SSRF: o destino de um webhook de saída é digitado por usuário, então não pode apontar
    pra rede interna (metadados da nuvem, serviços privados, localhost)."""
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return False, "URL inválida"
    if u.scheme != "https" and not (PERMITIR_HTTP and u.scheme == "http"):
        return False, "Use uma URL https://"
    if not u.hostname:
        return False, "URL inválida"
    if u.username or u.password:
        return False, "A URL não pode conter usuário ou senha"
    if PERMITIR_REDE_PRIVADA:
        return True, ""
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "Não consegui resolver o endereço dessa URL"
    for i in infos:
        if not ipaddress.ip_address(i[4][0].split("%")[0]).is_global:
            return False, "Endereços internos ou privados não são permitidos"
    return True, ""


def assinar(segredo: str, corpo: bytes) -> str:
    return "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


def _http_post(url: str, corpo: bytes, headers: dict) -> int:
    """Única função que faz o POST de saída (os testes trocam esta). Sem seguir redirect (anti-SSRF)."""
    return requests.post(url, data=corpo, headers=headers, timeout=6, allow_redirects=False).status_code


def enviar_hook(hook: dict, evento: str, tarefa: dict) -> tuple[Optional[int], str]:
    ok, motivo = url_segura(hook["url"])
    if not ok:
        return None, motivo
    corpo = json.dumps({"evento": evento, "enviado_em": datetime.utcnow().isoformat() + "Z", "tarefa": tarefa},
                       ensure_ascii=False, default=str).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "faiston-ops-kanban",
               "X-Faiston-Event": evento, "X-Faiston-Delivery": str(uuid.uuid4()),
               "X-Faiston-Signature": assinar(hook["segredo"], corpo)}
    erro = ""
    for tentativa in (1, 2):
        try:
            st = _http_post(hook["url"], corpo, headers)
            if st < 500:
                return st, "" if st < 400 else f"HTTP {st}"
            erro = f"HTTP {st}"
        except requests.RequestException as e:
            erro = type(e).__name__
        if tentativa == 1:
            time.sleep(1)
    return None, erro


def _executar(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def emitir(tipo: str, tid: int, snapshot: dict = None) -> None:
    """Dispara (em segundo plano) os webhooks de saída e a sincronização do calendário. `snapshot` só
    é necessário pra tarefa excluída (já não dá pra ler do banco)."""
    _executar(lambda: _processar(tipo, tid, snapshot))


def _processar(tipo: str, tid: int, snapshot: Optional[dict]) -> None:
    conn = dkdb.get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        tarefa = snapshot
        if tarefa is None:
            lst = dkdb.listar_tarefas(cur, tid)
            tarefa = foco.anexar(lst)[0] if lst else None
        if tarefa is None:
            return
        cur.execute("SELECT id, url, segredo, eventos FROM dev_hooks_saida WHERE ativo")
        for hid, url, segredo, eventos in cur.fetchall():
            if eventos and tipo not in eventos:
                continue
            st, erro = enviar_hook({"url": url, "segredo": segredo}, tipo, tarefa)
            cur.execute("UPDATE dev_hooks_saida SET ultimo_status=%s, ultimo_erro=%s, ultimo_em=NOW() WHERE id=%s", (st, erro or None, hid))
            conn.commit()
        try:
            _sincronizar_calendario(cur, tipo, tid, tarefa)
            conn.commit()
        except Exception as e:                    # calendário falhar não pode atrapalhar o resto
            conn.rollback()
            print(f"[devkanban/eventos] calendário da tarefa {tid}: {e}")
    except Exception as e:
        print(f"[devkanban/eventos] {tipo} tarefa {tid}: {type(e).__name__}: {e}")
    finally:
        conn.close()


def _sincronizar_calendario(cur, tipo: str, tid: int, tarefa: dict) -> None:
    if not graph.configurado():
        return
    if tipo == "tarefa.excluida":
        evento_id, mailbox = tarefa.get("evento_id"), tarefa.get("evento_mailbox")
    else:
        cur.execute("SELECT evento_id, evento_mailbox FROM dev_tarefas WHERE id = %s", (tid,))
        row = cur.fetchone()
        evento_id, mailbox = row if row else (None, None)
    if not evento_id or not mailbox:
        return
    if tipo == "tarefa.excluida" or tarefa.get("status") == "done" or not tarefa.get("prazo"):
        graph.remover_evento(mailbox, evento_id)
        if tipo != "tarefa.excluida":
            cur.execute("UPDATE dev_tarefas SET evento_id = NULL, evento_mailbox = NULL WHERE id = %s", (tid,))
    else:
        graph.atualizar_evento(mailbox, evento_id, tarefa["titulo"], tarefa["prazo"], tarefa.get("link") or "")
