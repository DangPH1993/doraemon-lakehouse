"""Cloudflare Basin Catalog / Apache Iceberg adapter for Doraemon Lakehouse.

The compatibility mode mirrors PostgreSQL changes through an outbox. In direct-write mode,
news/article/topic/entity/relationship/FACT records are appended straight to Iceberg; PostgreSQL
contains only operational metadata such as source configuration, users, run status and CDC state. Current-state views are reconstructed from the latest
record version, so old Article/Topic/Entity/FACT rows remain queryable after hot data is
purged from PostgreSQL.

All access is opt-in. Set CLOUDFLARE_LAKEHOUSE_ENABLED=true only after applying migration
018 and configuring the Cloudflare R2 Data Catalog credentials.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import threading
import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import requests
from .db import get_conn

log = logging.getLogger("mini-lakehouse.cloudflare")

# Reuse read-only DuckDB connections in the API process. Opening a fresh in-memory
# DuckDB instance and attaching Basin Catalog for every SQL query added substantial
# latency to the News Radar page (which issues several queries on first load).
# The pool is bounded and serialized per connection by checkout: a connection is never
# used concurrently by two requests. Keep a connection warm longer than the slowest Iceberg
# snapshot refresh, otherwise each slow request ages out its own connection and every page
# triggers the expensive cold scan again. Snapshot TTL is deliberately several minutes to
# avoid repeatedly scanning remote event tables during pagination.
_READ_POOL_CONDITION = threading.Condition()
_READ_POOL_AVAILABLE: list[tuple[Any, float]] = []
_READ_POOL_CREATED = 0
_READ_POOL_CONFIG_KEY: tuple[str, ...] | None = None
_READ_POOL_MAX_AGE_SECONDS = 3600.0
_READ_POOL_ACQUIRE_TIMEOUT_SECONDS = 90.0
# Current-state views over the append-only Iceberg event log otherwise execute a
# row_number() scan on every API query. Materialize per-connection snapshots and reuse
# them briefly; this is the main fast path for the public article list.
_READ_SNAPSHOT_CACHE: dict[int, dict[str, float]] = {}
_READ_POOLED_CONN_IDS: set[int] = set()
_READ_SNAPSHOT_CACHE_LOCK = threading.RLock()
_READ_REGISTRY_CACHE: tuple[float, list[dict]] | None = None
_READ_REGISTRY_CACHE_LOCK = threading.RLock()


def _read_pool_size() -> int:
    try:
        # Public page loads issue several independent requests concurrently. Keep enough
        # readers to avoid one slow query blocking the entire page. Basin SQL can be used
        # for the heavy article-list and hot-topic reads; this pool serves other endpoints.
        return max(1, min(6, int(os.getenv("CLOUDFLARE_LAKEHOUSE_READ_POOL_SIZE", "3"))))
    except (TypeError, ValueError):
        return 3


def _read_snapshot_ttl_seconds() -> float:
    try:
        # The previous 30-second default was shorter than the observed 10-60s
        # Iceberg snapshot refresh. That made the next request refresh the same
        # remote tables again. Keep a minimum 2-minute snapshot even when an old
        # Render environment still supplies the former value of 30.
        return max(120.0, min(900.0, float(os.getenv("CLOUDFLARE_LAKEHOUSE_READ_SNAPSHOT_TTL_SECONDS", "300"))))
    except (TypeError, ValueError):
        return 300.0


def _forget_read_snapshot_cache(conn) -> None:
    with _READ_SNAPSHOT_CACHE_LOCK:
        conn_id = id(conn)
        _READ_SNAPSHOT_CACHE.pop(conn_id, None)
        _READ_POOLED_CONN_IDS.discard(conn_id)


def _registry_rows_cached() -> list[dict]:
    """Cache the tiny operational table registry briefly rather than querying PG on every read."""
    global _READ_REGISTRY_CACHE
    now = time.monotonic()
    with _READ_REGISTRY_CACHE_LOCK:
        if _READ_REGISTRY_CACHE and now - _READ_REGISTRY_CACHE[0] < 30.0:
            return [dict(row) for row in _READ_REGISTRY_CACHE[1]]
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT table_name,columns_json,bootstrap_status FROM lakehouse_table_registry WHERE bootstrap_status='complete'")
            rows = [dict(row) for row in cur.fetchall()]
        _READ_REGISTRY_CACHE = (now, rows)
        return [dict(row) for row in rows]


def _invalidate_read_registry_cache() -> None:
    global _READ_REGISTRY_CACHE
    with _READ_REGISTRY_CACHE_LOCK:
        _READ_REGISTRY_CACHE = None


def _read_pool_key() -> tuple[str, ...]:
    # The writer token is intentionally excluded; public queries must use the read-only token.
    return tuple(os.getenv(name, "").strip() for name in (
        "CLOUDFLARE_R2_WAREHOUSE", "CLOUDFLARE_R2_CATALOG_URI", "CLOUDFLARE_R2_READ_TOKEN"
    ))


@contextmanager
def _pooled_read_catalog_connection():
    """Checkout one dedicated read connection, reusing Basin Catalog attachment across queries."""
    global _READ_POOL_CREATED, _READ_POOL_CONFIG_KEY, _READ_POOL_AVAILABLE
    key = _read_pool_key()
    if not all(key):
        raise LakehouseNotReady(
            "Cloudflare read catalog is not configured. Set Warehouse, Catalog URI and CLOUDFLARE_R2_READ_TOKEN."
        )
    deadline = time.monotonic() + _READ_POOL_ACQUIRE_TIMEOUT_SECONDS
    conn = None
    created_at = None
    should_create = False
    with _READ_POOL_CONDITION:
        if _READ_POOL_CONFIG_KEY != key:
            # A live env/config change invalidates idle handles. Any in-flight connection
            # from the previous key is closed when its request releases it.
            for idle_conn, _ in _READ_POOL_AVAILABLE:
                _forget_read_snapshot_cache(idle_conn)
                try:
                    idle_conn.close()
                except Exception:
                    pass
            _READ_POOL_AVAILABLE = []
            _READ_POOL_CREATED = 0
            _READ_POOL_CONFIG_KEY = key
        while True:
            while _READ_POOL_AVAILABLE:
                candidate, candidate_created_at = _READ_POOL_AVAILABLE.pop()
                if time.monotonic() - candidate_created_at <= _READ_POOL_MAX_AGE_SECONDS:
                    conn, created_at = candidate, candidate_created_at
                    break
                _forget_read_snapshot_cache(candidate)
                try:
                    candidate.close()
                except Exception:
                    pass
                _READ_POOL_CREATED = max(0, _READ_POOL_CREATED - 1)
            if conn is not None:
                break
            if _READ_POOL_CREATED < _read_pool_size():
                _READ_POOL_CREATED += 1
                should_create = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                waited_ms = int(_READ_POOL_ACQUIRE_TIMEOUT_SECONDS * 1000)
                log.error("Cloudflare read pool exhausted: wait_ms=%s pool_size=%s", waited_ms, _read_pool_size())
                raise LakehouseNotReady("Timed out waiting for a free Cloudflare read connection; retry the request.")
            _READ_POOL_CONDITION.wait(remaining)
    if should_create:
        try:
            conn = _connect_catalog(read_only=True)
            created_at = time.monotonic()
            with _READ_SNAPSHOT_CACHE_LOCK:
                _READ_POOLED_CONN_IDS.add(id(conn))
        except Exception:
            with _READ_POOL_CONDITION:
                if _READ_POOL_CONFIG_KEY == key:
                    _READ_POOL_CREATED = max(0, _READ_POOL_CREATED - 1)
                _READ_POOL_CONDITION.notify()
            raise
    discard = False
    try:
        yield conn
    except Exception:
        # Syntax/binder errors do not poison DuckDB connections. Discard only if the
        # local engine connection itself is no longer usable.
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception:
            discard = True
        raise
    finally:
        with _READ_POOL_CONDITION:
            if (not discard and _READ_POOL_CONFIG_KEY == key
                    and time.monotonic() - (created_at or 0) <= _READ_POOL_MAX_AGE_SECONDS
                    and len(_READ_POOL_AVAILABLE) < _read_pool_size()):
                _READ_POOL_AVAILABLE.append((conn, created_at or time.monotonic()))
            else:
                _forget_read_snapshot_cache(conn)
                try:
                    conn.close()
                except Exception:
                    pass
                if _READ_POOL_CONFIG_KEY == key:
                    _READ_POOL_CREATED = max(0, _READ_POOL_CREATED - 1)
            _READ_POOL_CONDITION.notify()

# Bronze/raw is intentionally excluded. These are the clean core tables, bridge tables,
# dimensions and Gold facts needed by News Radar and the analysis layer.
TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "data_sources": ("source_id",),
    "news_articles": ("article_id",),
    "topics": ("topic_id",),
    "entities": ("entity_id",),
    "topic_aliases": ("topic_alias_id",),
    "entity_aliases": ("alias_id",),
    "article_topics": ("article_id", "topic_id"),
    "article_entities": ("article_id", "entity_id", "mention_text"),
    "fact_news_article": ("article_id",),
    "fact_topic_daily": ("stat_date", "topic_id", "source_id"),
    "fact_topic_trending": ("period_type", "period_start", "topic_id"),
    "fact_entity_daily": ("stat_date", "entity_id", "source_id"),
    "fact_source_daily": ("stat_date", "source_id"),
    "catalog_reconciliation_merges": ("object_type", "source_id"),
}

# Forward-only bootstrap is a genuinely empty start: it creates every registered Iceberg
# event table and captures the CDC watermark, but imports NO existing source rows, including
# dimensions/catalogs. Required dimension rows are hydrated lazily only when a post-cutover
# article/relationship references them, so the initial bootstrap stays fast regardless of
# the size of historical PostgreSQL tables.
FORWARD_ONLY_SKIP_TABLES = set(TABLE_KEYS)


class LakehouseNotReady(RuntimeError):
    pass


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def lakehouse_enabled() -> bool:
    return _flag("CLOUDFLARE_LAKEHOUSE_ENABLED")


def lakehouse_reads_enabled() -> bool:
    return lakehouse_enabled() and _flag("CLOUDFLARE_LAKEHOUSE_READS")


def direct_writes_requested() -> bool:
    """Return the explicit cutover intent even if a required companion flag is missing."""
    return _flag("CLOUDFLARE_LAKEHOUSE_DIRECT_WRITES")


def direct_writes_enabled() -> bool:
    """Enable the R2/Iceberg-first ingestion path only when the master flag is also on."""
    return lakehouse_enabled() and direct_writes_requested()


def read_configured() -> bool:
    # Render should have only a read-only token; never infer read permission from a writer secret.
    return all(os.getenv(k, "").strip() for k in (
        "CLOUDFLARE_R2_WAREHOUSE", "CLOUDFLARE_R2_CATALOG_URI", "CLOUDFLARE_R2_READ_TOKEN"
    ))


def basin_sql_reads_enabled() -> bool:
    """Whether public read paths should use Cloudflare's distributed Basin SQL engine.

    This stays opt-in because the Basin SQL API token needs Basin SQL + Catalog + R2 read
    permissions, and the API uses the R2 bucket name rather than the DuckDB warehouse URI.
    """
    return lakehouse_reads_enabled() and _flag("CLOUDFLARE_BASIN_SQL_READS") and all(
        os.getenv(name, "").strip() for name in (
            "CLOUDFLARE_BASIN_SQL_ACCOUNT_ID", "CLOUDFLARE_BASIN_SQL_BUCKET", "CLOUDFLARE_BASIN_SQL_READ_TOKEN"
        )
    )


def _basin_sql_column_expr(column: dict) -> str:
    """Project one JSON event field into a Basin SQL typed current-state column."""
    name = str(column["column_name"])
    quoted_name = '"' + name.replace('"', '""') + '"'
    json_key = _sql_quote(name)
    doc = "CAST(row_data AS JSON)"
    data_type = str(column.get("data_type") or "").lower()
    udt = str(column.get("udt_name") or "").lower()
    raw_json = f"json_get_json({doc}, {json_key})"

    def preserve_nulls(expression: str, sql_type: str) -> str:
        # Basin's typed JSON helpers return 0/false/empty-string for absent or nonmatching
        # values. That would turn nullable timestamps into CAST('' AS TIMESTAMP) failures
        # and nullable numeric fields into misleading zeroes. Inspect the raw JSON value
        # first so JSON null/missing stays SQL NULL while a real empty string stays empty.
        return (f"CASE WHEN {raw_json} IS NULL OR CAST({raw_json} AS VARCHAR) = 'null' "
                f"THEN CAST(NULL AS {sql_type}) ELSE {expression} END")

    if data_type in {"json", "jsonb"} or udt in {"json", "jsonb"}:
        expr = f"json_get_json({doc}, {json_key})"
    elif data_type == "boolean":
        expr = preserve_nulls(f"json_get_bool({doc}, {json_key})", "BOOLEAN")
    elif data_type in {"smallint", "integer", "bigint"}:
        expr = preserve_nulls(f"CAST(json_get_int({doc}, {json_key}) AS BIGINT)", "BIGINT")
    elif data_type in {"numeric", "decimal", "real", "double precision"}:
        expr = preserve_nulls(f"CAST(json_get_float({doc}, {json_key}) AS DOUBLE)", "DOUBLE")
    elif data_type == "date":
        expr = preserve_nulls(f"CAST(json_get_str({doc}, {json_key}) AS DATE)", "DATE")
    elif data_type in {"timestamp with time zone", "timestamp without time zone"}:
        expr = preserve_nulls(f"CAST(json_get_str({doc}, {json_key}) AS TIMESTAMP)", "TIMESTAMP")
    elif data_type == "array":
        # The Lakehouse's public keyword array is text[]; JSON arrays become SQL arrays.
        expr = f"json_get_array({doc}, {json_key})"
    else:
        expr = preserve_nulls(f"json_get_str({doc}, {json_key})", "VARCHAR")
    return f"{expr} AS {quoted_name}"


def _basin_current_state_ctes(table_names: list[str]) -> str:
    """Build read-only typed CTEs over the append-only Iceberg event tables."""
    wanted = list(dict.fromkeys(table_names))
    unknown = set(wanted) - set(TABLE_KEYS)
    if unknown:
        raise LakehouseNotReady(f"Unsupported Basin SQL view(s): {sorted(unknown)}")
    registry_rows = _registry_rows_cached()
    registry = {row["table_name"]: row for row in registry_rows}
    missing = set(wanted) - set(registry)
    if missing:
        raise LakehouseNotReady(f"Iceberg table(s) not bootstrapped: {sorted(missing)}")
    ctes = []
    for table_name in wanted:
        columns = registry[table_name]["columns_json"]
        if isinstance(columns, str):
            columns = json.loads(columns)
        projections = ", ".join(_basin_sql_column_expr(column) for column in columns)
        events = f"doraemon.{table_name}_events"
        ctes.append(
            f'"{table_name}" AS (SELECT {projections} FROM '
            f'(SELECT record_key, operation, row_data, event_id, '
            f'ROW_NUMBER() OVER (PARTITION BY record_key ORDER BY event_id DESC) AS __rn '
            f"FROM {events}) ranked WHERE __rn = 1 AND operation <> 'DELETE')"
        )
    return ",\n".join(ctes)


_BASIN_SQL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_BASIN_SQL_CACHE_LOCK = threading.RLock()
_BASIN_SQL_CACHE_TTL_SECONDS = 20.0
_BASIN_SQL_CACHE_MAX_ENTRIES = 128


def basin_sql_query_current(sql: str, table_names: list[str]) -> list[dict]:
    """Run a read-only query in Cloudflare Basin SQL against typed current-state CTEs.

    SQL input must be authored by this server from validated query parameters. The API does
    not support prepared parameters, so public callers must quote literals with
    ``basin_sql_literal`` rather than interpolate unchecked user input.
    """
    if not basin_sql_reads_enabled():
        raise LakehouseNotReady("Basin SQL reads are disabled or not fully configured")
    ctes = _basin_current_state_ctes(table_names)
    body = sql.strip().rstrip(";")
    if body[:5].upper() == "WITH ":
        full_sql = "WITH " + ctes + ",\n" + body[5:]
    else:
        full_sql = "WITH " + ctes + "\n" + body
    cache_key = hashlib.sha256(full_sql.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _BASIN_SQL_CACHE_LOCK:
        cached = _BASIN_SQL_CACHE.get(cache_key)
        if cached and now - cached[0] < _BASIN_SQL_CACHE_TTL_SECONDS:
            return [dict(row) for row in cached[1]]
        if cached:
            _BASIN_SQL_CACHE.pop(cache_key, None)
    account_id = os.getenv("CLOUDFLARE_BASIN_SQL_ACCOUNT_ID", "").strip()
    bucket = os.getenv("CLOUDFLARE_BASIN_SQL_BUCKET", "").strip()
    token = os.getenv("CLOUDFLARE_BASIN_SQL_READ_TOKEN", "").strip()
    url = f"https://api.sql.cloudflarestorage.com/api/v1/accounts/{account_id}/basin-sql/query/{bucket}"
    started = time.monotonic()
    try:
        response = requests.post(
            url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": full_sql}, timeout=(5, 75),
        )
        if not response.ok:
            detail = (response.text or "")[:500]
            raise LakehouseNotReady(f"Basin SQL HTTP {response.status_code}: {detail}")
        payload = response.json()
        if payload.get("success") is False:
            raise LakehouseNotReady(f"Basin SQL query failed: {str(payload.get('errors') or payload)[:500]}")
        result = payload.get("result") or {}
        rows = result.get("rows")
        if not isinstance(rows, list):
            raise LakehouseNotReady("Basin SQL returned an unexpected response format")
        safe_rows = [dict(row) for row in rows if isinstance(row, dict)]
        with _BASIN_SQL_CACHE_LOCK:
            expired = [key for key, (inserted_at, _) in _BASIN_SQL_CACHE.items()
                       if now - inserted_at >= _BASIN_SQL_CACHE_TTL_SECONDS]
            for key in expired:
                _BASIN_SQL_CACHE.pop(key, None)
            if len(_BASIN_SQL_CACHE) >= _BASIN_SQL_CACHE_MAX_ENTRIES and cache_key not in _BASIN_SQL_CACHE:
                oldest = min(_BASIN_SQL_CACHE, key=lambda key: _BASIN_SQL_CACHE[key][0])
                _BASIN_SQL_CACHE.pop(oldest, None)
            _BASIN_SQL_CACHE[cache_key] = (time.monotonic(), safe_rows)
        duration_ms = int((time.monotonic() - started) * 1000)
        if duration_ms >= 1000:
            log.warning("Slow Basin SQL read: duration_ms=%s tables=%s rows=%s", duration_ms,
                        ",".join(table_names), len(safe_rows))
        return [dict(row) for row in safe_rows]
    except requests.RequestException as exc:
        raise LakehouseNotReady(f"Basin SQL request failed: {str(exc)[:300]}") from exc


def basin_sql_literal(value: Any) -> str:
    """Safely quote a scalar literal for Basin SQL's non-parameterized HTTP API."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    return "'" + str(value).replace("'", "''") + "'"


