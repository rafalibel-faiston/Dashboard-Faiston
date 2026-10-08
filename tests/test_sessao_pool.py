"""
Testes da sessão sem escrita a cada request e do pool de conexões (2026-10-08).

get_session deixou de fazer DELETE das sessões vencidas + UPDATE de last_seen
em todo request (todo mundo disputava lock em `sessoes`): last_seen só é
regravado depois de SESSAO_LAST_SEEN_S ou quando a página muda, e a limpeza
virou o job limpar_sessoes_expiradas.

Roda contra um Postgres real de teste (ver tests/conftest.py).
"""
import secrets

import pytest


@pytest.fixture()
def sessao(app):
    """Sessão do admin inserida direto no banco; devolve (main, token)."""
    import main
    token = secrets.token_hex(16)
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM usuarios WHERE usuario = 'admin'")
    uid = cur.fetchone()[0]
    cur.execute("""INSERT INTO sessoes (token, usuario_id, nome, perfil, time_usuario, pagina, cargo, expira_em)
                   VALUES (%s, %s, 'Admin', 'admin', 'Projetos', 'inicio', '', NOW() + INTERVAL '1 hour')""",
                (token, uid))
    conn.commit(); cur.close(); conn.close()
    yield main, token
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessoes WHERE token = %s", (token,))
    conn.commit(); cur.close(); conn.close()


def _sql(main, q, params=()):
    conn = main.get_db()
    cur = conn.cursor()
    cur.execute(q, params)
    row = cur.fetchone() if cur.description else None
    conn.commit(); cur.close(); conn.close()
    return row


def test_last_seen_recente_nao_e_regravado(sessao):
    main, token = sessao
    _sql(main, "UPDATE sessoes SET last_seen = NOW() - INTERVAL '10 seconds' WHERE token = %s", (token,))
    antes = _sql(main, "SELECT last_seen FROM sessoes WHERE token = %s", (token,))[0]
    assert main.get_session(token)["id"]
    depois = _sql(main, "SELECT last_seen FROM sessoes WHERE token = %s", (token,))[0]
    assert depois == antes


def test_last_seen_antigo_e_regravado(sessao):
    main, token = sessao
    _sql(main, "UPDATE sessoes SET last_seen = NOW() - INTERVAL '5 minutes' WHERE token = %s", (token,))
    assert main.get_session(token)
    recente = _sql(main, "SELECT last_seen > NOW() - INTERVAL '10 seconds' FROM sessoes WHERE token = %s",
                   (token,))[0]
    assert recente


def test_troca_de_pagina_e_gravada(sessao):
    main, token = sessao
    _sql(main, "UPDATE sessoes SET last_seen = NOW() WHERE token = %s", (token,))
    sess = main.get_session(token, page="relatorio")
    assert sess["page"] == "relatorio"
    assert _sql(main, "SELECT pagina FROM sessoes WHERE token = %s", (token,))[0] == "relatorio"
    # Sem página informada, mantém a que já estava.
    assert main.get_session(token)["page"] == "relatorio"


def test_sessao_vencida_nega_e_job_apaga(sessao):
    main, token = sessao
    _sql(main, "UPDATE sessoes SET expira_em = NOW() - INTERVAL '1 minute' WHERE token = %s", (token,))
    assert main.get_session(token) is None
    main.limpar_sessoes_expiradas()
    assert _sql(main, "SELECT COUNT(*) FROM sessoes WHERE token = %s", (token,))[0] == 0


def test_requests_seguidos_reaproveitam_conexao(admin_client):
    import main
    for _ in range(5):
        assert admin_client.get("/api/admin/online").status_code == 200
    # Depois dos requests as conexões voltaram pro pool (nenhuma presa).
    assert main._pool_db._livres
