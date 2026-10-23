"""Initialize a brand-new Neon PostgreSQL database for Doraemon Lakehouse.

This script deliberately refuses to start on a non-Neon host or a non-empty public
schema without this script's own migration ledger. It does not import or copy old data.
Run with a direct Neon connection string via DATABASE_URL_UNPOOLED.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
LEDGER = "lakehouse_bootstrap_migrations"


def choose_direct_neon_url() -> str:
    """Get and validate the direct Neon URL for DDL/session operations."""
    load_dotenv(ROOT / ".env")
    url = (os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL_UNPOOLED to the direct Neon connection URL.")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"postgres", "postgresql"} or not host.endswith(".neon.tech"):
        raise RuntimeError("Refusing bootstrap: destination must be a Neon PostgreSQL URL (*.neon.tech).")
    if "-pooler." in host:
        raise RuntimeError(
            "Refusing bootstrap through Neon pooler. Use DATABASE_URL_UNPOOLED (endpoint host without -pooler)."
        )
    if not parsed.path or parsed.path == "/":
        raise RuntimeError("Neon connection URL must include the target database name.")
    return url


def _migration_sql_without_transaction_wrappers(sql: str) -> str:
    """Remove file-level BEGIN/COMMIT so the migration and ledger row share one transaction."""
    sql = re.sub(r"(?im)^\s*BEGIN\s*;\s*(?:--[^\n]*)?$", "", sql)
    sql = re.sub(r"(?im)^\s*COMMIT\s*;\s*(?:--[^\n]*)?$", "", sql)
    return sql


def _ensure_empty_or_managed_database(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.lakehouse_bootstrap_migrations') IS NOT NULL AS ledger_exists")
        ledger_exists = bool(cur.fetchone()["ledger_exists"])
        if not ledger_exists:
            cur.execute(
                """SELECT count(*) AS object_count
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')"""
            )
            object_count = int(cur.fetchone()["object_count"])
            if object_count:
                raise RuntimeError(
                    f"Refusing bootstrap: public schema already contains {object_count} table/view/sequence objects. "
                    "This script is for a blank Neon database only; it will not touch an existing database."
                )
            cur.execute(
                f"""CREATE TABLE public.{LEDGER} (
                    migration_name text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
        conn.commit()


def _is_applied(conn, migration_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM public.{LEDGER} WHERE migration_name=%s", (migration_name,))
        applied = cur.fetchone() is not None
    conn.commit()  # close the read transaction before beginning the next migration transaction
    return applied


def _apply_named_script(conn, migration_name: str, sql: str) -> bool:
    if _is_applied(conn, migration_name):
        print(f"SKIP already applied: {migration_name}")
        return False
    with conn.transaction():
        # psycopg's simple-query mode supports the project's multi-statement SQL files.
        conn.execute(sql, prepare=False)
        conn.execute(
            f"INSERT INTO public.{LEDGER}(migration_name) VALUES(%s) ON CONFLICT DO NOTHING",
            (migration_name,),
        )
    print(f"APPLIED: {migration_name}")
    return True


def bootstrap() -> dict:
    url = choose_direct_neon_url()
    with psycopg.connect(
        url,
        row_factory=dict_row,
        connect_timeout=15,
        options="-c statement_timeout=120000 -c lock_timeout=15000",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS database_name, current_user AS database_user, version() AS server_version")
            info = cur.fetchone()
        print(f"Connected to Neon database={info['database_name']!r}, user={info['database_user']!r}")

        _ensure_empty_or_managed_database(conn)
        applied = 0
        schema_path = ROOT / "sql" / "schema.sql"
        if _apply_named_script(conn, "000_schema.sql", schema_path.read_text(encoding="utf-8")):
            applied += 1

        migration_dir = ROOT / "sql" / "migrations"
        for path in sorted(migration_dir.glob("*.sql")):
            sql = _migration_sql_without_transaction_wrappers(path.read_text(encoding="utf-8"))
            if _apply_named_script(conn, path.name, sql):
                applied += 1

        required_tables = {
            "data_sources", "pipeline_runs", "raw_feed_items", "news_articles", "topics", "entities",
            "entity_aliases", "article_topics", "article_entities", "fact_news_article", "fact_topic_daily",
            "fact_topic_trending", "fact_entity_daily", "fact_source_daily", "topic_aliases",
            "catalog_reconciliation_runs", "catalog_reconciliation_decisions", "catalog_reconciliation_merges",
            "app_users", "user_saved_timelines", "user_telegram_deliveries", "payment_requests", "user_password_reset_tokens",
        }
        with conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_type='BASE TABLE'"""
            )
            existing_tables = {row["table_name"] for row in cur.fetchall()}
            missing = sorted(required_tables - existing_tables)
            if missing:
                raise RuntimeError("Bootstrap verification failed; missing tables: " + ", ".join(missing))
            cur.execute("SELECT count(*) AS count FROM news_articles")
            articles = int(cur.fetchone()["count"])
            cur.execute("SELECT count(*) AS count FROM data_sources")
            sources = int(cur.fetchone()["count"])
            cur.execute("SELECT count(*) AS count FROM topics")
            topics = int(cur.fetchone()["count"])
            cur.execute("SELECT extname FROM pg_extension WHERE extname='pg_trgm'")
            has_trgm = cur.fetchone() is not None
        if not has_trgm:
            raise RuntimeError("Bootstrap verification failed: pg_trgm extension was not installed by migration 011.")

    result = {
        "database": info["database_name"],
        "migrations_applied_this_run": applied,
        "sources_seeded": sources,
        "starter_topics_seeded": topics,
        "articles": articles,
        "pg_trgm_enabled": has_trgm,
        "historical_data_migrated": False,
    }
    print("\nNeon bootstrap finished:")
    for key, value in result.items():
        print(f"  {key}: {value}")
    if articles:
        print("WARNING: this database contains articles. Bootstrap did not import them; inspect this database before proceeding.", file=sys.stderr)
    return result


def main() -> int:
    try:
        bootstrap()
        return 0
    except Exception as exc:
        print(f"NEON_BOOTSTRAP_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