def configured() -> bool:
    """Whether the process has the write credential required for bootstrap/sync/compact."""
    return all(os.getenv(k, "").strip() for k in (
        "CLOUDFLARE_R2_WAREHOUSE", "CLOUDFLARE_R2_CATALOG_URI", "CLOUDFLARE_R2_TOKEN"
    ))


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:
        raise LakehouseNotReady("DuckDB dependency is not installed; redeploy requirements.txt") from exc
    return duckdb


def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _connect_catalog(read_only: bool = False):
    token = os.getenv("CLOUDFLARE_R2_READ_TOKEN", "").strip() if read_only else os.getenv("CLOUDFLARE_R2_TOKEN", "").strip()
    if not all(os.getenv(k, "").strip() for k in ("CLOUDFLARE_R2_WAREHOUSE", "CLOUDFLARE_R2_CATALOG_URI")) or not token:
        if read_only:
            raise LakehouseNotReady("Cloudflare read catalog is not configured. Set Warehouse, Catalog URI and CLOUDFLARE_R2_READ_TOKEN.")
        raise LakehouseNotReady("Cloudflare write catalog is not configured. Set Warehouse, Catalog URI and CLOUDFLARE_R2_TOKEN.")
    duckdb = _duckdb()
    conn = duckdb.connect(database=":memory:")
    try:
        # LOAD is cheap on every request. Only download an extension on first use (or if
        # the container's extension cache was cleared), rather than INSTALLing on each API call.
        for extension in ("iceberg", "httpfs"):
            try:
                conn.execute(f"LOAD {extension}")
            except Exception:
                conn.execute(f"INSTALL {extension}")
                conn.execute(f"LOAD {extension}")
        conn.execute(
            "CREATE SECRET doraemon_r2_iceberg_secret (TYPE ICEBERG, TOKEN "
            + _sql_quote(token) + ")"
        )
        warehouse = os.environ["CLOUDFLARE_R2_WAREHOUSE"].strip()
        endpoint = os.environ["CLOUDFLARE_R2_CATALOG_URI"].strip()
        conn.execute(
            "ATTACH " + _sql_quote(warehouse) + " AS cf_lakehouse "
            "(TYPE ICEBERG, ENDPOINT " + _sql_quote(endpoint) + ")"
        )
        # The serving app uses a read-only token; namespace creation is a write operation.
        # Bootstrap creates the namespace before read traffic is enabled.
        if not read_only:
            conn.execute("CREATE SCHEMA IF NOT EXISTS cf_lakehouse.doraemon")
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def catalog_connection(read_only: bool = False):
    conn = _connect_catalog(read_only=read_only)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"Cannot JSON encode {type(value).__name__}")


