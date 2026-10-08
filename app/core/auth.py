"""Senha e sessão. Saiu do main.py (2026-10-08) junto com app/core/db.py;
o main reexporta os nomes."""
import hashlib
import logging
import re

import bcrypt

from app.core.db import get_db

logger = logging.getLogger("faiston")


def hash_senha(senha):
    """bcrypt com salt próprio por senha. SHA-256 puro (o esquema anterior)
    não tem salt nem custo, o que torna quebra por rainbow table trivial se
    o banco vazar."""
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()

def _hash_legado(senha):
    """Esquema antigo. Mantido só para validar quem ainda não fez login
    desde a migração -- a senha é reescrita em bcrypt no primeiro acesso."""
    return hashlib.sha256(senha.encode()).hexdigest()

def senha_confere(senha, hash_armazenado):
    if not hash_armazenado:
        return False
    if hash_armazenado.startswith("$2"):
        try:
            return bcrypt.checkpw(senha.encode(), hash_armazenado.encode())
        except Exception:
            return False
    return _hash_legado(senha) == hash_armazenado

def _senha_fraca(senha, usuario=""):
    """Política mínima de senha forte (2026-07-30). Segue NIST SP 800-63B:
    prioriza tamanho em vez de regra de composição arbitrária (que empurra
    pra padrões previsíveis tipo 'Senha1!'), mas ainda barra os casos mais
    óbvios (só número, só uma palavra, senha = usuário). Retorna a mensagem
    de erro, ou None se a senha passa."""
    if not senha or len(senha) < 8:
        return "A senha deve ter pelo menos 8 caracteres."
    if not re.search(r'[A-Za-z]', senha) or not re.search(r'[0-9]', senha):
        return "A senha deve conter letras e números."
    if usuario and senha.lower() == usuario.strip().lower():
        return "A senha não pode ser igual ao nome de usuário."
    return None


SESSAO_LAST_SEEN_S = 60


def limpar_sessoes_expiradas():
    """Job do scheduler: apaga sessões vencidas (antes era feito em todo request)."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessoes WHERE expira_em < NOW()")
        conn.commit(); cur.close()
    except Exception as e:
        logger.error(f"Erro limpando sessões expiradas: {e}")
    finally:
        conn.close()


def get_session(token: str, page: str = ""):
    if not token:
        return None
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Leitura pura no caminho comum (2026-10-08). Antes todo request fazia
        # um DELETE das sessões vencidas na tabela inteira + UPDATE de
        # last_seen, e todo mundo disputava lock em `sessoes`. A limpeza agora
        # é job (limpar_sessoes_expiradas) e last_seen só é regravado quando
        # passou de SESSAO_LAST_SEEN_S ou a página mudou -- o "quem está
        # online" olha uma janela de 5 minutos, então 1 minuto de folga não muda nada.
        # Nome/perfil/área/cargo vêm do cadastro (`usuarios`), não da cópia
        # gravada em `sessoes` no login: com a cópia, mudança de cargo ou
        # renomeação de área só valia depois de relogar -- e /dashboard (que
        # lia o cargo atual pelo Service Desk) e o redirect (que lia a cópia)
        # discordavam e entravam em loop. Usuário desativado cai na hora.
        cur.execute("""
            SELECT s.usuario_id, u.nome, u.perfil, COALESCE(u.time, 'Projetos'), s.pagina, u.cargo,
                   COALESCE(s.last_seen < NOW() - make_interval(secs => %s), TRUE)
            FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id
            WHERE s.token = %s AND s.expira_em > NOW() AND u.ativo
        """, (SESSAO_LAST_SEEN_S, token))
        row = cur.fetchone()
        if row and (row[6] or (page and page != (row[4] or ""))):
            cur.execute("UPDATE sessoes SET last_seen = NOW(), pagina = COALESCE(%s, pagina) WHERE token = %s",
                        (page or None, token))
        conn.commit(); cur.close(); conn.close()
        if not row:
            return None
        row = row[:4] + ((page or row[4]),) + row[5:6]
        # Perfil 'dev' tem acesso equivalente a admin em todo o sistema (todas as
        # checagens de permissão existentes usam sess["perfil"]) -- "perfil_real"
        # preserva o valor de fato gravado no banco, só pra exibição/auditoria.
        perfil_real = row[2]
        perfil = "admin" if perfil_real == "dev" else perfil_real
        return {"id": row[0], "nome": row[1], "perfil": perfil, "perfil_real": perfil_real, "time": row[3], "page": row[4] or "", "cargo": row[5] or ""}
    except Exception as e:
        print(f"Erro get_session: {e}")
        return None
