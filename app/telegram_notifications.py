"""Telegram linking replies and per-user digest notifications.

The sender runs as a stage inside the existing ingestion pipeline; no extra workflow is needed.
"""
from __future__ import annotations

import html
import json
import logging
import time
from urllib.parse import urlsplit

import requests

from .config import settings
from .db import fetch_all, get_conn

log = logging.getLogger("mini-lakehouse.telegram")
TELEGRAM_API_ROOT = "https://api.telegram.org/bot"
MAX_TELEGRAM_TEXT = 3600  # Under Telegram's 4096 character limit after HTML is parsed.


def send_telegram_text(chat_id: str, text: str) -> dict:
    """Send one HTML-formatted Telegram message. Raises on API/network errors."""
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    try:
        response = requests.post(
            f"{TELEGRAM_API_ROOT}{token}/sendMessage",
            json={
                "chat_id": str(chat_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        # Request exception messages can contain the token-bearing API URL. Never surface it.
        raise RuntimeError(f"Telegram network request failed ({type(exc).__name__}).") from None
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code >= 400 or not payload.get("ok"):
        description = str(payload.get("description") or f"HTTP {response.status_code}")[:240]
        raise RuntimeError(f"Telegram sendMessage failed: {description}")
    return payload


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _telegram_units(value: str) -> int:
    # Telegram message lengths are measured in UTF-16 code units.
    return len(value.encode("utf-16-le")) // 2


def _plain(value, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > max_chars:
        return text[: max(1, max_chars - 1)].rstrip() + "…"
    return text


def _article_block(row: dict, number: int) -> str:
    title = _plain(row.get("title"), 150) or "Bài báo mới"
    url = str(row.get("url") or "").strip()
    parts = urlsplit(url)
    safe_url = url if parts.scheme in ("http", "https") and parts.netloc else ""
    escaped_title = html.escape(title, quote=True)
    headline = f'{number}. <a href="{html.escape(safe_url, quote=True)}">{escaped_title}</a>' if safe_url else f"{number}. {escaped_title}"
    source = html.escape(_plain(row.get("source_name"), 55), quote=True)
    lines = [headline]
    if source:
        lines.append(f"   <i>{source}</i>")
    topics = [_plain(x.get("name"), 42) for x in _json_list(row.get("matched_topics")) if isinstance(x, dict) and x.get("name")]
    entities = [_plain(x.get("name"), 42) for x in _json_list(row.get("matched_entities")) if isinstance(x, dict) and x.get("name")]
    labels = []
    if topics:
        labels.append("Sự kiện: " + ", ".join(topics[:2]))
    if entities:
        labels.append("Đối tượng: " + ", ".join(entities[:2]))
    if labels:
        lines.append("   Theo dõi: " + html.escape(" · ".join(labels), quote=True))
    return "\n".join(lines)


def build_digest(rows: list[dict]) -> tuple[str, list[int]]:
    """Build a size-bounded digest, returning the exact article IDs in the message."""
    if not rows:
        return "", []
    max_articles = max(1, min(int(settings.telegram_digest_max_articles), 20))
    selected: list[dict] = []
    blocks: list[str] = []
    for row in rows[:max_articles]:
        block = _article_block(row, len(selected) + 1)
        candidate = "\n\n".join(blocks + [block])
        header = "<b>News Radar · Bản tin mới</b>\nCác bài mới theo topic/entity bạn theo dõi:\n\n"
        if _telegram_units(header + candidate) > MAX_TELEGRAM_TEXT and selected:
            break
        if _telegram_units(header + candidate) > MAX_TELEGRAM_TEXT:
            # A malformed/extremely long record must not prevent the queue from progressing.
            minimal = {
                **row,
                "title": _plain(row.get("title"), 80),
                "source_name": _plain(row.get("source_name"), 30),
                "matched_topics": [],
                "matched_entities": [],
            }
            block = _article_block(minimal, 1)
            if _telegram_units(header + block) > MAX_TELEGRAM_TEXT:
                block = "1. " + html.escape(_plain(row.get("title"), 60), quote=True)
            if _telegram_units(header + block) > MAX_TELEGRAM_TEXT:
                break
        selected.append(row)
        blocks.append(block)
    if not selected:
        return "", []
    header = f"<b>News Radar · {len(selected)} bài mới</b>\nCác bài mới theo topic/entity bạn theo dõi:\n\n"
    text = header + "\n\n".join(blocks)
    return text, [int(row["article_id"]) for row in selected]


_PENDING_SQL = r"""
WITH matches AS (
    SELECT ust.user_id, a.article_id, 'topic'::text AS follow_type,
           t.topic_id::bigint AS follow_id, t.name AS follow_name
    FROM app_users u
    JOIN user_saved_timelines ust ON ust.user_id=u.user_id AND ust.timeline_type='topic'
    JOIN topics t ON t.topic_id=ust.topic_id AND t.status='active' AND t.topic_type='event'
    JOIN article_topics at ON at.topic_id=t.topic_id
    JOIN news_articles a ON a.article_id=at.article_id AND a.is_excluded=false
    WHERE u.telegram_enabled=true AND u.telegram_chat_id IS NOT NULL
      AND u.telegram_notifications_since IS NOT NULL
      AND a.collected_at >= u.telegram_notifications_since
      AND a.collected_at >= now() - interval '90 days'
      AND NOT EXISTS (
          SELECT 1 FROM user_telegram_deliveries d
          WHERE d.user_id=u.user_id AND d.article_id=a.article_id
      )
    UNION ALL
    SELECT ust.user_id, a.article_id, 'entity'::text AS follow_type,
           e.entity_id::bigint AS follow_id, e.canonical_name AS follow_name
    FROM app_users u
    JOIN user_saved_timelines ust ON ust.user_id=u.user_id AND ust.timeline_type='entity'
    JOIN entities e ON e.entity_id=ust.entity_id AND e.status='active'
    JOIN article_entities ae ON ae.entity_id=e.entity_id
    JOIN news_articles a ON a.article_id=ae.article_id AND a.is_excluded=false
    WHERE u.telegram_enabled=true AND u.telegram_chat_id IS NOT NULL
      AND u.telegram_notifications_since IS NOT NULL
      AND a.collected_at >= u.telegram_notifications_since
      AND a.collected_at >= now() - interval '90 days'
      AND NOT EXISTS (
          SELECT 1 FROM user_telegram_deliveries d
          WHERE d.user_id=u.user_id AND d.article_id=a.article_id
      )
), rolled_up AS (
    SELECT user_id, article_id,
           COALESCE(jsonb_agg(DISTINCT jsonb_build_object('id',follow_id,'name',follow_name))
                    FILTER (WHERE follow_type='topic'), '[]'::jsonb) AS matched_topics,
           COALESCE(jsonb_agg(DISTINCT jsonb_build_object('id',follow_id,'name',follow_name))
                    FILTER (WHERE follow_type='entity'), '[]'::jsonb) AS matched_entities
    FROM matches GROUP BY user_id, article_id
), ranked AS (
    SELECT r.user_id, u.telegram_chat_id, a.article_id, a.title, a.url,
           a.published_at, a.collected_at, s.name AS source_name,
           r.matched_topics, r.matched_entities,
           row_number() OVER (
               PARTITION BY r.user_id ORDER BY a.collected_at ASC, a.article_id ASC
           ) AS row_num
    FROM rolled_up r
    JOIN app_users u ON u.user_id=r.user_id
    JOIN news_articles a ON a.article_id=r.article_id
    JOIN data_sources s ON s.source_id=a.source_id
)
SELECT user_id, telegram_chat_id, article_id, title, url, published_at,
       collected_at, source_name, matched_topics, matched_entities, row_num
FROM ranked
WHERE row_num <= %s
ORDER BY user_id, row_num
"""


def send_telegram_digests() -> dict:
    """Send one bounded digest per enabled user and record each successful delivery."""
    if not settings.telegram_bot_token:
        log.info("Telegram digest skipped: TELEGRAM_BOT_TOKEN is not configured in this pipeline run.")
        return {"status": "skipped_missing_token", "users_attempted": 0, "messages_sent": 0, "articles_sent": 0, "failed_users": 0}

    limit = max(1, min(int(settings.telegram_digest_max_articles), 20))
    rows = fetch_all(_PENDING_SQL, (limit,))
    if not rows:
        log.info("Telegram digest: no undelivered article matches for enabled users.")
        return {"status": "no_pending_articles", "users_attempted": 0, "messages_sent": 0, "articles_sent": 0, "failed_users": 0}

    by_user: dict[int, list[dict]] = {}
    for row in rows:
        by_user.setdefault(int(row["user_id"]), []).append(row)

    stats = {"status": "completed", "users_attempted": len(by_user), "messages_sent": 0, "articles_sent": 0, "failed_users": 0}
    for index, (user_id, user_rows) in enumerate(by_user.items()):
        # Stay below Telegram's normal broadcast throughput when many users follow the radar.
        if index:
            time.sleep(0.05)
        chat_id = str(user_rows[0]["telegram_chat_id"])
        message, article_ids = build_digest(user_rows)
        if not article_ids or not message:
            log.warning("Telegram digest for user_id=%s skipped because its message was empty or over the safe length.", user_id)
            continue
        try:
            send_telegram_text(chat_id, message)
            with get_conn() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO user_telegram_deliveries(user_id, article_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    [(user_id, article_id) for article_id in article_ids],
                )
            stats["messages_sent"] += 1
            stats["articles_sent"] += len(article_ids)
            log.info("Telegram digest sent: user_id=%s articles=%s.", user_id, len(article_ids))
        except Exception as exc:
            stats["failed_users"] += 1
            log.exception("Telegram digest failed for user_id=%s: %s", user_id, str(exc)[:240])
    if stats["failed_users"]:
        stats["status"] = "partial"
    return stats


def cleanup_old_delivery_markers() -> int:
    """Bound deduplication storage; articles older than 90 days are never re-queued."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_telegram_deliveries WHERE sent_at < now() - interval '90 days'")
        return max(0, cur.rowcount)