def _row_json(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        # psycopg may expose a jsonb column as the decoded dict, but tolerate strings too.
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), default=_json_default)
        except (TypeError, ValueError):
            pass
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _record_key(table_name: str, row: dict, key_columns: tuple[str, ...] | list[str]) -> str:
    # Match PostgreSQL jsonb::text formatting in lakehouse_record_key(): JSONB arrays
    # include a single space after commas. Exact agreement is essential for CDC updates
    # to supersede bootstrap records instead of creating duplicate logical rows.
    return json.dumps([str(row.get(c, "")) if row.get(c) is not None else None for c in key_columns],
                      ensure_ascii=False)


def _event_table(table_name: str) -> str:
    if table_name not in TABLE_KEYS:
        raise ValueError(f"Unsupported Lakehouse table: {table_name}")
    return f"cf_lakehouse.doraemon.{table_name}_events"


def _ensure_event_table(conn, table_name: str) -> None:
    table = _event_table(table_name)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
        event_id BIGINT,
        record_key VARCHAR,
        operation VARCHAR,
        row_data VARCHAR,
        event_at VARCHAR
    )""")


def _insert_event_rows_bulk(conn, target: str, event_rows: list[tuple], statement_batch_size: int = 500) -> None:
    """Insert event rows using set-based multi-row INSERT statements.

    DuckDB's ``executemany`` executes the prepared INSERT repeatedly, which is extremely
    slow against Iceberg tables because every individual execution incurs row-level
    execution overhead. Multi-row VALUES statements keep the same transaction semantics
    while sending hundreds of records through one execution. The target is internal and
    always comes from ``_event_table``; payload values remain parameterized.
    """
    if not event_rows:
        return
    chunk_size = max(1, min(int(statement_batch_size), 500))
    for offset in range(0, len(event_rows), chunk_size):
        chunk = event_rows[offset:offset + chunk_size]
        values_sql = ", ".join(["(?, ?, ?, ?, ?)"] * len(chunk))
        parameters = tuple(value for row in chunk for value in row)
        conn.execute(f"INSERT INTO {target} VALUES {values_sql}", parameters)


def _rollback_if_active(conn) -> None:
    """Try to roll back without masking the original transaction/commit error."""
    try:
        conn.execute("ROLLBACK")
    except Exception as rollback_error:
        log.debug("Rollback skipped because no active DuckDB transaction remained: %s", rollback_error)


def _available_source_tables() -> list[str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_schema='public' AND table_type='BASE TABLE'""")
        names = {r["table_name"] for r in cur.fetchall()}
    return [name for name in TABLE_KEYS if name in names]


def _column_registry(table_name: str) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT column_name,data_type,udt_name,is_nullable,ordinal_position
                       FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=%s
                       ORDER BY ordinal_position""", (table_name,))
        return [dict(r) for r in cur.fetchall()]


def _register_table(table_name: str, columns: list[dict], status: str, row_count: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO lakehouse_table_registry
                       (table_name,primary_key_columns,columns_json,bootstrap_status,bootstrap_row_count,updated_at)
                       VALUES(%s,%s,%s::jsonb,%s,%s,now())
                       ON CONFLICT(table_name) DO UPDATE SET
                         primary_key_columns=excluded.primary_key_columns,
                         columns_json=excluded.columns_json,
                         bootstrap_status=excluded.bootstrap_status,
                         bootstrap_row_count=excluded.bootstrap_row_count,
                         updated_at=now()""",
                    (table_name, list(TABLE_KEYS[table_name]), json.dumps(columns), status, row_count))
    _invalidate_read_registry_cache()


