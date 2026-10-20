"""Operator CLI for the Cloudflare R2 + Apache Iceberg cutover.

Examples:
  python -m app.lakehouse_migration bootstrap
  python -m app.lakehouse_migration sync --limit 10000
  python -m app.lakehouse_migration verify
  python -m app.lakehouse_migration archive --cutoff-days 90          # dry-run
  python -m app.lakehouse_migration archive --cutoff-days 90 --apply # destructive after verify
  python -m app.lakehouse_migration status
  python -m app.lakehouse_migration compact
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from . import cloudflare_lakehouse as lakehouse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Doraemon Cloudflare Iceberg Lakehouse migration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Copy current clean core tables and FACTs into Iceberg")
    sync = sub.add_parser("sync", help="Append pending transactional outbox events to Iceberg")
    sync.add_argument("--limit", type=int, default=10000)
    sub.add_parser("verify", help="Compare PostgreSQL and Iceberg row counts before archiving")
    sub.add_parser("status", help="Show Lakehouse configuration and migration status")
    compact = sub.add_parser("compact", help="Remove superseded CDC row versions in Iceberg")
    compact.add_argument("--limit-tables", type=int, default=None)
    archive = sub.add_parser("archive", help="Purge old Article/FACT rows from PostgreSQL after verification")
    archive.add_argument("--cutoff-days", type=int, default=90)
    archive.add_argument("--batch-size", type=int, default=500)
    archive.add_argument("--apply", action="store_true", help="Actually delete after checks; default is dry-run")
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = lakehouse.bootstrap()
        elif args.command == "sync":
            result = lakehouse.sync_pending(args.limit)
        elif args.command == "verify":
            result = lakehouse.verify()
        elif args.command == "status":
            result = lakehouse.status()
        elif args.command == "compact":
            result = lakehouse.compact_event_tables(args.limit_tables)
        else:
            result = lakehouse.archive_old_data(args.cutoff_days, args.apply, args.batch_size)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("status") in {"mismatch", "failed"}:
            return 2
        return 0
    except Exception as exc:
        logging.exception("Cloudflare Lakehouse command failed")
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
