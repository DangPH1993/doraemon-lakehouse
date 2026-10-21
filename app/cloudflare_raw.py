"""Temporary RSS raw-payload storage in Cloudflare R2.

Raw RSS entries are uploaded to a dedicated S3-compatible R2 bucket before their clean
rows are committed directly to Iceberg. In direct-write mode the raw payload and its key
are never stored in PostgreSQL. The pipeline deletes the object only after verifying that
the clean article and its base FACT are visible in Iceberg.

This deliberately uses R2 S3 credentials, which are distinct from the Basin Catalog API
Token used by the Iceberg adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any


class CloudflareRawNotReady(RuntimeError):
    """Raised when temporary R2 raw storage is enabled but not fully configured."""


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def raw_storage_enabled() -> bool:
    return _flag("CLOUDFLARE_R2_RAW_ENABLED")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CloudflareRawNotReady(
            f"Cloudflare R2 raw storage is enabled, but {name} is not configured. "
            "Set the R2 S3 access key, secret, account ID and raw bucket."
        )
    return value


def _s3_client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - installed in deployment via requirements.txt
        raise CloudflareRawNotReady(
            "boto3 is required for Cloudflare R2 raw storage; redeploy requirements.txt"
        ) from exc

    account_id = _required_env("CLOUDFLARE_R2_ACCOUNT_ID")
    access_key = _required_env("CLOUDFLARE_R2_S3_ACCESS_KEY_ID")
    secret_key = _required_env("CLOUDFLARE_R2_S3_SECRET_ACCESS_KEY")
    endpoint_url = os.getenv("CLOUDFLARE_R2_S3_ENDPOINT", "").strip()
    if not endpoint_url:
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    )


def _bucket() -> str:
    return _required_env("CLOUDFLARE_R2_RAW_BUCKET")


def object_key(source_id: int, entry_key: str) -> str:
    """Return a deterministic key so a retry overwrites rather than duplicates the raw item."""
    digest = hashlib.sha256(f"{int(source_id)}\0{entry_key}".encode("utf-8", errors="replace")).hexdigest()
    return f"rss-raw/source_id={int(source_id)}/{digest[:2]}/{digest}.json"


def store_raw_entry(source_id: int, entry_key: str, item_url: str, payload: dict[str, Any]) -> str:
    """Write the raw RSS entry to R2 and return its durable object key.

    Upload happens before any clean Iceberg writes. If later processing fails, the raw
    object is deliberately kept for recovery and is overwritten on a retry.
    """
    key = object_key(source_id, entry_key)
    document = {
        "source_id": int(source_id),
        "entry_key": str(entry_key),
        "item_url": str(item_url or ""),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    body = json.dumps(document, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    _s3_client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        Metadata={"source-id": str(int(source_id)), "raw-format": "rss-entry-json-v1"},
    )
    return key


def read_raw_entry(key: str) -> dict[str, Any]:
    """Read a staged entry when it is still present, for safe recovery/backfills."""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError("Invalid Cloudflare R2 raw object key")
    response = _s3_client().get_object(Bucket=_bucket(), Key=key)
    body = response["Body"].read()
    document = json.loads(body.decode("utf-8"))
    payload = document.get("payload") if isinstance(document, dict) else None
    return payload if isinstance(payload, dict) else {}


def delete_raw_object(key: str) -> None:
    """Delete a staged raw entry. R2 DeleteObject is safe to retry for an already-absent key."""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError("Invalid Cloudflare R2 raw object key")
    _s3_client().delete_object(Bucket=_bucket(), Key=key)


def raw_reference_payload(key: str) -> dict[str, str]:
    """Small PostgreSQL reference; the raw payload itself is never copied into PostgreSQL."""
    return {"_raw_storage": "cloudflare-r2", "_r2_object_key": key}


def referenced_object_key(payload: Any) -> str | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict) or payload.get("_raw_storage") != "cloudflare-r2":
        return None
    key = payload.get("_r2_object_key")
    return str(key) if isinstance(key, str) and key else None


def list_raw_entries(limit: int = 100, source_id: int | None = None, query: str | None = None,
                     source_names: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """List the bounded set of transient RSS objects for the existing admin Raw view.

    Raw data is never copied to PostgreSQL in direct mode. This adapter
    reads only a bounded number of small staged objects from R2 and returns the display
    fields expected by the admin table.
    """
    if not raw_storage_enabled():
        return []
    limit = max(1, min(int(limit), 500))
    query_norm = str(query or "").casefold().strip()
    names = {int(k): str(v) for k, v in (source_names or {}).items()}
    client = _s3_client()
    response = client.list_objects_v2(Bucket=_bucket(), Prefix="rss-raw/", MaxKeys=min(1000, max(100, limit * 2)))
    rows: list[dict[str, Any]] = []
    for item in sorted(response.get("Contents") or [], key=lambda x: x.get("LastModified") or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        key = str(item.get("Key") or "")
        if not key.endswith(".json") or not key.startswith("rss-raw/"):
            continue
        try:
            response_obj = client.get_object(Bucket=_bucket(), Key=key)
            document = json.loads(response_obj["Body"].read().decode("utf-8"))
        except Exception:
            # A partially uploaded/deleted item should not make the whole admin list fail.
            continue
        try:
            sid = int(document.get("source_id"))
        except (TypeError, ValueError):
            continue
        if source_id is not None and sid != int(source_id):
            continue
        payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
        title = str(payload.get("title") or "")
        summary = str(payload.get("summary") or payload.get("description") or "")
        url = str(document.get("item_url") or payload.get("link") or "")
        if query_norm and query_norm not in f"{title} {summary} {url}".casefold():
            continue
        raw_id = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF
        published = str(payload.get("published") or payload.get("updated") or payload.get("pubDate") or "")
        captured = document.get("captured_at") or item.get("LastModified")
        rows.append({"raw_id": raw_id or 1, "entry_key": document.get("entry_key") or key,
                     "item_url": url, "fetched_at": captured, "source_name": names.get(sid, f"Source {sid}"),
                     "raw_title": title, "raw_published": published, "raw_summary": summary})
        if len(rows) >= limit:
            break
    return rows