def bootstrap(mode: str = "full") -> dict:
    """Initialize Iceberg from a consistent source watermark.

    ``full`` imports all current clean tables and facts. ``forward-only`` imports ZERO
    existing source rows: it creates empty event tables, captures the cutover watermark, and
    marks older outbox entries covered/ignored. Future CDC events populate the tables. Small
    topic/entity/source dimensions needed by new rows are hydrated lazily during sync, not
    bulk-loaded during bootstrap. Verification checks table readiness, not historical parity.
    """
    mode = str(mode or "full").strip().lower()
    if mode not in {"full", "forward-only"}:
        raise LakehouseNotReady("Bootstrap mode must be 'full' or 'forward-only'")
    skipped_tables = sorted(FORWARD_ONLY_SKIP_TABLES) if mode == "forward-only" else []
    if not configured():
        raise LakehouseNotReady("Cloudflare R2 Catalog environment variables are missing")
    if not lakehouse_enabled():
        raise LakehouseNotReady("Set CLOUDFLARE_LAKEHOUSE_ENABLED=true before bootstrap")
    available = _available_source_tables()
    if not available:
        raise LakehouseNotReady("No clean source tables are available for bootstrap")

    # A rebuild from PostgreSQL cannot reconstruct rows that have already been archived
    # out of PostgreSQL; preserve the existing Iceberg archive instead of overwriting it.
    archived_offsets = _get_state("archive_totals") or {}
    archived_any = any(int(value or 0) > 0 for value in archived_offsets.values())
    if archived_any:
        raise LakehouseNotReady(
            "Bootstrap is disabled after PostgreSQL archival. Restore/rehydrate the archived data first."
        )

    previous_snapshot = _get_state("bootstrap_snapshot") or {}
    if mode == "forward-only" and previous_snapshot.get("status") == "complete":
        raise LakehouseNotReady(
            "A completed Iceberg baseline already exists. Refusing to reset it with an empty forward-only bootstrap; "
            "clear the partial bootstrap state only after confirming no post-cutover data must be retained."
        )

    previous_progress = _get_state("bootstrap_progress") or {}
    snapshot_rows: dict[str, list[dict]] = {}
    columns_by_table: dict[str, list[dict]] = {}
    with get_conn() as pg:
        with pg.cursor() as cur:
            # Take every relation lock in one statement and capture the snapshot + watermark
            # before releasing the locks. Remote Iceberg I/O happens after those locks are gone.
            lock_targets = ", ".join(f'public."{name}"' for name in available)
            cur.execute(f"LOCK TABLE {lock_targets} IN SHARE MODE")
            cur.execute("SELECT coalesce(max(outbox_id),0)::bigint AS high_water FROM lakehouse_outbox")
            snapshot_watermark = int(cur.fetchone()["high_water"])
            for table_name in available:
                cur.execute("""SELECT column_name,data_type,udt_name,is_nullable,ordinal_position
                               FROM information_schema.columns
                               WHERE table_schema='public' AND table_name=%s
                               ORDER BY ordinal_position""", (table_name,))
                columns_by_table[table_name] = [dict(row) for row in cur.fetchall()]
                if mode == "forward-only" and table_name in FORWARD_ONLY_SKIP_TABLES:
                    # Do not fetch historical payloads at all. Keeping the typed table
                    # metadata is enough to create an empty Iceberg event table for CDC.
                    snapshot_rows[table_name] = []
                else:
                    cur.execute(f'SELECT * FROM public."{table_name}"')
                    snapshot_rows[table_name] = [dict(row) for row in cur.fetchall()]

    row_counts = {name: len(snapshot_rows[name]) for name in available}
    can_resume = (
        previous_progress.get("status") == "running"
        and previous_progress.get("mode", "full") == mode
        and int(previous_progress.get("outbox_high_water", -1)) == snapshot_watermark
        and previous_progress.get("table_row_counts") == row_counts
        and previous_progress.get("source_tables") == available
    )
    if can_resume:
        progress = previous_progress
        progress.setdefault("completed_tables", [])
        progress.setdefault("tables", {})
        log.info(
            "Cloudflare Lakehouse bootstrap: resuming snapshot watermark=%s completed_tables=%s",
            snapshot_watermark, len(progress["completed_tables"]),
        )
    else:
        progress = {
            "status": "running",
            "mode": mode,
            "skipped_historical_tables": skipped_tables,
            "outbox_high_water": snapshot_watermark,
            "source_tables": available,
            "table_row_counts": row_counts,
            "completed_tables": [],
            "tables": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _set_state("bootstrap_progress", progress)
        log.info(
            "Cloudflare Lakehouse bootstrap: starting mode=%s snapshot watermark=%s tables=%s rows_to_import=%s historical_tables_skipped=%s",
            mode, snapshot_watermark, len(available), sum(row_counts.values()), skipped_tables,
        )

    batch_size = int(os.getenv("CLOUDFLARE_LAKEHOUSE_BOOTSTRAP_BATCH_SIZE", "500"))
    batch_size = max(100, min(batch_size, 10000))
    counts: dict[str, int] = {}

    def save_progress() -> None:
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        _set_state("bootstrap_progress", progress)

    with catalog_connection() as con:
        for table_name in available:
            columns = columns_by_table[table_name]
            # Stable key ordering makes partial batches resumable across runner restarts.
            rows = sorted(
                snapshot_rows[table_name],
                key=lambda row: _record_key(table_name, row, TABLE_KEYS[table_name]),
            )
            expected_rows = len(rows)
            if not columns:
                raise LakehouseNotReady(f"No column metadata found for source table {table_name}")

            target = _event_table(table_name)
            # A prior failed bootstrap may have left large partial baseline tables (for
            # example thousands of topics). With the empty forward-only mode, replace those
            # partial event tables once instead of issuing row-wise DELETEs against them.
            if mode == "forward-only" and not can_resume:
                con.execute(f"DROP TABLE IF EXISTS {target}")
            _ensure_event_table(con, table_name)
            table_progress = progress.get("tables", {}).get(table_name, {})
            is_marked_complete = (
                can_resume
                and table_name in progress.get("completed_tables", [])
                and table_progress.get("status") == "complete"
                and int(table_progress.get("expected_rows", -1)) == expected_rows
            )
            if is_marked_complete:
                # The progress checkpoint is written only after the Iceberg batch commits
                # and the PG registry marks this table complete.
                counts[table_name] = expected_rows
                log.info("Cloudflare Lakehouse bootstrap: table=%s already complete rows=%s; skipping",
                         table_name, expected_rows)
                continue

            resume_from = 0
            if can_resume and table_progress.get("status") == "running":
                # Check the committed prefix rather than trusting a checkpoint count: the
                # runner could have been cancelled between an Iceberg commit and PG update.
                existing = con.execute(
                    f"SELECT record_key FROM {target} WHERE event_id = ? AND operation = 'UPSERT'",
                    [snapshot_watermark],
                ).fetchall()
                existing_keys = [str(item[0]) for item in existing]
                existing_count = len(existing_keys)
                total_count = int(con.execute(f"SELECT count(*) FROM {target}").fetchone()[0])
                prefix_keys = [
                    _record_key(table_name, row, TABLE_KEYS[table_name])
                    for row in rows[:existing_count]
                ] if existing_count <= expected_rows else []
                if (
                    total_count == existing_count
                    and existing_count <= expected_rows
                    and len(set(existing_keys)) == existing_count
                    and set(existing_keys) == set(prefix_keys)
                ):
                    resume_from = existing_count
                else:
                    log.warning(
                        "Cloudflare Lakehouse bootstrap: partial table=%s is not a valid resumable prefix; rebuilding",
                        table_name,
                    )

            if resume_from == 0:
                # Rebuild just this table when starting a fresh snapshot or when its
                # partial prefix cannot be validated. Commit the empty table before batches.
                con.execute("BEGIN TRANSACTION")
                try:
                    con.execute(f"DELETE FROM {target}")
                    con.execute("COMMIT")
                except Exception:
                    _rollback_if_active(con)
                    raise

            _register_table(table_name, columns, "running", expected_rows)
            progress.setdefault("tables", {})[table_name] = {
                "status": "running", "expected_rows": expected_rows,
                "loaded_rows": resume_from, "started_at": datetime.now(timezone.utc).isoformat(),
            }
            save_progress()
            log.info(
                "Cloudflare Lakehouse bootstrap: table=%s starting rows=%s resume_from=%s batch_size=%s",
                table_name, expected_rows, resume_from, batch_size,
            )

            for offset in range(resume_from, expected_rows, batch_size):
                batch = rows[offset:offset + batch_size]
                event_rows = [
                    (snapshot_watermark,
                     _record_key(table_name, row, TABLE_KEYS[table_name]),
                     "UPSERT", _row_json(row), datetime.now(timezone.utc).isoformat())
                    for row in batch
                ]
                con.execute("BEGIN TRANSACTION")
                try:
                    _insert_event_rows_bulk(con, target, event_rows)
                    con.execute("COMMIT")
                except Exception:
                    _rollback_if_active(con)
                    raise

                loaded_rows = offset + len(batch)
                progress["tables"][table_name]["loaded_rows"] = loaded_rows
                save_progress()
                log.info(
                    "Cloudflare Lakehouse bootstrap: table=%s progress=%s/%s",
                    table_name, loaded_rows, expected_rows,
                )

            _register_table(table_name, columns, "complete", expected_rows)
            progress["tables"][table_name] = {
                "status": "complete", "expected_rows": expected_rows,
                "loaded_rows": expected_rows, "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            if table_name not in progress["completed_tables"]:
                progress["completed_tables"].append(table_name)
            counts[table_name] = expected_rows
            save_progress()
            log.info("Cloudflare Lakehouse bootstrap: table=%s complete rows=%s watermark=%s",
                     table_name, expected_rows, snapshot_watermark)

    # All source rows at or before the snapshot watermark are already represented by the
    # snapshot. Mark them handled so stale payloads cannot overwrite the baseline.
    with get_conn() as pg, pg.cursor() as cur:
        cur.execute("""UPDATE lakehouse_outbox SET processed_at=coalesce(processed_at,now()),processing_error=NULL
                       WHERE outbox_id <= %s AND processed_at IS NULL""", (snapshot_watermark,))
        handled_outbox = int(cur.rowcount)
    snapshot_state = {
        "status": "complete", "mode": mode, "skipped_historical_tables": skipped_tables,
        "outbox_high_water": snapshot_watermark,
        "tables": counts, "total_rows": sum(counts.values()),
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }
    _set_state("bootstrap_snapshot", snapshot_state)
    progress["status"] = "complete"
    progress["completed_at"] = datetime.now(timezone.utc).isoformat()
    progress["outbox_rows_covered_by_snapshot"] = handled_outbox
    save_progress()
    return {"status": "bootstrapped", "mode": mode,
            "skipped_historical_tables": skipped_tables,
            "outbox_high_water": snapshot_watermark,
            "outbox_rows_covered_by_snapshot": handled_outbox,
            "tables": counts, "total_rows": sum(counts.values())}

def _hydrate_missing_dimensions(con, outbox_rows: list[dict], registered_tables: set[str]) -> dict[str, int]:
    """Copy only dimension rows referenced by post-cutover changes, never whole catalogs.

    A fresh forward-only baseline intentionally has no source/topic/entity rows. New articles
    still point to stable PostgreSQL source/topic/entity IDs, so hydrate only those referenced
    rows (and their aliases) before inserting the new relationships. This avoids orphan joins
    in Radar while keeping bootstrap at zero source rows.
    """
    needed: dict[str, dict[str, int]] = {name: {} for name in ("data_sources", "topics", "entities")}
    for event in outbox_rows:
        if str(event.get("operation")) != "UPSERT":
            continue
        row = event.get("row_data")
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except (TypeError, ValueError):
                continue
        if not isinstance(row, dict):
            continue
        source_event_id = int(event["outbox_id"])
        table_name = str(event.get("table_name") or "")
        ref_fields: list[tuple[str, str]] = []
        if table_name in {"news_articles", "fact_source_daily", "fact_topic_daily", "fact_entity_daily"}:
            ref_fields.append(("data_sources", "source_id"))
        if table_name in {"article_topics", "fact_topic_daily", "fact_topic_trending"}:
            ref_fields.append(("topics", "topic_id"))
        if table_name in {"article_entities", "fact_entity_daily"}:
            ref_fields.append(("entities", "entity_id"))
        for dimension, field in ref_fields:
            value = row.get(field)
            if value is None:
                continue
            key = str(value)
            needed[dimension][key] = max(source_event_id, needed[dimension].get(key, 0))

    hydrated: dict[str, int] = {name: 0 for name in needed}

    def append_missing(table_name: str, requested: dict[str, int]) -> list[dict]:
        if not requested or table_name not in registered_tables or table_name not in TABLE_KEYS:
            return []
        key_column = TABLE_KEYS[table_name][0]
        target = _event_table(table_name)
        keys = [_record_key(table_name, {key_column: value}, TABLE_KEYS[table_name]) for value in requested]
        placeholders = ",".join(["?"] * len(keys))
        existing_rows = con.execute(
            f"""SELECT record_key,operation FROM (
                    SELECT record_key,operation,row_number() OVER (PARTITION BY record_key ORDER BY event_id DESC) AS rn
                    FROM {target} WHERE record_key IN ({placeholders})
                ) WHERE rn=1""", keys
        ).fetchall()
        alive = {str(item[0]) for item in existing_rows if str(item[1]) != "DELETE"}
        missing_ids = [value for value in requested if _record_key(table_name, {key_column: value}, TABLE_KEYS[table_name]) not in alive]
        if not missing_ids:
            return []
        pg_ids: list[Any] = []
        for value in missing_ids:
            try:
                pg_ids.append(int(value))
            except (TypeError, ValueError):
                pg_ids.append(value)
        with get_conn() as pg, pg.cursor() as cur:
            cur.execute(f'SELECT * FROM public."{table_name}" WHERE "{key_column}" = ANY(%s)', (pg_ids,))
            records = [dict(item) for item in cur.fetchall()]
        if records:
            event_rows = []
            for record in records:
                record_id = str(record.get(key_column))
                event_rows.append((requested[record_id], _record_key(table_name, record, TABLE_KEYS[table_name]),
                                   "UPSERT", _row_json(record), datetime.now(timezone.utc).isoformat()))
            _insert_event_rows_bulk(con, target, event_rows)
        return records

    for dimension, requested in needed.items():
        requested_items = list(requested.items())
        # Keep parameter lists bounded when a single sync batch references many dimensions.
        for offset in range(0, len(requested_items), 500):
            request_chunk = dict(requested_items[offset:offset + 500])
            records = append_missing(dimension, request_chunk)
            hydrated[dimension] += len(records)
            # Check aliases for every referenced dimension, not only newly inserted
            # dimensions: a dimension may have arrived in an earlier CDC event while its
            # pre-cutover aliases were intentionally omitted from the empty baseline.
            alias_table = "topic_aliases" if dimension == "topics" else ("entity_aliases" if dimension == "entities" else None)
            fk_column = "topic_id" if dimension == "topics" else ("entity_id" if dimension == "entities" else None)
            if alias_table and fk_column and alias_table in registered_tables and request_chunk:
                ids: list[Any] = []
                for value in request_chunk:
                    try:
                        ids.append(int(value))
                    except (TypeError, ValueError):
                        ids.append(value)
                with get_conn() as pg, pg.cursor() as cur:
                    cur.execute(f'SELECT * FROM public."{alias_table}" WHERE "{fk_column}" = ANY(%s)', (ids,))
                    aliases = [dict(item) for item in cur.fetchall()]
                alias_target = _event_table(alias_table)
                for alias_offset in range(0, len(aliases), 500):
                    alias_chunk = aliases[alias_offset:alias_offset + 500]
                    if not alias_chunk:
                        continue
                    key_values = [_record_key(alias_table, item, TABLE_KEYS[alias_table]) for item in alias_chunk]
                    placeholders = ",".join(["?"] * len(key_values))
                    current = con.execute(
                        f"""SELECT record_key,operation FROM (
                               SELECT record_key,operation,row_number() OVER (PARTITION BY record_key ORDER BY event_id DESC) AS rn
                               FROM {alias_target} WHERE record_key IN ({placeholders})
                           ) WHERE rn=1""", key_values
                    ).fetchall()
                    already = {str(item[0]) for item in current if str(item[1]) != "DELETE"}
                    alias_events = []
                    for alias in alias_chunk:
                        key = _record_key(alias_table, alias, TABLE_KEYS[alias_table])
                        if key in already:
                            continue
                        dimension_id = str(alias.get(fk_column))
                        alias_events.append((request_chunk.get(dimension_id, 0), key, "UPSERT",
                                             _row_json(alias), datetime.now(timezone.utc).isoformat()))
                    if alias_events:
                        _insert_event_rows_bulk(con, alias_target, alias_events)
                        hydrated[alias_table] = hydrated.get(alias_table, 0) + len(alias_events)
    # Resolve only merge-map rows connected to referenced topic/entity IDs. The map may
    # contain older reconciliations that predate the empty baseline; importing those small
    # connected paths preserves canonical timeline behavior without loading the whole map.
    merge_table = "catalog_reconciliation_merges"
    if merge_table in registered_tables:
        merge_target = _event_table(merge_table)
        for object_type, dimension in (("topic", "topics"), ("entity", "entities")):
            frontier = dict(needed[dimension])
            visited: set[str] = set()
            for _depth in range(20):
                frontier = {key: event_id for key, event_id in frontier.items() if key not in visited}
                if not frontier:
                    break
                visited.update(frontier)
                pg_ids = []
                for value in frontier:
                    try:
                        pg_ids.append(int(value))
                    except (TypeError, ValueError):
                        pg_ids.append(value)
                with get_conn() as pg, pg.cursor() as cur:
                    cur.execute(
                        'SELECT * FROM public."catalog_reconciliation_merges" WHERE object_type=%s AND source_id = ANY(%s)',
                        (object_type, pg_ids),
                    )
                    merges = [dict(item) for item in cur.fetchall()]
                if not merges:
                    break
                keys = [_record_key(merge_table, item, TABLE_KEYS[merge_table]) for item in merges]
                key_chunks = []
                for offset in range(0, len(keys), 500):
                    key_chunks.extend(keys[offset:offset + 500])
                active_keys: set[str] = set()
                for offset in range(0, len(key_chunks), 500):
                    key_chunk = key_chunks[offset:offset + 500]
                    placeholders = ",".join(["?"] * len(key_chunk))
                    current = con.execute(
                        f"""SELECT record_key,operation FROM (
                               SELECT record_key,operation,row_number() OVER (PARTITION BY record_key ORDER BY event_id DESC) AS rn
                               FROM {merge_target} WHERE record_key IN ({placeholders})
                           ) WHERE rn=1""", key_chunk
                    ).fetchall()
                    active_keys.update(str(item[0]) for item in current if str(item[1]) != "DELETE")
                merge_events = []
                next_frontier: dict[str, int] = {}
                for merge in merges:
                    source_id = str(merge.get("source_id"))
                    event_id = frontier.get(source_id, 0)
                    target_id = str(merge.get("target_id"))
                    next_frontier[target_id] = max(event_id, next_frontier.get(target_id, 0))
                    key = _record_key(merge_table, merge, TABLE_KEYS[merge_table])
                    if key not in active_keys:
                        merge_events.append((event_id, key, "UPSERT", _row_json(merge),
                                             datetime.now(timezone.utc).isoformat()))
                for offset in range(0, len(merge_events), 500):
                    _insert_event_rows_bulk(con, merge_target, merge_events[offset:offset + 500])
                if merge_events:
                    hydrated[merge_table] = hydrated.get(merge_table, 0) + len(merge_events)
                frontier = next_frontier

    nonzero = {name: count for name, count in hydrated.items() if count}
    if nonzero:
        log.info("Cloudflare Lakehouse lazy dimension hydration: %s", nonzero)
    return hydrated


def sync_pending(limit: int = 10000) -> dict:
    """Append pending PostgreSQL outbox changes into Iceberg event tables.

    Outbox rows are marked processed only after Iceberg accepted them. On a fresh forward-only
    baseline, only dimensions referenced by new article/link/fact events are hydrated, avoiding
    a full copy of historical source/topic/entity catalogs.
    """
    if not configured() or not lakehouse_enabled():
        raise LakehouseNotReady("Enable/configure Cloudflare Lakehouse before syncing")
    if direct_writes_enabled():
        # In direct mode, article/catalog/FACT rows are authoritative in Iceberg. Do not
        # mirror stale PostgreSQL compatibility tables back over the direct dataset.
        # Only operational data_sources changes are allowed to flow through the outbox.
        direct_owned = {"news_articles", "topics", "entities", "topic_aliases", "entity_aliases",
                        "article_topics", "article_entities", "fact_news_article", "fact_topic_daily",
                        "fact_topic_trending", "fact_entity_daily", "fact_source_daily",
                        "catalog_reconciliation_merges"}
    else:
        direct_owned = set()
    limit = max(1, min(int(limit), 50000))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT table_name,bootstrap_status FROM lakehouse_table_registry")
        registry = {row["table_name"]: row["bootstrap_status"] for row in cur.fetchall()}
        if direct_owned:
            # Any clean-data mutations accidentally emitted by a legacy PG-only admin/reconcile
            # path must not overwrite Iceberg's authoritative direct-write dataset.
            # Drop legacy clean-row payloads from the PostgreSQL outbox after cutover. These
            # historical duplicates must not consume PG storage or be replayed over the direct
            # Iceberg dataset. Keep only event metadata/keys for auditability; business clean
            # tables themselves are left untouched until a separately approved cleanup.
            cur.execute("""UPDATE lakehouse_outbox SET processed_at=coalesce(processed_at,now()),
                           processing_error='ignored: Iceberg direct-write mode is authoritative',
                           row_data='{}'::jsonb
                           WHERE table_name=ANY(%s::text[])
                             AND (processed_at IS NULL OR row_data <> '{}'::jsonb)""", (sorted(direct_owned),))
        pending_tables = [t for t, status in registry.items() if status == "complete" and t not in direct_owned]
        if not pending_tables:
            raise LakehouseNotReady("No completed bootstrap tables; run bootstrap first")
        cur.execute("""SELECT outbox_id,table_name,operation,record_key,row_data,event_at
                       FROM lakehouse_outbox
                       WHERE processed_at IS NULL AND table_name=ANY(%s::text[])
                       ORDER BY outbox_id
                       LIMIT %s""", (pending_tables, limit))
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {"status": "up_to_date", "events_synced": 0, "tables": {}}
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["table_name"], []).append(row)
    ids_by_table: dict[str, list[int]] = {}
    with catalog_connection() as con:
        _hydrate_missing_dimensions(con, rows, {t for t, status in registry.items() if status == "complete"})
        for table_name, events in grouped.items():
            _ensure_event_table(con, table_name)
            target = _event_table(table_name)
            event_rows = [
                (int(e["outbox_id"]), str(e["record_key"]), str(e["operation"]),
                 _row_json(e["row_data"]),
                 e["event_at"].isoformat() if hasattr(e["event_at"], "isoformat") else str(e["event_at"]))
                for e in events
            ]
            # Commit bounded chunks so transient Catalog/R2 errors do not hold one very
            # large transaction open. Outbox IDs are acknowledged only after all chunks
            # for the selected sync batch have been written successfully.
            for offset in range(0, len(event_rows), 500):
                chunk = event_rows[offset:offset + 500]
                con.execute("BEGIN TRANSACTION")
                try:
                    _insert_event_rows_bulk(con, target, chunk)
                    con.execute("COMMIT")
                except Exception:
                    _rollback_if_active(con)
                    raise
            ids_by_table[table_name] = [int(e["outbox_id"]) for e in events]
    synced_ids = [int(row["outbox_id"]) for row in rows]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE lakehouse_outbox SET processed_at=now(),processing_error=NULL
                       WHERE outbox_id=ANY(%s::bigint[])""", (synced_ids,))
        # The durable CDC history is in Iceberg. Keep a short PostgreSQL retry/audit window
        # only, so the outbox itself does not become another growing storage table.
        cur.execute("""WITH old_rows AS (
                         SELECT outbox_id FROM lakehouse_outbox
                         WHERE processed_at < now()-interval '7 days'
                         ORDER BY outbox_id LIMIT 20000
                       ) DELETE FROM lakehouse_outbox o USING old_rows x WHERE o.outbox_id=x.outbox_id""")
    per_table = {name: len(items) for name, items in grouped.items()}
    log.info("Cloudflare Lakehouse CDC sync: events=%s tables=%s", len(rows), per_table)
    return {"status": "synced", "events_synced": len(synced_ids), "tables": per_table}


def pending_count() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*)::bigint AS n FROM lakehouse_outbox WHERE processed_at IS NULL")
        return int(cur.fetchone()["n"])


def _duck_type(column: dict) -> str:
    data_type = str(column.get("data_type") or "").lower()
    udt = str(column.get("udt_name") or "").lower()
    if data_type in {"json", "jsonb"} or udt in {"json", "jsonb"}:
        return "JSON"
    if data_type == "array":
        return "VARCHAR[]" if udt in {"_text", "_varchar", "_bpchar"} else "JSON"
    if data_type in {"character varying", "character", "text", "name"}:
        return "VARCHAR"
    if data_type == "boolean":
        return "BOOLEAN"
    if data_type in {"smallint"}:
        return "SMALLINT"
    if data_type in {"integer"}:
        return "INTEGER"
    if data_type in {"bigint"}:
        return "BIGINT"
    if data_type in {"numeric", "decimal"}:
        return "DECIMAL(38,10)"
    if data_type in {"real"}:
        return "REAL"
    if data_type in {"double precision"}:
        return "DOUBLE"
    if data_type == "date":
        return "DATE"
    if data_type == "timestamp with time zone":
        return "TIMESTAMPTZ"
    if data_type == "timestamp without time zone":
        return "TIMESTAMP"
    if data_type == "uuid":
        return "VARCHAR"
    return "VARCHAR"


def _column_expression(column: dict) -> str:
    name = str(column["column_name"])
    q_name = '"' + name.replace('"', '""') + '"'
    path = "$." + name
    path_lit = _sql_quote(path)
    typ = _duck_type(column)
    if typ == "JSON":
        expr = f"try_cast(json_extract(row_data, {path_lit}) AS JSON)"
    elif typ.endswith("[]") or typ == "JSON":
        expr = f"try_cast(json_extract(row_data, {path_lit}) AS {typ})"
    else:
        expr = f"try_cast(json_extract_string(row_data, {path_lit}) AS {typ})"
    return f"{expr} AS {q_name}"


def _create_current_state_view(con, table_name: str, columns: list[dict], view_name: str | None = None) -> None:
    """Expose current-state rows; pooled API sessions materialize short-lived snapshots.

    The backing Iceberg tables are append-only event logs, so a ROW_NUMBER() current-state
    view forces repeated scans of the same remote data on every endpoint call. Pooled API
    connections keep a connection-local TEMP TABLE snapshot for a short TTL and expose the
    public / *_raw names as cheap views over it. Direct pipeline, verification and migration
    connections do not cache, so they always see fresh data and cannot leak a cache across
    closed DuckDB sessions.
    """
    projections = ", ".join(_column_expression(c) for c in columns)
    event_table = _event_table(table_name)
    output_name = view_name or table_name
    quoted_output = '"' + output_name.replace('"', '""') + '"'
    conn_id = id(con)
    with _READ_SNAPSHOT_CACHE_LOCK:
        use_snapshot_cache = conn_id in _READ_POOLED_CONN_IDS
        last_materialized = _READ_SNAPSHOT_CACHE.get(conn_id, {}).get(table_name, 0.0)
    if not use_snapshot_cache:
        con.execute(f"""CREATE OR REPLACE TEMP VIEW {quoted_output} AS
            WITH ranked AS (
              SELECT record_key,operation,row_data,event_id,
                     row_number() OVER (PARTITION BY record_key ORDER BY event_id DESC) AS rn
              FROM {event_table}
            )
            SELECT {projections}
            FROM ranked WHERE rn=1 AND operation <> 'DELETE'""")
        return

    snapshot_name = f"__lh_current_state_{table_name}"
    quoted_snapshot = '"' + snapshot_name.replace('"', '""') + '"'
    now = time.monotonic()
    refresh = (not last_materialized) or (now - last_materialized >= _read_snapshot_ttl_seconds())
    ranked_cte = f"""WITH ranked AS (
              SELECT record_key,operation,row_data,event_id,
                     row_number() OVER (PARTITION BY record_key ORDER BY event_id DESC) AS rn
              FROM {event_table}
            )"""
    state_select = f"SELECT {projections} FROM ranked WHERE rn=1 AND operation <> 'DELETE'"
    if refresh and last_materialized:
        # Keep dependent TEMP VIEWs valid while refreshing the snapshot in-place. The
        # transaction makes readers see either the previous snapshot or the new one.
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f"DELETE FROM {quoted_snapshot}")
            con.execute(f"{ranked_cte} INSERT INTO {quoted_snapshot} {state_select}")
            con.execute("COMMIT")
        except Exception:
            _rollback_if_active(con)
            raise
    elif refresh:
        con.execute(f"CREATE TEMP TABLE {quoted_snapshot} AS {ranked_cte} {state_select}")
    if refresh:
        with _READ_SNAPSHOT_CACHE_LOCK:
            _READ_SNAPSHOT_CACHE.setdefault(conn_id, {})[table_name] = time.monotonic()
    con.execute(f"CREATE OR REPLACE TEMP VIEW {quoted_output} AS SELECT * FROM {quoted_snapshot}")

def create_current_views(con, table_names: list[str] | None = None) -> list[str]:
    """Create typed temporary views for selected bootstrapped tables in this session.

    For normal API requests, only views referenced by the SQL are created. For migration
    verification, omitting `table_names` selects every table actually bootstrapped in the
    registry (older databases may not have every optional table yet).
    """
    requested = None if table_names is None else set(table_names)
    if requested is not None:
        unknown = requested - set(TABLE_KEYS)
        if unknown:
            raise LakehouseNotReady(f"Unsupported Iceberg view(s) requested: {sorted(unknown)}")
    registry_rows = _registry_rows_cached()
    registered = {row['table_name'] for row in registry_rows}
    if requested is None:
        requested = set(registered)
    missing = requested - registered
    if missing:
        raise LakehouseNotReady(f"Iceberg table(s) not bootstrapped: {sorted(missing)}")
    selected = set(requested)
    # Merge map is an optional dependency for preserving canonical IDs across older links.
    # If catalog reconciliation has not been deployed, relations can still be read as-is.
    if selected.intersection({
        'article_topics', 'article_entities', 'fact_topic_daily',
        'fact_topic_trending', 'fact_entity_daily'
    }) and 'catalog_reconciliation_merges' in registered:
        selected.add('catalog_reconciliation_merges')
    rows = [row for row in registry_rows if row['table_name'] in selected]
    rows.sort(key=lambda item: (item['table_name'] == 'catalog_reconciliation_merges', item['table_name']))
    created = []
    columns_by_table: dict[str, list[dict]] = {}
    for row in rows:
        table_name = row["table_name"]
        try:
            columns = row["columns_json"]
            if isinstance(columns, str):
                columns = json.loads(columns)
            columns_by_table[table_name] = columns
            _create_current_state_view(con, table_name, columns, view_name=table_name)
            created.append(table_name)
        except Exception as exc:
            raise LakehouseNotReady(f"Could not construct current-state view for {table_name}: {exc}") from exc
    if "catalog_reconciliation_merges" in created:
        for table_name, fk, object_type in (
            ("article_topics", "topic_id", "topic"),
            ("article_entities", "entity_id", "entity"),
        ):
            if table_name not in created:
                continue
            view = f'"{table_name}_raw"'
            # Build the raw bridge view from its Iceberg event table, not from the
            # replaceable canonical view. This avoids DuckDB binding a recursive edge.
            _create_current_state_view(con, table_name, columns_by_table[table_name],
                                       view_name=f"{table_name}_raw")
            if table_name == "article_topics":
                con.execute(f"""CREATE OR REPLACE TEMP VIEW "{table_name}" AS
                    WITH RECURSIVE map(source_id,target_id,depth) AS (
                      SELECT source_id,target_id,1 FROM catalog_reconciliation_merges WHERE object_type='topic'
                      UNION ALL
                      SELECT m.source_id,n.target_id,m.depth+1 FROM map m
                      JOIN catalog_reconciliation_merges n ON n.object_type='topic' AND n.source_id=m.target_id
                      WHERE m.depth<20
                    ), resolved AS (SELECT source_id,arg_max(target_id,depth) AS target_id FROM map GROUP BY source_id),
                    canonical AS (
                      SELECT r.article_id,coalesce(x.target_id,r.topic_id) AS topic_id,r.confidence,r.assignment_method,r.created_at
                      FROM {view} r LEFT JOIN resolved x ON x.source_id=r.topic_id
                    )
                    SELECT article_id,topic_id,max(confidence) AS confidence,
                           arg_max(assignment_method,created_at) AS assignment_method,min(created_at) AS created_at
                    FROM canonical GROUP BY article_id,topic_id""")
            else:
                con.execute(f"""CREATE OR REPLACE TEMP VIEW "{table_name}" AS
                    WITH RECURSIVE map(source_id,target_id,depth) AS (
                      SELECT source_id,target_id,1 FROM catalog_reconciliation_merges WHERE object_type='entity'
                      UNION ALL
                      SELECT m.source_id,n.target_id,m.depth+1 FROM map m
                      JOIN catalog_reconciliation_merges n ON n.object_type='entity' AND n.source_id=m.target_id
                      WHERE m.depth<20
                    ), resolved AS (SELECT source_id,arg_max(target_id,depth) AS target_id FROM map GROUP BY source_id),
                    canonical AS (
                      SELECT r.article_id,coalesce(x.target_id,r.entity_id) AS entity_id,r.mention_text,r.confidence,r.created_at
                      FROM {view} r LEFT JOIN resolved x ON x.source_id=r.entity_id
                    )
                    SELECT article_id,entity_id,mention_text,max(confidence) AS confidence,min(created_at) AS created_at
                    FROM canonical GROUP BY article_id,entity_id,mention_text""")

        # Historical Gold fact rows may still refer to a source ID that was merged after
        # the fact was produced. Keep those facts queryable under the canonical ID and
        # aggregate collisions after ID resolution. Raw views are used for exact bootstrap
        # verification; user-facing fact queries read these canonical views.
        if "fact_topic_daily" in created:
            _create_current_state_view(con, "fact_topic_daily", columns_by_table["fact_topic_daily"],
                                       view_name="fact_topic_daily_raw")
            con.execute("""CREATE OR REPLACE TEMP VIEW "fact_topic_daily" AS
                WITH RECURSIVE map(source_id,target_id,depth) AS (
                  SELECT source_id,target_id,1 FROM catalog_reconciliation_merges WHERE object_type='topic'
                  UNION ALL SELECT m.source_id,n.target_id,m.depth+1 FROM map m
                    JOIN catalog_reconciliation_merges n ON n.object_type='topic' AND n.source_id=m.target_id WHERE m.depth<20
                ), resolved AS (SELECT source_id,arg_max(target_id,depth) AS target_id FROM map GROUP BY source_id),
                canonical AS (SELECT f.stat_date,coalesce(x.target_id,f.topic_id) AS topic_id,f.source_id,f.article_count,f.updated_at
                              FROM "fact_topic_daily_raw" f LEFT JOIN resolved x ON x.source_id=f.topic_id)
                SELECT stat_date,topic_id,source_id,sum(article_count)::INTEGER AS article_count,max(updated_at) AS updated_at
                FROM canonical GROUP BY stat_date,topic_id,source_id""")
        if "fact_entity_daily" in created:
            _create_current_state_view(con, "fact_entity_daily", columns_by_table["fact_entity_daily"],
                                       view_name="fact_entity_daily_raw")
            con.execute("""CREATE OR REPLACE TEMP VIEW "fact_entity_daily" AS
                WITH RECURSIVE map(source_id,target_id,depth) AS (
                  SELECT source_id,target_id,1 FROM catalog_reconciliation_merges WHERE object_type='entity'
                  UNION ALL SELECT m.source_id,n.target_id,m.depth+1 FROM map m
                    JOIN catalog_reconciliation_merges n ON n.object_type='entity' AND n.source_id=m.target_id WHERE m.depth<20
                ), resolved AS (SELECT source_id,arg_max(target_id,depth) AS target_id FROM map GROUP BY source_id),
                canonical AS (SELECT f.stat_date,coalesce(x.target_id,f.entity_id) AS entity_id,f.source_id,f.article_count,f.updated_at
                              FROM "fact_entity_daily_raw" f LEFT JOIN resolved x ON x.source_id=f.entity_id)
                SELECT stat_date,entity_id,source_id,sum(article_count)::INTEGER AS article_count,max(updated_at) AS updated_at
                FROM canonical GROUP BY stat_date,entity_id,source_id""")
        if "fact_topic_trending" in created:
            _create_current_state_view(con, "fact_topic_trending", columns_by_table["fact_topic_trending"],
                                       view_name="fact_topic_trending_raw")
            con.execute("""CREATE OR REPLACE TEMP VIEW "fact_topic_trending" AS
                WITH RECURSIVE map(source_id,target_id,depth) AS (
                  SELECT source_id,target_id,1 FROM catalog_reconciliation_merges WHERE object_type='topic'
                  UNION ALL SELECT m.source_id,n.target_id,m.depth+1 FROM map m
                    JOIN catalog_reconciliation_merges n ON n.object_type='topic' AND n.source_id=m.target_id WHERE m.depth<20
                ), resolved AS (SELECT source_id,arg_max(target_id,depth) AS target_id FROM map GROUP BY source_id),
                canonical AS (
                  SELECT f.period_type,f.period_start,coalesce(x.target_id,f.topic_id) AS topic_id,
                         f.article_count,f.source_count,f.previous_article_count,f.growth_pct,f.hotness_score,f.updated_at
                  FROM "fact_topic_trending_raw" f LEFT JOIN resolved x ON x.source_id=f.topic_id
                ), aggregate_rows AS (
                  SELECT period_type,period_start,topic_id,sum(article_count)::INTEGER AS article_count,
                         sum(source_count)::INTEGER AS source_count,sum(previous_article_count)::INTEGER AS previous_article_count,
                         CASE WHEN sum(previous_article_count)>0
                              THEN round(100.0*(sum(article_count)-sum(previous_article_count))/sum(previous_article_count),2)
                              ELSE NULL END AS growth_pct,
                         sum(hotness_score)::DECIMAL(12,3) AS hotness_score,max(updated_at) AS updated_at
                  FROM canonical GROUP BY period_type,period_start,topic_id
                )
                SELECT period_type,period_start,topic_id,article_count,source_count,previous_article_count,growth_pct,hotness_score,
                       row_number() OVER (PARTITION BY period_type,period_start ORDER BY hotness_score DESC,article_count DESC,source_count DESC,topic_id)::INTEGER AS rank_position,
                       updated_at
                FROM aggregate_rows""")
    return created


def _tables_referenced_by_sql(sql: str) -> list[str]:
    """Extract physical table names from internal SQL and keep only our allow-list."""
    found = []
    pattern = re.compile(r'\b(?:FROM|JOIN)\s+["`]?([a-z_][a-z0-9_]*)', re.IGNORECASE)
    for match in pattern.finditer(sql):
        name = match.group(1).lower()
        # Maintenance queries may explicitly request the unreconciled/raw current-state
        # view for key-level comparison. Its physical event table is still the base name.
        base_name = name[:-4] if name.endswith('_raw') else name
        if base_name in TABLE_KEYS and base_name not in found:
            found.append(base_name)
    return found or list(TABLE_KEYS)


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    """Run a read query against latest Iceberg state using a bounded warm connection pool."""
    if not lakehouse_reads_enabled():
        raise LakehouseNotReady("Lakehouse reads are disabled")
    started = time.monotonic()
    pool_wait_started = time.monotonic()
    table_names = _tables_referenced_by_sql(sql)
    with _pooled_read_catalog_connection() as con:
        pool_wait_ms = int((time.monotonic() - pool_wait_started) * 1000)
        create_current_views(con, table_names)
        cursor = con.execute(sql, list(params))
        columns = [d[0] for d in cursor.description or []]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if elapsed_ms >= 1000:
        log.warning(
            "Slow Iceberg read: duration_ms=%s pool_wait_ms=%s snapshot_ttl_s=%s pool_size=%s tables=%s rows=%s",
            elapsed_ms, pool_wait_ms, _read_snapshot_ttl_seconds(), _read_pool_size(), ",".join(table_names), len(rows)
        )
    return rows


def scalar(sql: str, params: tuple | list = (), default=None):
    rows = query(sql, params)
    if not rows:
        return default
    return next(iter(rows[0].values()), default)


def direct_query(sql: str, params: tuple | list = ()) -> list[dict]:
    """Query current Iceberg state from the writer runtime, without relying on the API read flag.

    This is for GitHub Actions ingestion/admin jobs that hold the Lakehouse writer token.
    Render public reads should continue to use ``query`` and the dedicated read-only token.
    """
    if not configured() or not lakehouse_enabled():
        raise LakehouseNotReady("Direct Iceberg access requires CLOUDFLARE_LAKEHOUSE_ENABLED and writer credentials")
    with catalog_connection(read_only=False) as con:
        requested = _tables_referenced_by_sql(sql)
        create_current_views(con, requested)
        cursor = con.execute(sql, list(params))
        columns = [d[0] for d in cursor.description or []]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def direct_upsert_records(table_name: str, rows: list[dict], *, batch_size: int = 250) -> int:
    """Write current-state records directly to Iceberg event tables, without storing rows in PG.

    The PostgreSQL outbox sequence is used solely as a globally monotonic event-ID allocator;
    no clean record or outbox payload is written to PostgreSQL. Sharing its sequence means any
    later operational CDC event will sort after these direct writes, while event IDs remain unique.
    """
    if not rows:
        return 0
    if table_name not in TABLE_KEYS:
        raise ValueError(f"Unsupported Lakehouse table: {table_name}")
    if not configured() or not lakehouse_enabled():
        raise LakehouseNotReady("Direct Iceberg writes require CLOUDFLARE_LAKEHOUSE_ENABLED and writer credentials")
    with get_conn() as pg, pg.cursor() as cur:
        cur.execute("SELECT bootstrap_status FROM lakehouse_table_registry WHERE table_name=%s", (table_name,))
        registry = cur.fetchone()
        if not registry or registry["bootstrap_status"] != "complete":
            raise LakehouseNotReady(f"Iceberg table {table_name} is not bootstrapped; run forward-only bootstrap first")
        cur.execute("SELECT nextval('lakehouse_outbox_outbox_id_seq'::regclass) AS event_id FROM generate_series(1,%s)", (len(rows),))
        event_ids = [int(item["event_id"]) for item in cur.fetchall()]
    now_iso = datetime.now(timezone.utc).isoformat()
    event_rows = []
    for event_id, row in zip(event_ids, rows):
        if not isinstance(row, dict):
            raise TypeError(f"Direct Lakehouse row for {table_name} must be a dict")
        event_rows.append((event_id, _record_key(table_name, row, TABLE_KEYS[table_name]),
                           "UPSERT", _row_json(row), now_iso))
    target = _event_table(table_name)
    with catalog_connection(read_only=False) as con:
        _ensure_event_table(con, table_name)
        width = max(1, min(int(batch_size), 250))
        for offset in range(0, len(event_rows), width):
            batch = event_rows[offset:offset + width]
            # Catalog gateways can return transient 502/503/504 errors during metadata
            # commits. Retry the exact same stable event IDs; duplicate retries are
            # idempotent at the current-state view because they carry identical row data.
            attempt = 0
            while True:
                try:
                    con.execute("BEGIN TRANSACTION")
                    _insert_event_rows_bulk(con, target, batch, statement_batch_size=width)
                    con.execute("COMMIT")
                    break
                except Exception as exc:
                    _rollback_if_active(con)
                    message = str(exc).lower()
                    transient = any(token in message for token in (
                        "502", "503", "504", "bad gateway", "gateway timeout",
                        "temporarily unavailable", "connection reset", "timed out", "timeout"
                    ))
                    if not transient or attempt >= 3:
                        raise
                    delay = min(2 ** attempt, 8)
                    log.warning("Transient Iceberg write failure table=%s batch_start=%s attempt=%s; retrying in %ss: %s",
                                table_name, offset, attempt + 1, delay, str(exc)[:220])
                    time.sleep(delay)
                    attempt += 1
    return len(rows)


def direct_delete_records(table_name: str, key_rows: list[dict]) -> int:
    """Append tombstones directly to Iceberg. This never deletes PostgreSQL source rows."""
    if not key_rows:
        return 0
    if table_name not in TABLE_KEYS:
        raise ValueError(f"Unsupported Lakehouse table: {table_name}")
    with get_conn() as pg, pg.cursor() as cur:
        cur.execute("SELECT nextval('lakehouse_outbox_outbox_id_seq'::regclass) AS event_id FROM generate_series(1,%s)", (len(key_rows),))
        event_ids = [int(item["event_id"]) for item in cur.fetchall()]
    target = _event_table(table_name)
    events = [(event_id, _record_key(table_name, row, TABLE_KEYS[table_name]), "DELETE", "{}",
               datetime.now(timezone.utc).isoformat()) for event_id, row in zip(event_ids, key_rows)]
    with catalog_connection(read_only=False) as con:
        _ensure_event_table(con, table_name)
        _insert_event_rows_bulk(con, target, events)
    return len(events)


def _set_state(key: str, value: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO lakehouse_state(state_key,state_value,updated_at)
                       VALUES(%s,%s::jsonb,now()) ON CONFLICT(state_key) DO UPDATE
                       SET state_value=excluded.state_value,updated_at=now()""",
                    (key, json.dumps(value, ensure_ascii=False, default=_json_default)))


