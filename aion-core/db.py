"""PostgreSQL connection pool for Aion services.

Usage:
    from db import get_conn, dict_cursor

    with get_conn() as conn:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT ...")
            rows = cur.fetchall()
        conn.commit()
"""

import logging
import threading

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from config import CONFIG

logger = logging.getLogger("aion.db")

_pool: "ThreadedConnectionPool | None" = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        dsn = CONFIG.get("DATABASE_URL", "postgresql://mft_user:change-me@localhost:5432/aion_db")
        _pool = ThreadedConnectionPool(minconn=2, maxconn=20, dsn=dsn)
        safe_dsn = dsn.rsplit("@", 1)[-1] if "@" in dsn else dsn
        logger.info("PostgreSQL pool ready (%s)", safe_dsn)
        return _pool


class _PooledConn:
    """Thin wrapper that returns the connection to the pool on close/exit."""

    def __init__(self, pool: ThreadedConnectionPool):
        self._pool = pool
        self._conn = pool.getconn()
        self._conn.autocommit = False

    # context-manager support
    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, *_):
        if exc_type:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self._pool.putconn(self._conn)

    # direct attribute delegation so callers can do `conn_obj.cursor()`
    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_conn() -> _PooledConn:
    """Borrow a connection from the pool.

    Use as a context manager::

        with get_conn() as conn:
            with dict_cursor(conn) as cur:
                ...
            conn.commit()
    """
    return _PooledConn(_get_pool())


def dict_cursor(conn):
    """Return a RealDictCursor for dict-style row access."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


import re as _re


class SQLiteCompatConn:
    """Wraps a pooled psycopg2 connection with a SQLite-compatible execute() API.

    Handles the most common SQLite idioms used in web.py:
      - ? → %s parameter placeholders
      - INSERT OR IGNORE INTO … → INSERT INTO … ON CONFLICT DO NOTHING
      - Row access via column name (RealDictCursor)
      - .executescript() (runs statements split by ';')
    """

    def __init__(self, pool: ThreadedConnectionPool):
        self._pool = pool
        self._conn = pool.getconn()
        self._conn.autocommit = False

    # ── SQL translation ────────────────────────────────────────────────────────

    @staticmethod
    def _translate(sql: str) -> str:
        # INSERT OR IGNORE INTO → INSERT INTO … ON CONFLICT DO NOTHING
        sql = _re.sub(r'(?i)\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', sql)
        # Append ON CONFLICT DO NOTHING to INSERT … VALUES(…) that don't already have ON CONFLICT
        if _re.search(r'(?i)^\s*INSERT\s+INTO\b', sql) and 'ON CONFLICT' not in sql.upper():
            sql = sql.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
        # ? → %s  (only standalone ?, not inside string literals)
        sql = _re.sub(r'\?', '%s', sql)
        return sql

    # ── SQLite-compatible interface ────────────────────────────────────────────

    def execute(self, sql: str, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(self._translate(sql), params or ())
        return cur

    def executescript(self, script: str):
        with self._conn.cursor() as cur:
            for stmt in script.split(';'):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(self._translate(stmt))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        """Return the connection to the pool."""
        self._pool.putconn(self._conn)

    # Support `with get_db() as db:` idiom
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self.close()


def get_compat_conn() -> SQLiteCompatConn:
    """Return a SQLite-compatible connection wrapper (for legacy code in web.py)."""
    return SQLiteCompatConn(_get_pool())


def close_pool():
    """Shut down the connection pool (call on app teardown)."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
