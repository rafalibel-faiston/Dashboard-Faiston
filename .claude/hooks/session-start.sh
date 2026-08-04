#!/bin/bash
# Prepara o ambiente para que a suíte de testes rode sem setup manual.
#
# O app não tem ORM nem camada de injeção -- os testes conversam com um
# Postgres de verdade (ver tests/conftest.py). Por segurança o conftest se
# recusa a rodar sem TEST_DATABASE_URL explícita, então este hook sobe um
# Postgres descartável e exporta a variável para a sessão.
set -euo pipefail

# Só faz sentido no ambiente remoto (Claude Code na web). Na máquina de
# alguém, mexer em Postgres e instalar pacote global seria invasivo.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
    exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

echo "[setup] instalando dependências Python..."
pip install --quiet --disable-pip-version-check -r requirements-dev.txt

# ── Postgres de teste ────────────────────────────────────────────────────────
PGDATA_DIR="/var/tmp/pgdata_faiston"
PGPORT=5433
PGSOCK="/var/tmp"
PGDB="faiston_test"

PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"
if [ -z "$PGBIN" ] || [ ! -x "$PGBIN/initdb" ]; then
    echo "[setup] Postgres não encontrado neste ambiente -- a suíte vai ser pulada."
    echo "[setup] (o conftest pula tudo quando TEST_DATABASE_URL não existe)"
    exit 0
fi

# initdb e postgres se recusam a rodar como root; quando for o caso,
# delega para um usuário sem privilégio.
run_pg() {
    if [ "$(id -u)" -eq 0 ]; then
        su nobody -s /bin/bash -c "$1"
    else
        bash -c "$1"
    fi
}

if "$PGBIN/pg_isready" -h "$PGSOCK" -p "$PGPORT" >/dev/null 2>&1; then
    echo "[setup] Postgres de teste já está no ar."
else
    if [ ! -s "$PGDATA_DIR/PG_VERSION" ]; then
        echo "[setup] inicializando cluster de teste..."
        rm -rf "$PGDATA_DIR"
        mkdir -p "$PGDATA_DIR"
        [ "$(id -u)" -eq 0 ] && chown nobody:nogroup "$PGDATA_DIR"
        run_pg "$PGBIN/initdb -D $PGDATA_DIR -U postgres -A trust" >/dev/null
    fi

    echo "[setup] subindo Postgres na porta $PGPORT..."
    run_pg "$PGBIN/pg_ctl -D $PGDATA_DIR -o '-p $PGPORT -k $PGSOCK' -l $PGDATA_DIR/log -w start" >/dev/null

    # pg_ctl -w já espera, mas em container lento vale a rede de segurança
    for _ in $(seq 1 20); do
        "$PGBIN/pg_isready" -h "$PGSOCK" -p "$PGPORT" >/dev/null 2>&1 && break
        sleep 0.5
    done
fi

if ! "$PGBIN/psql" -h "$PGSOCK" -p "$PGPORT" -U postgres -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$PGDB"; then
    echo "[setup] criando banco $PGDB..."
    "$PGBIN/psql" -h "$PGSOCK" -p "$PGPORT" -U postgres -qc "CREATE DATABASE $PGDB;" >/dev/null
fi

TEST_URL="postgresql://postgres@/$PGDB?host=$PGSOCK&port=$PGPORT"

# Persiste para o resto da sessão. ADMIN_INITIAL_PASSWORD é lido pelo
# setup_banco() ao criar o admin de seed -- sem ele a senha é aleatória e
# o conftest não consegue logar.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    {
        echo "export TEST_DATABASE_URL='$TEST_URL'"
        echo "export ADMIN_INITIAL_PASSWORD='senhaTeste123'"
        echo "export PATH=\"\$PATH:$PGBIN\""
    } >> "$CLAUDE_ENV_FILE"
fi

echo "[setup] pronto. Rode a suíte com: python3 -m pytest -q"
