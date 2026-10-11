from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from .config import settings

@contextmanager
def get_conn():
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is not configured')
    conn = psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=12)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def fetch_all(query: str, params=()):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()

def fetch_one(query: str, params=()):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()

def execute(query: str, params=()):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount
