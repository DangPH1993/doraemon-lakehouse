"""Private Cloudflare R2 object storage for payment QR and proof images.

The bucket should remain private. Images are streamed through authenticated or
controlled FastAPI endpoints, so proof object keys are never exposed to users.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Optional

from .config import settings

log = logging.getLogger("mini-lakehouse.r2")


class R2NotConfigured(RuntimeError):
    """Required Cloudflare R2 credentials or bucket name are missing."""


def is_configured() -> bool:
    return all((
        settings.cloudflare_r2_account_id,
        settings.cloudflare_r2_access_key_id,
        settings.cloudflare_r2_secret_access_key,
        settings.cloudflare_r2_bucket_name,
    ))


def ensure_configured() -> None:
    if not is_configured():
        raise R2NotConfigured(
            "Cloudflare R2 chưa được cấu hình. Hãy thiết lập CLOUDFLARE_R2_ACCOUNT_ID, "
            "CLOUDFLARE_R2_ACCESS_KEY_ID, CLOUDFLARE_R2_SECRET_ACCESS_KEY và "
            "CLOUDFLARE_R2_BUCKET_NAME trong Environment của Render."
        )


@lru_cache(maxsize=1)
def _client():
    ensure_configured()
    # Import lazily so non-image routes can still import the application if the
    # package has not yet been installed in a local development environment.
    import boto3
    from botocore.config import Config

    endpoint = f"https://{settings.cloudflare_r2_account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.cloudflare_r2_access_key_id,
        aws_secret_access_key=settings.cloudflare_r2_secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def make_object_key(category: str, filename: str, scope: str = "") -> str:
    """Build an R2 key under the configured prefix, without trusting user paths."""
    prefix = (settings.cloudflare_r2_key_prefix or "news-radar").strip(" /")
    safe_category = re.sub(r"[^A-Za-z0-9_-]", "", category) or "images"
    safe_scope = re.sub(r"[^A-Za-z0-9_-]", "", str(scope))
    safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:180]
    parts = [part for part in (prefix, safe_category, safe_scope, safe_filename) if part]
    return "/".join(parts)


def put_bytes(key: str, content: bytes, content_type: str) -> None:
    if not key or not content:
        raise ValueError("R2 key and image content must not be empty")
    _client().put_object(
        Bucket=settings.cloudflare_r2_bucket_name,
        Key=key,
        Body=content,
        ContentLength=len(content),
        ContentType=content_type,
        CacheControl="private, no-store, max-age=0",
        Metadata={"storage-managed-by": "news-radar"},
    )


def get_bytes(key: str, max_bytes: int = 3 * 1024 * 1024 + 1) -> bytes:
    if not key:
        raise ValueError("R2 object key must not be empty")
    response = _client().get_object(Bucket=settings.cloudflare_r2_bucket_name, Key=key)
    body = response["Body"]
    try:
        content = body.read(max_bytes)
    finally:
        body.close()
    if len(content) >= max_bytes:
        raise RuntimeError("R2 image exceeds the permitted size")
    return content


def delete_object(key: Optional[str]) -> None:
    if not key:
        return
    try:
        _client().delete_object(Bucket=settings.cloudflare_r2_bucket_name, Key=key)
    except Exception:
        # Cleanup must never mask the main database result. Log orphan-cleanup
        # failures so they can be reconciled without breaking user workflows.
        log.warning("Unable to delete obsolete R2 object %s", key, exc_info=True)
