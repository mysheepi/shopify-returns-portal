import os
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=os.environ["DATABASE_URL"],
        )
    return _pool


@contextmanager
def get_db():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db():
    migration_path = os.path.join(os.path.dirname(__file__), "migrations", "001_init.sql")
    with open(migration_path) as f:
        sql = f.read()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def query(conn, sql, params=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def fetchone(conn, sql, params=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()
