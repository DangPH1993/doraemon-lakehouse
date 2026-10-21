"""Iceberg-first RSS ingestion path.

Only operational metadata (RSS source definitions, cleaning rules, pipeline runs and the
shared monotonic event-id sequence) uses PostgreSQL. Raw RSS is staged in R2; clean articles,
topics, entities, aliases, relationships and FACT rows are written directly to Iceberg.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import feedparser
import requests
from dateutil import parser as date_parser

from .config import settings
from .db import get_conn
from . import cloudflare_lakehouse as lakehouse
from . import cloudflare_raw as raw_store
from .entity_rules import should_track_entity, is_grounded_entity
from .entity_names import entity_normalized_key
from .entity_resolution import (
    build_entity_lookup, add_entity_to_lookup, entity_name_variants,
    resolve_entity_in_lookup, strip_person_title,
)
from .field_categories import FIELD_CATEGORIES, classify_field_category, normalize_field_category, is_broad_field_topic
from .named_infrastructure import augment_entities_with_named_infrastructure
from .named_people import extract_named_person_entities
from .named_proper_nouns import extract_named_proper_noun_entities
from .processing import (
    apply_cleaning_rules, canonicalize_url, clean_html, extract_entities, hash_text,
    normalize_space, norm_name, slugify, extract_event_signature,
)
from .article_images import extract_image_url, _lookup_open_graph_images, _IMAGE_OG_FALLBACK_LIMIT_PER_SOURCE

log = logging.getLogger("mini-lakehouse.direct-pipeline")
USER_AGENT = "DoraemonMiniLakehouse/1.0 (+RSS feed reader; contact admin of the configured source)"


def _stable_id(namespace: str, value: str) -> int:
    digest = hashlib.sha256(f"{namespace}\0{value}".encode("utf-8", errors="replace")).digest()
    ident = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
    return ident or 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _parse_date(entry: dict) -> datetime | None:
    # Use the existing pipeline parser so source-local naive RSS timestamps remain consistent.
    from .pipeline import parse_date
    return parse_date(entry)


def _extract_content(entry: dict) -> tuple[str, str]:
    from .pipeline import extract_feed_content
    return extract_feed_content(entry)


def _source_headers(source: dict) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if source.get("etag"):
        headers["If-None-Match"] = str(source["etag"])
    if source.get("last_modified"):
        headers["If-Modified-Since"] = str(source["last_modified"])
    return headers


def _fetch_operational_config(source_ids: list[int] | None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM data_sources WHERE enabled=true ORDER BY source_id")
        sources = [dict(x) for x in cur.fetchall()]
        cur.execute("SELECT * FROM cleaning_rules WHERE enabled=true ORDER BY rule_id")
        rules = [dict(x) for x in cur.fetchall()]
    if source_ids is not None:
        allowed = {int(x) for x in source_ids}
        sources = [s for s in sources if int(s["source_id"]) in allowed]
    return sources, rules


def _load_iceberg_catalogs() -> tuple[dict[str, dict], dict[str, dict], dict[int, dict]]:
    rows = lakehouse.direct_query("SELECT * FROM topics WHERE status='active' AND topic_type='event'")
    topics_by_event = {str(r.get("event_key") or ""): r for r in rows if r.get("event_key")}
    entities = lakehouse.direct_query("SELECT * FROM entities WHERE status='active'")
    aliases = lakehouse.direct_query("SELECT * FROM entity_aliases")
    lookup = build_entity_lookup(entities, aliases)
    source_rows = lakehouse.direct_query("SELECT * FROM data_sources")
    sources_by_id = {int(r["source_id"]): r for r in source_rows if r.get("source_id") is not None}
    return topics_by_event, lookup, sources_by_id


def _fallback_entities(title: str, summary: str) -> list[dict]:
    source_text = normalize_space(f"{title}. {summary}")
    raw_found = extract_entities(source_text)
    raw = [{"name": name, "type": typ, "aliases": []} for name, typ, _confidence in raw_found]
    entities, suppressed_people = augment_entities_with_named_infrastructure(title, summary, raw, content_text="")
    seen = {norm_name(str(x.get("name") or "")) for x in entities}
    for item in extract_named_person_entities(source_text):
        key = norm_name(item.get("name") or "")
        if key and key not in seen:
            entities.append(item)
            seen.add(key)
    for item in extract_named_proper_noun_entities(source_text):
        key = norm_name(item.get("name") or "")
        if key and key not in seen:
            entities.append(item)
            seen.add(key)
    out = []
    seen_types = set()
    for item in entities:
        name = normalize_space(str(item.get("name") or item.get("canonical_name") or ""))[:180]
        typ = str(item.get("type") or item.get("entity_type") or "UNKNOWN").upper().strip()
        aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
        key = (norm_name(name), typ)
        if not name or key in seen_types or not should_track_entity(name, typ):
            continue
        if typ == "PERSON" and norm_name(name) in suppressed_people:
            continue
        if not is_grounded_entity(name, aliases, source_text, typ):
            continue
        seen_types.add(key)
        out.append({"name": name, "type": typ, "aliases": aliases, "confidence": 0.72})
    return out


def _classification_for(items: list[dict], existing_topics: list[dict]) -> dict[int, dict]:
    """Batch-classify a bounded number of articles; deterministic fallback is always available."""
    from .genai_events import _call_openai

    result: dict[int, dict] = {}
    ai_limit = max(0, min(int(settings.ai_classification_run_limit), 48)) if settings.openai_api_key else 0
    ai_items = items[:ai_limit]
    if ai_items:
        existing_events = [{"event_topic_id": int(t["topic_id"]), "name": str(t.get("name") or ""),
                            "event_key": str(t.get("event_key") or "")} for t in existing_topics[-300:]]
        for offset in range(0, len(ai_items), 8):
            batch = ai_items[offset:offset + 8]
            payload = [{"article_id": int(x["article_id"]), "title": x["title"],
                        "summary": x["summary"], "content_excerpt": x["content_text"][:1800]} for x in batch]
            try:
                answers = _call_openai(payload, list(FIELD_CATEGORIES), existing_events)
                for answer in answers:
                    try:
                        aid = int(answer.get("article_id"))
                    except (TypeError, ValueError):
                        continue
                    result[aid] = {**answer, "_ai_success": True}
            except Exception as exc:
                log.warning("Direct Iceberg AI classification batch failed; deterministic fallback used: %s", exc)
    for item in items:
        aid = int(item["article_id"])
        answer = result.get(aid)
        if not answer:
            signature = extract_event_signature(item["title"])
            fallback_topic = (signature or {}).get("name") if isinstance(signature, dict) else None
            topic_name = normalize_space(str(fallback_topic or item["title"]))[:180]
            if is_broad_field_topic(topic_name) or len(norm_name(topic_name)) < 5:
                topic_name = normalize_space(item["title"])[:180]
            result[aid] = {"article_id": aid, "field_category": classify_field_category(f"{item['title']} {item['summary']}"),
                           "event_topic_name": topic_name, "event_confidence": 0.65,
                           "entities": _fallback_entities(item["title"], item["summary"]),
                           "_ai_success": False}
        else:
            answer["event_topic_name"] = normalize_space(str(answer.get("event_topic_name") or item["title"]))[:180]
            answer["field_category"] = normalize_field_category(answer.get("field_category"), item["title"], item["summary"])
            if not isinstance(answer.get("entities"), list):
                answer["entities"] = _fallback_entities(item["title"], item["summary"])
    return result


def _topic_for(name: str, lookup: dict[str, dict]) -> dict:
    name = normalize_space(name)[:180]
    normalized = norm_name(name)
    if not normalized or len(normalized) < 5 or is_broad_field_topic(name):
        name = "Sự kiện: " + name[:165]
        normalized = norm_name(name)
    event_key = "direct:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    if event_key in lookup:
        row = dict(lookup[event_key])
        if row.get("status") != "active":
            row["status"] = "active"
            row["updated_at"] = _iso()
        return row
    slug = ("event-" + slugify(name))[:160].strip("-") or "event"
    topic_id = _stable_id("topic", event_key)
    row = {"topic_id": topic_id, "slug": f"{slug}-{hashlib.sha256(event_key.encode()).hexdigest()[:8]}",
           "name": name, "description": "Tự động chuẩn hóa từ bài báo.", "keywords": [name],
           "parent_topic_id": None, "topic_type": "event", "event_key": event_key,
           "status": "active", "origin": "discovered", "created_at": _iso(), "updated_at": _iso()}
    lookup[event_key] = row
    return row


def _entity_records(raw_entities: list[dict], article: dict, lookup: dict) -> tuple[list[dict], list[dict]]:
    entity_rows: dict[int, dict] = {}
    alias_rows: dict[int, dict] = {}
    relation_rows: dict[tuple[int, str], dict] = {}
    source_text = normalize_space(f"{article['title']}. {article['summary']}")
    normalized_entities = []
    for item in raw_entities[:50]:
        if not isinstance(item, dict):
            continue
        name = normalize_space(str(item.get("name") or item.get("canonical_name") or ""))[:180]
        typ = str(item.get("type") or item.get("entity_type") or "UNKNOWN").upper().strip()
        if typ not in {"PERSON", "ORG", "LOCATION", "PRODUCT", "EVENT", "GROUP", "UNKNOWN"}:
            typ = "UNKNOWN"
        aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
        if not name or not should_track_entity(name, typ) or not is_grounded_entity(name, aliases, source_text, typ):
            continue
        canonical = strip_person_title(name) if typ == "PERSON" else name
        if typ == "PERSON" and norm_name(canonical) == "trump":
            canonical = "Donald Trump"
        key = entity_normalized_key(canonical, typ)
        if len(key) < 2:
            continue
        resolved_id, ambiguous = resolve_entity_in_lookup(canonical, typ, lookup, aliases)
        if ambiguous:
            log.info("Direct Iceberg skipped ambiguous entity alias %r", canonical)
            continue
        if resolved_id:
            entity = dict(lookup["entities_by_id"][int(resolved_id)])
            entity_id = int(resolved_id)
        else:
            entity_id = _stable_id("entity", key)
            entity = {"entity_id": entity_id, "canonical_name": canonical, "normalized_name": key,
                      "entity_type": typ, "external_id": None,
                      "description": "Tự động trích xuất và chuẩn hóa từ tin tức.",
                      "status": "active", "origin": "discovered", "created_at": _iso(), "updated_at": _iso()}
            variants = entity_name_variants(canonical, typ, aliases)
            add_entity_to_lookup(lookup, entity, variants)
        entity_rows[entity_id] = entity
        variants = entity_name_variants(name, typ, aliases)
        for alias in variants[:30]:
            alias_norm = norm_name(alias)
            if not alias_norm:
                continue
            alias_id = _stable_id("entity-alias", f"{entity_id}:{alias_norm}")
            alias_rows[alias_id] = {"alias_id": alias_id, "entity_id": entity_id, "alias": alias,
                                    "normalized_alias": alias_norm, "created_at": _iso()}
            # add to in-run lookup so variant names in the same batch resolve consistently.
            lookup["alias_to_ids"][alias_norm].add(entity_id)
        mention = normalize_space(name)
        confidence = item.get("confidence", 0.85)
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.85
        relation_rows[(entity_id, mention)] = {"article_id": int(article["article_id"]), "entity_id": entity_id,
                                                "mention_text": mention, "confidence": confidence, "created_at": _iso()}
    return list(entity_rows.values()) + list(alias_rows.values()), list(relation_rows.values())


def _split_entity_payload(entity_payload: list[dict], article: dict, lookup: dict):
    # _entity_records returns both entity and alias records in one sequence; split by keys.
    all_rows, links = _entity_records(entity_payload, article, lookup)
    entities = [r for r in all_rows if "canonical_name" in r]
    aliases = [r for r in all_rows if "alias_id" in r]
    return entities, aliases, links


def _write_source_mirror(source: dict) -> None:
    row = {k: v for k, v in source.items()}
    lakehouse.direct_upsert_records("data_sources", [row])


def _topic_records_for(items: list[dict], classifications: dict[int, dict], topic_lookup: dict[str, dict]):
    topics: dict[int, dict] = {}
    links: list[dict] = []
    for item in items:
        answer = classifications[int(item["article_id"])]
        topic = _topic_for(answer.get("event_topic_name") or item["title"], topic_lookup) if not item["is_excluded"] else None
        if topic:
            topics[int(topic["topic_id"])] = topic
            links.append({"article_id": int(item["article_id"]), "topic_id": int(topic["topic_id"]),
                          "confidence": float(answer.get("event_confidence") or 0.65),
                          "assignment_method": "openai" if answer.get("_ai_success") else "deterministic",
                          "created_at": _iso()})
        item["field_category"] = normalize_field_category(answer.get("field_category"), item["title"], item["summary"])
        item["ai_classified_at"] = _iso() if answer.get("_ai_success") else None
        item["ai_classification"] = {"direct_iceberg": True, "event_topic_name": topic["name"] if topic else None,
                                     "field_category": item["field_category"],
                                     "entities": answer.get("entities") or []}
        item["ai_classification_error"] = None if answer.get("_ai_success") else "Awaiting AI normalization; deterministic fallback used."
        item["event_topic_scanned_at"] = _iso()
        item["_assigned_topic_id"] = int(topic["topic_id"]) if topic else None
    return list(topics.values()), links


def _refresh_iceberg_facts(now_local: datetime) -> dict:
    """Recompute rolling FACTs from Iceberg using Python timezone conversion, no ICU SQL dependency."""
    tz = ZoneInfo(settings.timezone)
    local_now = now_local.astimezone(tz)
    today = local_now.date()
    stamp = _iso()
    first_day = today - timedelta(days=30)
    utc_start = datetime.combine(first_day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    def as_datetime(value):
        if isinstance(value, datetime):
            parsed = value
        elif value is None:
            return None
        else:
            try:
                parsed = date_parser.parse(str(value))
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def local_day(value):
        parsed = as_datetime(value)
        return parsed.astimezone(tz).date() if parsed else None

    # The raw article/bridge reads are restricted to the current rolling window and grouped
    # in Python by the configured local timezone. This works on the minimal DuckDB extension
    # set used in Render/GitHub Actions and avoids requiring ICU's SQL timezone() function.
    topic_rows = lakehouse.direct_query("""SELECT rel.article_id,rel.topic_id,a.source_id,
            coalesce(a.published_at,a.collected_at) AS article_at
        FROM article_topics rel JOIN news_articles a ON a.article_id=rel.article_id
        JOIN topics t ON t.topic_id=rel.topic_id
        WHERE a.is_excluded=false AND t.status='active' AND t.topic_type='event'
          AND coalesce(a.published_at,a.collected_at)>=?""", [utc_start])
    entity_rows = lakehouse.direct_query("""SELECT rel.article_id,rel.entity_id,a.source_id,
            coalesce(a.published_at,a.collected_at) AS article_at
        FROM article_entities rel JOIN news_articles a ON a.article_id=rel.article_id
        JOIN entities e ON e.entity_id=rel.entity_id
        WHERE a.is_excluded=false AND e.status='active'
          AND coalesce(a.published_at,a.collected_at)>=?""", [utc_start])
    source_rows = lakehouse.direct_query("""SELECT article_id,source_id,coalesce(published_at,collected_at) AS article_at,
            is_excluded,quality_score FROM news_articles
        WHERE coalesce(published_at,collected_at)>=?""", [utc_start])

    topic_groups: dict[tuple[str, int, int], set[int]] = {}
    for row in topic_rows:
        day = local_day(row.get('article_at'))
        if day is None or day < first_day:
            continue
        key = (day.isoformat(), int(row['topic_id']), int(row['source_id']))
        topic_groups.setdefault(key, set()).add(int(row['article_id']))
    topic_daily = [{'stat_date': day, 'topic_id': topic_id, 'source_id': source_id,
                    'article_count': len(article_ids), 'updated_at': stamp}
                   for (day, topic_id, source_id), article_ids in topic_groups.items()]

    entity_groups: dict[tuple[str, int, int], set[int]] = {}
    for row in entity_rows:
        day = local_day(row.get('article_at'))
        if day is None or day < first_day:
            continue
        key = (day.isoformat(), int(row['entity_id']), int(row['source_id']))
        entity_groups.setdefault(key, set()).add(int(row['article_id']))
    entity_daily = [{'stat_date': day, 'entity_id': entity_id, 'source_id': source_id,
                     'article_count': len(article_ids), 'updated_at': stamp}
                    for (day, entity_id, source_id), article_ids in entity_groups.items()]

    source_groups: dict[tuple[str, int], list[dict]] = {}
    for row in source_rows:
        day = local_day(row.get('article_at'))
        if day is None or day < first_day:
            continue
        source_groups.setdefault((day.isoformat(), int(row['source_id'])), []).append(row)
    source_daily = []
    for (day, source_id), group in source_groups.items():
        scores = [float(r['quality_score']) for r in group if r.get('quality_score') is not None]
        source_daily.append({'stat_date': day, 'source_id': source_id,
                             'article_count': len({int(r['article_id']) for r in group}),
                             'excluded_count': sum(1 for r in group if bool(r.get('is_excluded'))),
                             'avg_quality_score': (sum(scores) / len(scores)) if scores else None,
                             'updated_at': stamp})

    stats = {
        'fact_topic_daily': lakehouse.direct_upsert_records('fact_topic_daily', topic_daily),
        'fact_entity_daily': lakehouse.direct_upsert_records('fact_entity_daily', entity_daily),
        'fact_source_daily': lakehouse.direct_upsert_records('fact_source_daily', source_daily),
    }

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    previous_month_start = (month_start - timedelta(days=1)).replace(day=1)
    periods = [
        ('day', today, today, today + timedelta(days=1), today - timedelta(days=1), today),
        ('week', week_start, week_start, week_start + timedelta(days=7), week_start - timedelta(days=7), week_start),
        ('month', month_start, month_start, next_month, previous_month_start, month_start),
    ]

    def utc_bound(local_date):
        return datetime.combine(local_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    trending_rows = []
    for period_type, period_start, start_day, end_day, previous_start_day, _current_start in periods:
        current = lakehouse.direct_query("""SELECT rel.topic_id,a.article_id,a.source_id,
                coalesce(a.published_at,a.collected_at) AS article_at
            FROM article_topics rel JOIN news_articles a ON a.article_id=rel.article_id
            JOIN topics t ON t.topic_id=rel.topic_id
            WHERE a.is_excluded=false AND t.status='active' AND t.topic_type='event'
              AND coalesce(a.published_at,a.collected_at)>=? AND coalesce(a.published_at,a.collected_at)<?""",
            [utc_bound(start_day), utc_bound(end_day)])
        previous = lakehouse.direct_query("""SELECT rel.topic_id,a.article_id,
                coalesce(a.published_at,a.collected_at) AS article_at
            FROM article_topics rel JOIN news_articles a ON a.article_id=rel.article_id
            JOIN topics t ON t.topic_id=rel.topic_id
            WHERE a.is_excluded=false AND t.status='active' AND t.topic_type='event'
              AND coalesce(a.published_at,a.collected_at)>=? AND coalesce(a.published_at,a.collected_at)<?""",
            [utc_bound(previous_start_day), utc_bound(start_day)])
        current_articles: dict[int, set[int]] = {}
        current_sources: dict[int, set[int]] = {}
        previous_articles: dict[int, set[int]] = {}
        for row in current:
            topic_id = int(row['topic_id'])
            current_articles.setdefault(topic_id, set()).add(int(row['article_id']))
            current_sources.setdefault(topic_id, set()).add(int(row['source_id']))
        for row in previous:
            previous_articles.setdefault(int(row['topic_id']), set()).add(int(row['article_id']))
        for topic_id, article_ids in current_articles.items():
            count = len(article_ids)
            source_count = len(current_sources.get(topic_id, set()))
            previous_count = len(previous_articles.get(topic_id, set()))
            growth = round(100.0 * (count - previous_count) / previous_count, 2) if previous_count else None
            bonus = min(max(growth or 0, 0), 200) / 100.0
            hotness = round(count + (2.0 * math.log(1 + source_count)) + bonus, 3)
            trending_rows.append({'period_type': period_type, 'period_start': period_start.isoformat(),
                                  'topic_id': topic_id, 'article_count': count, 'source_count': source_count,
                                  'previous_article_count': previous_count, 'growth_pct': growth,
                                  'hotness_score': hotness, 'rank_position': 0, 'updated_at': stamp})
    for period_type, period_start, *_ in periods:
        group = [r for r in trending_rows if r['period_type'] == period_type and r['period_start'] == period_start.isoformat()]
        group.sort(key=lambda r: (-r['hotness_score'], -r['article_count'], -r['source_count'], r['topic_id']))
        for rank, row in enumerate(group, 1):
            row['rank_position'] = rank
    stats['fact_topic_trending'] = lakehouse.direct_upsert_records('fact_topic_trending', trending_rows)
    return stats

def _run_direct_pipeline_impl(trigger_type: str = "scheduled", source_ids: list[int] | None = None) -> dict:
    """Run RSS -> temporary R2 raw -> clean Iceberg writes; do not insert clean rows in PG."""
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for operational source configuration and run logs")
    if not lakehouse.direct_writes_enabled():
        raise RuntimeError("Direct Iceberg ingestion is disabled; set CLOUDFLARE_LAKEHOUSE_DIRECT_WRITES=true in GitHub Actions")
    if not raw_store.raw_storage_enabled():
        raise RuntimeError("Direct-write mode requires CLOUDFLARE_R2_RAW_ENABLED=true so raw RSS is staged in R2 before cleaning")
    from .pipeline import extract_feed_content  # keep content extraction identical to the legacy parser
    from .genai_events import _call_openai

    started = _now()
    run_clock = time.monotonic()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pipeline_runs(trigger_type,status,started_at,message) VALUES(%s,'running',now(),'Direct-to-Iceberg ingestion') RETURNING run_id", (trigger_type,))
        run_id = int(cur.fetchone()["run_id"])
    sources, rules = _fetch_operational_config(source_ids)
    totals = {"run_id": run_id, "storage_mode": "direct-iceberg", "sources": len(sources),
              "entries_seen": 0, "articles_inserted": 0, "entries_skipped_existing": 0,
              "entries_skipped_outside_window": 0, "entries_skipped_undated": 0,
              "raw_r2_uploaded": 0, "raw_r2_deleted": 0, "raw_r2_delete_failed": 0,
              "ai_articles_classified": 0, "ai_articles_fallback": 0, "entities_written": 0,
              "topics_written": 0, "source_errors": []}
    tz = ZoneInfo(settings.timezone)
    cutoff = _now() - timedelta(hours=max(1, settings.pipeline_lookback_hours))
    try:
        topic_lookup, entity_lookup, iceberg_sources = _load_iceberg_catalogs()
        for source in sources:
            source_id = int(source["source_id"])
            log.info("Direct Iceberg source start: id=%s name=%s url=%s", source_id, source.get("name"), source.get("feed_url"))
            try:
                response = requests.get(source["feed_url"], timeout=(10, settings.request_timeout_seconds), headers=_source_headers(source))
                if response.status_code == 304:
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE data_sources SET last_run_at=now(),last_status='success',last_error=NULL,updated_at=now() WHERE source_id=%s", (source_id,))
                        cur.execute("SELECT * FROM data_sources WHERE source_id=%s", (source_id,))
                        source_current = dict(cur.fetchone())
                    _write_source_mirror(source_current)
                    log.info("Direct Iceberg source unchanged (304): %s", source["name"])
                    continue
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                if parsed.bozo and not parsed.entries:
                    raise RuntimeError(f"RSS parsing failed: {str(parsed.bozo_exception)[:250]}")
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE data_sources SET etag=coalesce(%s,etag),last_modified=coalesce(%s,last_modified),updated_at=now() WHERE source_id=%s",
                                (response.headers.get("ETag"), response.headers.get("Last-Modified"), source_id))
                prepared = []
                for entry in parsed.entries[:settings.max_feed_entries_per_source]:
                    totals["entries_seen"] += 1
                    title = normalize_space(clean_html(entry.get("title", "")))
                    raw_url = str(entry.get("link") or "").strip()
                    canonical = canonicalize_url(raw_url)
                    published = _parse_date(entry)
                    if not title or not canonical:
                        continue
                    if published is None:
                        totals["entries_skipped_undated"] += 1
                        continue
                    if published < cutoff:
                        totals["entries_skipped_outside_window"] += 1
                        continue
                    summary, content_text = _extract_content(entry)
                    author = normalize_space(str(entry.get("author") or ""))[:250] or None
                    external_id = str(entry.get("id") or entry.get("guid") or "")[:1000] or None
                    entry_key = external_id or canonical or hash_text(title)
                    payload = json.loads(json.dumps({str(k): entry.get(k) for k in entry.keys()}, default=str))
                    aid = _stable_id("article", f"{source_id}:{canonical}")
                    image_url = extract_image_url(entry, raw_url)
                    prepared.append({"article_id": aid, "source_id": source_id, "title": title, "raw_url": raw_url,
                                     "canonical_url": canonical, "published_at": published, "summary": summary,
                                     "content_text": content_text, "author": author, "external_id": external_id,
                                     "entry_key": entry_key, "payload": payload, "image_url": image_url,
                                     "is_excluded": False, "exclude_reason": None, "quality_score": 100})
                if not prepared:
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE data_sources SET last_run_at=now(),last_status='success',last_error=NULL,updated_at=now() WHERE source_id=%s", (source_id,))
                        cur.execute("SELECT * FROM data_sources WHERE source_id=%s", (source_id,))
                        _write_source_mirror(dict(cur.fetchone()))
                    continue
                urls = list(dict.fromkeys(x["canonical_url"] for x in prepared))
                existing = {}
                for offset in range(0, len(urls), 150):
                    chunk = urls[offset:offset + 150]
                    in_sql = ",".join("?" for _ in chunk)
                    for row in lakehouse.direct_query(f"SELECT article_id,canonical_url,is_excluded FROM news_articles WHERE source_id=? AND canonical_url IN ({in_sql})",
                                                      [source_id, *chunk]):
                        existing[str(row["canonical_url"])] = row
                new_items = []
                repair_fact_rows = []
                existing_raw_cleanup: list[tuple[dict, int]] = []
                existing_fact_ids = set()
                existing_ids = [int(r["article_id"]) for r in existing.values()]
                if existing_ids:
                    for offset in range(0, len(existing_ids), 150):
                        chunk_ids = existing_ids[offset:offset + 150]
                        in_sql = ",".join("?" for _ in chunk_ids)
                        facts_present = lakehouse.direct_query(f"SELECT article_id FROM fact_news_article WHERE article_id IN ({in_sql})", chunk_ids)
                        existing_fact_ids.update(int(r["article_id"]) for r in facts_present)
                for item in prepared:
                    old = existing.get(item["canonical_url"])
                    if old:
                        totals["entries_skipped_existing"] += 1
                        old_id = int(old["article_id"])
                        existing_raw_cleanup.append((item, old_id))
                        if old_id not in existing_fact_ids:
                            # Resume a prior partial write: reconstruct its article FACT without
                            # re-inserting the clean article into PostgreSQL or Iceberg.
                            old_rows = lakehouse.direct_query("SELECT * FROM news_articles WHERE article_id=?", [old_id])
                            if old_rows:
                                old_row = old_rows[0]
                                old_content = str(old_row.get("content_text") or "")
                                old_topics = lakehouse.direct_query("SELECT topic_id FROM article_topics WHERE article_id=?", [old_id])
                                old_entities = lakehouse.direct_query("SELECT entity_id FROM article_entities WHERE article_id=?", [old_id])
                                old_pub = old_row.get("published_at") or old_row.get("collected_at")
                                old_pub_date = old_pub
                                if isinstance(old_pub, str):
                                    try: old_pub_date = date_parser.parse(old_pub)
                                    except Exception: old_pub_date = None
                                if isinstance(old_pub_date, datetime):
                                    old_pub_date = old_pub_date.astimezone(tz).date().isoformat()
                                elif hasattr(old_pub_date, "isoformat"):
                                    old_pub_date = old_pub_date.isoformat()
                                repair_fact_rows.append({"article_id": old_id, "source_id": int(old_row["source_id"]),
                                    "published_date": old_pub_date, "title_length": len(str(old_row.get("title") or "")),
                                    "word_count": len(re.findall(r"\S+", old_content)),
                                    "detected_entity_count": len({int(x["entity_id"]) for x in old_entities}),
                                    "detected_topic_count": len({int(x["topic_id"]) for x in old_topics}),
                                    "quality_score": float(old_row.get("quality_score") or 0),
                                    "is_excluded": bool(old_row.get("is_excluded")), "loaded_at": _iso()})
                        # Raw objects for existing rows are cleaned only after checking that
                        # their base FACT already exists or can be repaired and re-read below.
                        continue
                    # Stage the original RSS entry in R2 before any cleaning rules, quality
                    # scoring, AI classification, or clean Iceberg writes. On every downstream
                    # failure the object remains available for retry and recovery.
                    item["_raw_key"] = raw_store.store_raw_entry(source_id, item["entry_key"], item["raw_url"], item["payload"])
                    totals["raw_r2_uploaded"] += 1
                    excluded, reason = apply_cleaning_rules(item["title"], item["canonical_url"], source_id, rules)
                    item["is_excluded"] = bool(excluded)
                    item["exclude_reason"] = reason
                    quality = 100
                    if len(item["title"]) < 15: quality -= 15
                    if len(item["content_text"]) < 80: quality -= 20
                    if not item["author"]: quality -= 5
                    item["quality_score"] = max(0, quality)
                    new_items.append(item)
                if repair_fact_rows:
                    lakehouse.direct_upsert_records("fact_news_article", repair_fact_rows)
                    # Confirm repaired base FACTs are visible before deleting any retained raw object.
                    repair_ids = [int(r["article_id"]) for r in repair_fact_rows]
                    for offset in range(0, len(repair_ids), 150):
                        chunk_ids = repair_ids[offset:offset + 150]
                        p = ",".join("?" for _ in chunk_ids)
                        repair_visible = lakehouse.direct_query(f"SELECT article_id FROM fact_news_article WHERE article_id IN ({p})", chunk_ids)
                        existing_fact_ids.update(int(r["article_id"]) for r in repair_visible)
                for old_item, old_id in existing_raw_cleanup:
                    if old_id not in existing_fact_ids:
                        log.warning("Retaining raw object because article/base FACT is not verified: %s", old_item["canonical_url"])
                        continue
                    try:
                        raw_store.delete_raw_object(raw_store.object_key(source_id, old_item["entry_key"]))
                        totals["raw_r2_deleted"] += 1
                    except Exception as exc:
                        totals["raw_r2_delete_failed"] += 1
                        log.warning("Existing article raw cleanup deferred url=%s: %s", old_item["canonical_url"], exc)
                if not new_items:
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE data_sources SET last_run_at=now(),last_status='success',last_error=NULL,updated_at=now() WHERE source_id=%s", (source_id,))
                        cur.execute("SELECT * FROM data_sources WHERE source_id=%s", (source_id,))
                        _write_source_mirror(dict(cur.fetchone()))
                    continue
                # RSS image metadata is preferred; bounded Open Graph lookup handles only missing image URLs.
                missing_images = [x for x in new_items if not x.get("image_url")]
                for x in missing_images:
                    x["raw_url"] = x["raw_url"]
                    x["image_source"] = None
                    x["image_lookup_attempted"] = False
                _attempts, _found = _lookup_open_graph_images(missing_images, min(_IMAGE_OG_FALLBACK_LIMIT_PER_SOURCE, len(missing_images)))
                for item in new_items:
                    if item.get("image_url") is None and item.get("image_lookup_attempted"):
                        item["image_checked_at"] = _iso()
                    else:
                        item["image_checked_at"] = _iso() if item.get("image_url") else None
                classifications = _classification_for(new_items, list(topic_lookup.values()))
                topics, article_topic_links = _topic_records_for(new_items, classifications, topic_lookup)
                entities_to_write: dict[int, dict] = {}
                aliases_to_write: dict[int, dict] = {}
                article_entity_links = []
                article_rows = []
                article_facts = []
                for item in new_items:
                    answer = classifications[int(item["article_id"])]
                    entity_payload = answer.get("entities") if isinstance(answer.get("entities"), list) else []
                    if not answer.get("_ai_success"):
                        entity_payload = _fallback_entities(item["title"], item["summary"])
                    if item["is_excluded"]:
                        entity_payload = []
                    entities, aliases, links = _split_entity_payload(entity_payload, item, entity_lookup)
                    for ent in entities: entities_to_write[int(ent["entity_id"])] = ent
                    for ali in aliases: aliases_to_write[int(ali["alias_id"])] = ali
                    article_entity_links.extend(links if not item["is_excluded"] else [])
                    now_iso = _iso()
                    content = str(item["content_text"] or "")
                    article = {"article_id": int(item["article_id"]), "source_id": source_id, "raw_id": None,
                               "external_id": item["external_id"], "url": item["raw_url"], "canonical_url": item["canonical_url"],
                               "title": item["title"], "summary": item["summary"], "content_text": content,
                               "author": item["author"], "image_url": item.get("image_url"),
                               "image_checked_at": item.get("image_checked_at"), "language": source.get("language") or "vi",
                               "published_at": item["published_at"].isoformat(), "collected_at": now_iso,
                               "updated_at": now_iso, "headline_hash": hash_text(item["title"]),
                               "content_hash": hash_text(content) if content else None,
                               "is_excluded": bool(item["is_excluded"]), "exclude_reason": item["exclude_reason"],
                               "quality_score": float(item["quality_score"]), "field_category": item["field_category"],
                               "event_topic_scanned_at": item["event_topic_scanned_at"],
                               "ai_classified_at": item["ai_classified_at"],
                               "ai_classification": item["ai_classification"],
                               "ai_classification_error": item["ai_classification_error"],
                               "timeline_id": int(item["article_id"])}
                    article_rows.append(article)
                    local_date = item["published_at"].astimezone(tz).date().isoformat()
                    article_facts.append({"article_id": int(item["article_id"]), "source_id": source_id,
                                          "published_date": local_date, "title_length": len(item["title"]),
                                          "word_count": len(re.findall(r"\S+", content)),
                                          "detected_entity_count": len({x["entity_id"] for x in links}),
                                          "detected_topic_count": 0 if item["is_excluded"] else 1,
                                          "quality_score": float(item["quality_score"]),
                                          "is_excluded": bool(item["is_excluded"]), "loaded_at": now_iso})
                # Keep source configuration as operational PostgreSQL data, but mirror it to Iceberg
                # so every News Radar read can join the source name without depending on PG article rows.
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE data_sources SET last_run_at=now(),last_status='success',last_error=NULL,updated_at=now() WHERE source_id=%s", (source_id,))
                    cur.execute("SELECT * FROM data_sources WHERE source_id=%s", (source_id,))
                    source_current = dict(cur.fetchone())
                _write_source_mirror(source_current)
                if topics: totals["topics_written"] += lakehouse.direct_upsert_records("topics", topics)
                if entities_to_write: totals["entities_written"] += lakehouse.direct_upsert_records("entities", list(entities_to_write.values()))
                if aliases_to_write: lakehouse.direct_upsert_records("entity_aliases", list(aliases_to_write.values()))
                if article_rows: lakehouse.direct_upsert_records("news_articles", article_rows)
                if article_topic_links: lakehouse.direct_upsert_records("article_topics", article_topic_links)
                if article_entity_links: lakehouse.direct_upsert_records("article_entities", article_entity_links)
                if article_facts: lakehouse.direct_upsert_records("fact_news_article", article_facts)
                # Verify that the clean row AND its base FACT row are visible before deleting raw.
                ids = [int(x["article_id"]) for x in article_rows]
                visible = {}
                for offset in range(0, len(ids), 100):
                    chunk = ids[offset:offset + 100]
                    p = ",".join("?" for _ in chunk)
                    rows = lakehouse.direct_query(f"SELECT a.article_id,(f.article_id IS NOT NULL) AS fact_present FROM news_articles a LEFT JOIN fact_news_article f ON f.article_id=a.article_id WHERE a.article_id IN ({p})", chunk)
                    visible.update({int(r["article_id"]): bool(r["fact_present"]) for r in rows})
                for item in new_items:
                    aid = int(item["article_id"])
                    if not visible.get(aid, False):
                        log.error("Clean direct write did not verify for article_id=%s; retaining raw object %s", aid, item["_raw_key"])
                        continue
                    try:
                        raw_store.delete_raw_object(item["_raw_key"])
                        totals["raw_r2_deleted"] += 1
                    except Exception as exc:
                        totals["raw_r2_delete_failed"] += 1
                        log.error("Could not delete verified raw object %s: %s", item["_raw_key"], exc)
                    totals["articles_inserted"] += 1
                    totals["ai_articles_classified"] += 1 if classifications[aid].get("_ai_success") else 0
                    totals["ai_articles_fallback"] += 0 if classifications[aid].get("_ai_success") else 1
                log.info("Direct Iceberg source complete: source=%s new=%s raw_uploaded=%s raw_deleted=%s",
                         source.get("name"), len(article_rows), len(new_items), totals["raw_r2_deleted"])
            except Exception as exc:
                msg = f"{type(exc).__name__}: {str(exc)[:400]}"
                log.exception("Direct Iceberg source failed source=%s: %s", source.get("name"), msg)
                totals["source_errors"].append({"source_id": source_id, "name": source.get("name"), "error": msg})
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE data_sources SET last_run_at=now(),last_status='error',last_error=%s,updated_at=now() WHERE source_id=%s", (msg, source_id))
                    cur.execute("SELECT * FROM data_sources WHERE source_id=%s", (source_id,))
                    source_current = dict(cur.fetchone())
                try:
                    _write_source_mirror(source_current)
                except Exception:
                    log.exception("Could not mirror failed source status into Iceberg")
        # Recalculate current aggregate FACTs from Iceberg, never from PostgreSQL article tables.
        local_now = _now().astimezone(tz)
        totals["fact_refresh"] = _refresh_iceberg_facts(local_now)
        status = "success" if not totals["source_errors"] else ("partial" if totals["articles_inserted"] else "failed")
        totals["status"] = status
        run_message = f"Direct Iceberg ingestion completed in {(time.monotonic()-run_clock):.1f}s; {len(totals['source_errors'])} source error(s)."
        if totals["source_errors"]:
            run_message += " First errors: " + " | ".join(f"{x.get('name')}: {x.get('error')}" for x in totals["source_errors"][:3])
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE pipeline_runs SET status=%s,finished_at=now(),source_count=%s,entries_seen=%s,articles_inserted=%s,entries_skipped_existing=%s,message=%s,details=%s::jsonb WHERE run_id=%s",
                        (status, totals["sources"], totals["entries_seen"], totals["articles_inserted"], totals["entries_skipped_existing"],
                         run_message[:1000], json.dumps(totals, ensure_ascii=False, default=str), run_id))
        log.info("Direct Iceberg pipeline %s finished: %s", run_id, totals)
        return totals
    except Exception as exc:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE pipeline_runs SET status='failed',finished_at=now(),message=%s,details=%s::jsonb WHERE run_id=%s",
                        (f"{type(exc).__name__}: {str(exc)[:900]}", json.dumps(totals, ensure_ascii=False, default=str), run_id))
        raise