def _get_state(key: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT state_value FROM lakehouse_state WHERE state_key=%s", (key,))
        row = cur.fetchone()
    if not row:
        return None
    value = row["state_value"]
    return json.loads(value) if isinstance(value, str) else value


def _increment_archive_totals_in_transaction(cur, deltas: dict[str, int]) -> None:
    """Track rows intentionally retained in Iceberg but physically purged from PostgreSQL.

    The cumulative offsets let later verification distinguish intentional archival from
    an actual missing-data mismatch. The state update commits atomically with each PG purge.
    """
    cur.execute("SELECT state_value FROM lakehouse_state WHERE state_key='archive_totals' FOR UPDATE")
    row = cur.fetchone()
    value = row['state_value'] if row else {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    totals = {str(k): int(v) for k, v in (value or {}).items()}
    for table_name, delta in deltas.items():
        totals[table_name] = max(0, totals.get(table_name, 0) + int(delta))
    cur.execute("""INSERT INTO lakehouse_state(state_key,state_value,updated_at)
                   VALUES('archive_totals',%s::jsonb,now())
                   ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=now()""",
                (json.dumps(totals, ensure_ascii=False),))


def verify() -> dict:
    """Verify Iceberg parity for a full baseline, or validate forward-only cutover.

    A forward-only bootstrap intentionally imports no historical rows at all. It verifies that
    all required event tables/views exist, drains post-watermark CDC, and does not claim
    historical row-count parity. Archival remains prohibited in forward-only mode.
    """
    sync_result = {"status": "synced", "events_synced": 0, "batches": 0}
    while True:
        batch = sync_pending(50000)
        sync_result["events_synced"] += int(batch.get("events_synced", 0))
        sync_result["batches"] += 1
        if int(batch.get("events_synced", 0)) < 50000:
            break
    results: dict[str, dict] = {}
    archived_offsets = _get_state("archive_totals") or {}
    snapshot = _get_state("bootstrap_snapshot") or {}
    mode = str(snapshot.get("mode") or "full")
    skipped_tables = set(snapshot.get("skipped_historical_tables") or []) if mode == "forward-only" else set()
    created: list[str] = []
    with catalog_connection(read_only=True) as con:
        created = create_current_views(con)
        for table_name in created:
            # In forward-only mode, avoid rescanning large PostgreSQL history tables just to
            # prove a mismatch we deliberately accepted. Future CDC populates these tables.
            if table_name in skipped_tables:
                iceberg_count = int(con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0])
                results[table_name] = {
                    "postgres": None,
                    "intentional_archive_offset": 0,
                    "expected_iceberg": None,
                    "iceberg": iceberg_count,
                    "match": None,
                    "verification": "historical_baseline_skipped",
                    "note": "No pre-watermark source rows were imported; future CDC is enabled and required dimensions hydrate on demand.",
                }
                continue
            with get_conn() as pg, pg.cursor() as cur:
                cur.execute(f'SELECT count(*)::bigint AS n FROM "{table_name}"')
                postgres_count = int(cur.fetchone()["n"])
            # Current relationship views map merged IDs to canonical targets. Count their
            # raw versions for parity so normalization does not change cardinality.
            count_view = f'"{table_name}_raw"' if table_name in {
                "article_topics", "article_entities", "fact_topic_daily", "fact_topic_trending", "fact_entity_daily"
            } else f'"{table_name}"'
            try:
                iceberg_count = int(con.execute(f'SELECT count(*) FROM {count_view}').fetchone()[0])
            except Exception:
                iceberg_count = int(con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0])
            intentional_archive_offset = int(archived_offsets.get(table_name, 0) or 0)
            expected_iceberg_count = postgres_count + intentional_archive_offset
            results[table_name] = {"postgres": postgres_count,
                                   "intentional_archive_offset": intentional_archive_offset,
                                   "expected_iceberg": expected_iceberg_count,
                                   "iceberg": iceberg_count,
                                   "match": expected_iceberg_count == iceberg_count,
                                   "verification": "row_count_parity"}
    required_tables = {
        "data_sources", "news_articles", "topics", "entities", "article_topics", "article_entities",
        "fact_news_article", "fact_topic_daily", "fact_topic_trending",
        "fact_entity_daily", "fact_source_daily",
    }
    missing_required = sorted(required_tables - set(created))
    parity_results = [item for name, item in results.items() if name not in skipped_tables]
    exact = not missing_required and all(item["match"] is True for item in parity_results)
    if exact and mode == "forward-only":
        status_value = "verified_forward_only"
    else:
        status_value = "verified" if exact else "mismatch"
    payload = {"status": status_value, "verification_mode": mode,
               "historical_tables_skipped": sorted(skipped_tables),
               "tables": results, "required_tables_missing": missing_required,
               "tables_checked": len(results), "tables_checked_for_parity": len(parity_results),
               "sync": sync_result}
    if exact and mode == "full":
        payload["verified_at"] = datetime.now(timezone.utc).isoformat()
        _set_state("full_snapshot_verified", payload)
    elif exact and mode == "forward-only":
        payload["verified_at"] = datetime.now(timezone.utc).isoformat()
        # Keep the full-verification marker explicitly non-verified so archive-apply cannot
        # mistake a forward-only cutover for a complete historical Iceberg copy.
        _set_state("full_snapshot_verified", {"status": "forward_only", "tables": results,
                                                "verified_at": payload["verified_at"]})
        _set_state("forward_only_verified", payload)
    else:
        _set_state("full_snapshot_verified", {"status": "mismatch", "tables": results})
    return payload

