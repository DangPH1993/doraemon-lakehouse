from contextlib import contextmanager
from urllib.parse import urlsplit
import psycopg
from psycopg.rows import dict_row
from .config import settings


def get_lock_database_url() -> str:
    """Return a direct PostgreSQL URL for session-scoped advisory locks.

    Neon pooled endpoints use transaction pooling and do not guarantee a stable server
    session, so advisory locks must use DATABASE_URL_UNPOOLED. Existing non-Neon installs
    can continue using DATABASE_URL when the separate value is not configured.
    """
    direct_url = (settings.database_url_unpooled or '').strip()
    url = direct_url or (settings.database_url or '').strip()
    if not url:
        raise RuntimeError('DATABASE_URL is not configured')
    hostname = (urlsplit(url).hostname or '').lower()
    if hostname.endswith('.neon.tech') and '-pooler.' in hostname:
        raise RuntimeError(
            'Session advisory locks require a direct Neon URL. Set DATABASE_URL_UNPOOLED '
            'to the Neon connection string without the -pooler suffix.'
        )
    return url


@contextmanager
def get_conn():
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is not configured')
    # Neon pooled endpoints use PgBouncer, which rejects statement_timeout and
    # other custom GUCs in libpq startup options. Apply limits with SET LOCAL
    # inside the transaction instead, so they work with both pooled and direct URLs.
    conn = psycopg.connect(
        settings.database_url, row_factory=dict_row, connect_timeout=12,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120s'")
            cur.execute("SET LOCAL lock_timeout = '15s'")
            cur.execute("SET LOCAL idle_in_transaction_session_timeout = '180s'")
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
