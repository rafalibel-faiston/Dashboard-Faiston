"""Conexão com o Postgres: pool dos requests e conexão avulsa dos jobs.

Saiu do main.py (2026-10-08) pra os routers de app/ poderem usar get_db()
sem importar o main (import circular).
"""
import contextvars
import logging
import os
import threading
import time as _time
import traceback

import psycopg2

logger = logging.getLogger("faiston")

# Rastreia as conexões de banco abertas durante cada request para garantir que
# sejam fechadas ao final — mesmo nos caminhos de erro que dão `raise` sem
# `conn.close()`. Sem isso, cada exceção vazava uma conexão e, acumulando,
# esgotava o pool do PostgreSQL e derrubava o app ("Banco offline") para todos.
_request_conns = contextvars.ContextVar("request_conns", default=None)


def _abrir_conexao(de_request):
    # connect_timeout: sem ele, banco lento/indisponível deixava a thread
    # do request pendurada pra sempre -- e o usuário via a tela "travada".
    conn = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=10)
    cur = conn.cursor()
    cur.execute("SET TIME ZONE 'America/Sao_Paulo'")
    if de_request:
        # Só em conexão de request (jobs de fundo ficam sem limite): uma
        # query ou uma espera por lock nunca segura o usuário mais que
        # isso, e uma transação esquecida aberta não prende a tabela
        # tarefas pro resto da empresa. Sem esses limites, um único lock
        # preso enfileirava todo mundo atrás ("o sistema trava").
        cur.execute("SET statement_timeout = '60s'")
        cur.execute("SET lock_timeout = '15s'")
        cur.execute("SET idle_in_transaction_session_timeout = '120s'")
    cur.close()
    conn.commit()
    return conn


# Pool de conexões dos requests (2026-10-08). Antes cada get_db() abria uma
# conexão TCP+TLS nova com o Neon e rodava 4 comandos de setup -- e um request
# típico chama get_db() pelo menos duas vezes (get_session + handler). Agora a
# conexão volta pro pool no close() e é reaproveitada pelo próximo request.
# Jobs de fundo continuam com conexão própria (sem os timeouts de request).
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "20"))
DB_POOL_ESPERA_S = float(os.environ.get("DB_POOL_ESPERA_S", "5"))
# Conexão parada há mais que isso é testada (SELECT 1) antes de ser entregue:
# o Neon derruba conexão ociosa quando suspende o compute.
DB_POOL_TESTAR_APOS_S = 60


class _PoolConexoes:
    def __init__(self, maximo):
        self._livres = []  # (conexão crua, momento em que voltou pro pool)
        self._lock = threading.Lock()
        self._vagas = threading.BoundedSemaphore(maximo)

    def pegar(self, espera):
        """Conexão crua do pool, ou None se todas estiverem em uso por mais
        de `espera` segundos."""
        if not self._vagas.acquire(timeout=espera):
            return None
        try:
            while True:
                with self._lock:
                    item = self._livres.pop() if self._livres else None
                if item is None:
                    return _abrir_conexao(de_request=True)
                conn, devolvida_em = item
                if conn.closed:
                    continue
                if _time.monotonic() - devolvida_em > DB_POOL_TESTAR_APOS_S:
                    try:
                        cur = conn.cursor(); cur.execute("SELECT 1"); cur.close()
                        conn.rollback()
                    except Exception:
                        try: conn.close()
                        except Exception: pass
                        continue
                return conn
        except Exception:
            self._vagas.release()
            raise

    def devolver(self, conn):
        try:
            if not conn.closed:
                # Desfaz transação deixada aberta (o close() de antes também
                # descartava) pra o próximo request pegar a conexão limpa.
                conn.rollback()
                with self._lock:
                    self._livres.append((conn, _time.monotonic()))
        except Exception:
            try: conn.close()
            except Exception: pass
        finally:
            self._vagas.release()


_pool_db = _PoolConexoes(DB_POOL_MAX)


class _ConexaoDoPool:
    """Embrulha a conexão do pool: close() devolve ao pool em vez de fechar,
    e depois dele a conexão (e os cursores abertos por ela) ficam
    inutilizáveis -- quem ainda tiver a referência nunca toca a conexão que
    já foi entregue pra outro request."""
    __slots__ = ("_conn", "_cursores")

    def __init__(self, conn):
        self._conn = conn
        self._cursores = []

    def _crua(self):
        if self._conn is None:
            raise psycopg2.InterfaceError("connection already closed")
        return self._conn

    def __getattr__(self, nome):
        if nome in _ConexaoDoPool.__slots__:
            raise AttributeError(nome)  # slot ainda não preenchido (__init__ falhou)
        return getattr(self._crua(), nome)

    def cursor(self, *args, **kwargs):
        cur = self._crua().cursor(*args, **kwargs)
        self._cursores.append(cur)
        return cur

    @property
    def closed(self):
        return 1 if self._conn is None else self._conn.closed

    def close(self):
        conn = self._conn
        if conn is None:
            return  # fechar duas vezes é no-op, como no psycopg2
        self._conn = None
        for cur in self._cursores:
            try: cur.close()
            except Exception: pass
        self._cursores = []
        _pool_db.devolver(conn)

    def __del__(self):
        # Rede de segurança pra caminho que esquece o close() fora do
        # middleware (ex.: BackgroundTasks, que rodam depois dele).
        try: self.close()
        except Exception: pass


def get_db():
    try:
        lst = _request_conns.get()
        if lst is None:
            return _abrir_conexao(de_request=False)
        crua = _pool_db.pegar(DB_POOL_ESPERA_S)
        if crua is None:
            # Pool esgotado: cai no comportamento antigo em vez de falhar.
            logger.warning("Pool de conexões esgotado; abrindo conexão avulsa")
            conn = _abrir_conexao(de_request=True)
        else:
            conn = _ConexaoDoPool(crua)
        # Registra a conexão para fechamento garantido ao fim do request (ver
        # middleware fechar_conexoes_db). Fechar duas vezes é seguro (no-op),
        # então os conn.close() já existentes continuam válidos.
        lst.append(conn)
        return conn
    except Exception as e:
        logger.error(f"Falha ao conectar ao banco: {e}\n{traceback.format_exc()}")
        return None