def archive_old_data(cutoff_days: int = 90, apply: bool = False, batch_size: int = 500) -> dict:
    """Purge aged Article/FACT rows from PostgreSQL only after exact Iceberg verification.

    Articles stay queryable from Iceberg after purge; user timelines, search and followed
    feeds switch to Iceberg when CLOUDFLARE_LAKEHOUSE_READS=true. Topics/Entities remain as
    the small serving dimensions because user follows, identity checks and operational
    reconciliation need stable, transactionally-updated IDs; complete copies also exist in
    Iceberg. Bronze/raw feed payloads are not migrated.
    """
    if not lakehouse_reads_enabled():
        raise LakehouseNotReady("Refusing to archive: enable CLOUDFLARE_LAKEHOUSE_READS first")
    verification = _get_state("full_snapshot_verified")
    if not verification or verification.get("status") != "verified":
        raise LakehouseNotReady("Refusing to archive: run `verify` and resolve all row-count mismatches first")
    cutoff_days = max(90, min(int(cutoff_days), 3650))
    batch_size = max(100, min(int(batch_size), 2000))
    from datetime import timedelta
    cutoff_date = date.today() - timedelta(days=cutoff_days)
    # Capture all outstanding writes (including changes made by admin/API) before purging.
    sync_result = {"events_synced": 0, "batches": 0}
    while True:
        batch_sync = sync_pending(50000)
        sync_result["events_synced"] += int(batch_sync.get("events_synced", 0))
        sync_result["batches"] += 1
        if int(batch_sync.get("events_synced", 0)) < 50000:
            break
    left = pending_count()
    if left:
        raise LakehouseNotReady(f"Refusing to archive: {left} CDC events remain pending")
    # The saved verification marker may predate new pipeline/admin writes. Re-run the exact
    # current-state checks immediately before the first destructive batch.
    fresh_verification = verify()
    if fresh_verification.get("status") != "verified":
        raise LakehouseNotReady(
            "Refusing to archive: fresh Iceberg/PostgreSQL verification did not pass; "
            f"see table mismatches {fresh_verification.get('tables', {})}"
        )

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*)::bigint AS n FROM news_articles WHERE coalesce(published_at,collected_at) < %s::date", (cutoff_date,))
        selected_articles = int(cur.fetchone()["n"])
    report = {"cutoff_date": cutoff_date.isoformat(), "articles_selected": selected_articles,
              "articles_archived": 0, "facts_pruned": {}, "apply": apply, "sync": sync_result}
    if not apply:
        return report

    deleted = 0
    while True:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT article_id FROM news_articles
                           WHERE coalesce(published_at,collected_at) < %s::date
                           ORDER BY article_id LIMIT %s""", (cutoff_date, batch_size))
            batch = [int(row["article_id"]) for row in cur.fetchall()]
        if not batch:
            break
        # Never cascade-delete a parent unless its row AND dependent relationships/fact row
        # match the PostgreSQL snapshot in Iceberg. A global row-count check alone cannot
        # detect a key-level mismatch hidden by another duplicated/missing record.
        placeholders = ",".join("?" for _ in batch)
        lake_rows = query(f"""SELECT a.article_id,a.source_id,a.canonical_url,a.url,a.title,a.content_hash,
                   a.summary,a.field_category,a.image_url,a.is_excluded,a.published_at,a.collected_at,
                   (SELECT count(*)::BIGINT FROM article_topics_raw rel WHERE rel.article_id=a.article_id) AS topic_links,
                   (SELECT count(*)::BIGINT FROM article_entities_raw rel WHERE rel.article_id=a.article_id) AS entity_links,
                   (SELECT count(*)::BIGINT FROM fact_news_article f WHERE f.article_id=a.article_id) AS article_facts
                   FROM news_articles a WHERE a.article_id IN ({placeholders})""", batch)
        lake_by_id = {int(row["article_id"]): row for row in lake_rows}
        missing = sorted(set(batch) - set(lake_by_id))
        if missing:
            raise LakehouseNotReady(f"Refusing to delete article ids not present in Iceberg: {missing[:10]}")
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT a.article_id,a.source_id,a.canonical_url,a.url,a.title,a.content_hash,
                              a.summary,a.field_category,a.image_url,a.is_excluded,a.published_at,a.collected_at,
                              (SELECT count(*)::bigint FROM article_topics_raw rel WHERE rel.article_id=a.article_id) AS topic_links,
                              (SELECT count(*)::bigint FROM article_entities_raw rel WHERE rel.article_id=a.article_id) AS entity_links,
                              (SELECT count(*)::bigint FROM fact_news_article f WHERE f.article_id=a.article_id) AS article_facts
                           FROM news_articles a WHERE a.article_id=ANY(%s::bigint[])""", (batch,))
            pg_by_id = {int(row["article_id"]): row for row in cur.fetchall()}
            mismatches = []
            row_fields = ("source_id", "canonical_url", "url", "title", "content_hash", "summary",
                          "field_category", "image_url", "is_excluded", "published_at", "collected_at")
            count_fields = ("topic_links", "entity_links", "article_facts")
            def comparable(value):
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    return value.astimezone(timezone.utc).isoformat()
                if isinstance(value, date):
                    return value.isoformat()
                return value
            for article_id in batch:
                expected = pg_by_id.get(article_id)
                actual = lake_by_id.get(article_id)
                if expected is None or actual is None:
                    mismatches.append(article_id)
                    continue
                content_mismatch = any(comparable(expected[field]) != comparable(actual[field]) for field in row_fields)
                count_mismatch = any(int(expected[field] or 0) != int(actual[field] or 0) for field in count_fields)
                if content_mismatch or count_mismatch:
                    mismatches.append(article_id)
            if mismatches:
                raise LakehouseNotReady(
                    f"Refusing to archive: article relationship/FACT mismatch for IDs {mismatches[:10]}"
                )
            topic_links = sum(int(row["topic_links"] or 0) for row in pg_by_id.values())
            entity_links = sum(int(row["entity_links"] or 0) for row in pg_by_id.values())
            article_facts = sum(int(row["article_facts"] or 0) for row in pg_by_id.values())
            cur.execute("SET LOCAL doraemon.skip_lakehouse_outbox = 'on'")
            cur.execute("DELETE FROM news_articles WHERE article_id=ANY(%s::bigint[])", (batch,))
            batch_deleted = int(cur.rowcount)
            _increment_archive_totals_in_transaction(cur, {
                "news_articles": batch_deleted,
                "article_topics": topic_links,
                "article_entities": entity_links,
                "fact_news_article": article_facts,
            })
            deleted += batch_deleted
    report["articles_archived"] = deleted

    # Keep current analytical windows in PostgreSQL for low-latency dashboards; the full
    # FACT history has already been preserved in Iceberg. Suppress CDC for this intentional
    # hot-window purge, otherwise the outbox would mark historical FACT rows as deleted.
    retentions = {"fact_topic_daily": 90, "fact_topic_trending": 90,
                  "fact_entity_daily": 90, "fact_source_daily": 90}
    for table_name, days in retentions.items():
        date_col = "period_start" if table_name == "fact_topic_trending" else "stat_date"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL doraemon.skip_lakehouse_outbox = 'on'")
            cur.execute(f"DELETE FROM {table_name} WHERE {date_col} < current_date - %s", (days,))
            pruned_count = int(cur.rowcount)
            report["facts_pruned"][table_name] = pruned_count
            if pruned_count:
                _increment_archive_totals_in_transaction(cur, {table_name: pruned_count})
    _set_state("last_archive", {**report, "archived_at": datetime.now(timezone.utc).isoformat()})
    log.warning("Cloudflare Lakehouse archive complete: %s", report)
    return report


def compact_event_tables(limit_tables: int | None = None) -> dict:
    """Collapse CDC event logs to one latest version per key without changing current state.

    The underlying Iceberg tables are unpartitioned, which is important because DuckDB's
    current Iceberg integration does not support DELETE on partitioned tables. Run this
    maintenance command only when the CDC sync workflow is not running.
    """
    if not configured() or not lakehouse_enabled():
        raise LakehouseNotReady("Enable/configure Cloudflare Lakehouse before compaction")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT table_name FROM lakehouse_table_registry WHERE bootstrap_status='complete' ORDER BY table_name")
        tables = [str(r["table_name"]) for r in cur.fetchall()]
    if limit_tables is not None:
        tables = tables[:max(1, int(limit_tables))]
    results = {}
    with catalog_connection() as con:
        for table_name in tables:
            target = _event_table(table_name)
            before = int(con.execute(f"SELECT count(*) FROM {target}").fetchone()[0])
            temp = f"compact_{table_name}"
            con.execute(f"CREATE OR REPLACE TEMP TABLE {temp} AS SELECT event_id,record_key,operation,row_data,event_at FROM {target} QUALIFY row_number() OVER (PARTITION BY record_key ORDER BY event_id DESC)=1")
            after = int(con.execute(f"SELECT count(*) FROM {temp}").fetchone()[0])
            if after >= before:
                con.execute(f"DROP TABLE {temp}")
                results[table_name] = {"before": before, "after": after, "removed_versions": 0}
                continue
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute(f"DELETE FROM {target}")
                con.execute(f"INSERT INTO {target} SELECT event_id,record_key,operation,row_data,event_at FROM {temp}")
                con.execute("COMMIT")
            except Exception:
                _rollback_if_active(con)
                raise
            con.execute(f"DROP TABLE {temp}")
            results[table_name] = {"before": before, "after": after, "removed_versions": before-after}
    result = {"status": "compacted", "tables": results,
              "versions_removed": sum(x["removed_versions"] for x in results.values())}
    _set_state("last_compaction", {**result, "compacted_at": datetime.now(timezone.utc).isoformat()})
    return result

def status() -> dict:
    state = _get_state("full_snapshot_verified")
    forward_only_state = _get_state("forward_only_verified")
    archive = _get_state("last_archive")
    return {"enabled": lakehouse_enabled(), "reads_enabled": lakehouse_reads_enabled(),
            "read_configured": read_configured(), "write_configured": configured(),
            "configured": configured() or read_configured(),
            "pending_outbox": pending_count() if configured() else None,
            "bootstrap_snapshot": _get_state("bootstrap_snapshot"),
            "verification": state, "forward_only_verification": forward_only_state,
            "last_archive": archive}
