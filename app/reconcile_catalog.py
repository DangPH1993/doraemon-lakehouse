"""Scheduled GenAI reconciliation for equivalent topics/entities in recent Radar articles.

This is deliberately separate from RSS ingestion/classification and Telegram delivery. It
compares candidate catalog records linked to the rolling 48-hour article window, stores pair
decisions to avoid paying for the same comparison repeatedly, transfers references to canonical
IDs, and archives duplicates rather than deleting history.
"""
from __future__ import annotations

import hashlib
import itertools
from collections import defaultdict
import json
import logging
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import psycopg
import requests
from psycopg.rows import dict_row

from .config import settings
from .db import get_conn
from .field_categories import is_broad_field_topic
from .processing import norm_name

log = logging.getLogger('mini-lakehouse.reconcile')
OPENAI_URL = 'https://api.openai.com/v1/chat/completions'
WINDOW_HOURS = max(1, int(os.getenv('RECONCILIATION_WINDOW_HOURS', '48')))
MAX_TOPICS = max(20, min(20000, int(os.getenv('RECONCILIATION_MAX_TOPICS', '5000'))))
MAX_ENTITIES = max(20, min(30000, int(os.getenv('RECONCILIATION_MAX_ENTITIES', '12000'))))
MAX_PAIRS_PER_TYPE = max(10, min(250, int(os.getenv('RECONCILIATION_MAX_PAIRS_PER_TYPE', '80'))))
PAIRS_PER_REQUEST = max(5, min(30, int(os.getenv('RECONCILIATION_PAIRS_PER_REQUEST', '20'))))
MERGE_CONFIDENCE = max(0.5, min(1.0, float(os.getenv('RECONCILIATION_MERGE_CONFIDENCE', '0.88'))))

_DDL = (
    """CREATE TABLE IF NOT EXISTS topic_aliases (
         topic_alias_id bigserial PRIMARY KEY,
         topic_id bigint NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
         alias text NOT NULL, normalized_alias text NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(topic_id,normalized_alias))""",
    "CREATE INDEX IF NOT EXISTS idx_topic_aliases_normalized ON topic_aliases(normalized_alias)",
    """CREATE TABLE IF NOT EXISTS catalog_reconciliation_runs (
         run_id bigserial PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
         status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','success','partial','failed','skipped')),
         window_hours integer NOT NULL DEFAULT 48, topics_seen integer NOT NULL DEFAULT 0, entities_seen integer NOT NULL DEFAULT 0,
         topic_pairs_checked integer NOT NULL DEFAULT 0, entity_pairs_checked integer NOT NULL DEFAULT 0,
         topic_merges integer NOT NULL DEFAULT 0, entity_merges integer NOT NULL DEFAULT 0, llm_calls integer NOT NULL DEFAULT 0,
         message text NOT NULL DEFAULT '', details jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    """CREATE TABLE IF NOT EXISTS catalog_reconciliation_decisions (
         object_type text NOT NULL CHECK(object_type IN ('topic','entity')),
         left_id bigint NOT NULL, right_id bigint NOT NULL, input_fingerprint text NOT NULL,
         same_object boolean NOT NULL, confidence numeric(5,4) NOT NULL DEFAULT 0, reason text NOT NULL DEFAULT '',
         decided_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(object_type,left_id,right_id), CHECK(left_id<right_id))""",
    """CREATE TABLE IF NOT EXISTS catalog_reconciliation_merges (
         object_type text NOT NULL CHECK(object_type IN ('topic','entity')),
         source_id bigint NOT NULL, target_id bigint NOT NULL, merged_at timestamptz NOT NULL DEFAULT now(),
         reason text NOT NULL DEFAULT '', PRIMARY KEY(object_type,source_id), CHECK(source_id<>target_id))""",
)
_STOP = set('''
    va la cua co cho voi trong tren duoi tai tu den khi nhung cac mot nay do duoc dang se da
    nguoi ngay nam thang theo ve viec khong cung nhung nhat sau truoc tiep theo the a an and or
    but for with from after before over under this that those these said report news latest today
    yesterday trong khi do du kien cac loai noi dung
'''.split())


def _fold(text: str) -> str:
    text = str(text or '').replace('đ', 'd').replace('Đ', 'D').casefold()
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _fold(text).split() if (len(t) >= 3 and t not in _STOP) or (t.isdigit() and len(t) >= 2)}


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (TypeError, ValueError):
            return []
    return []


def ensure_schema() -> None:
    """Create only the reconciliation support tables, idempotently, on first use."""
    with get_conn() as conn, conn.cursor() as cur:
        for statement in _DDL:
            cur.execute(statement)


def load_recent_topics() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT t.topic_id AS id,t.name,t.description,t.keywords,t.topic_type,t.created_at,
                              count(DISTINCT a.article_id)::int AS article_count,
                              max(coalesce(a.published_at,a.collected_at)) AS last_seen_at,
                              (array_agg(a.title ORDER BY coalesce(a.published_at,a.collected_at) DESC))[1:3] AS sample_titles,
                              coalesce((SELECT array_agg(ta.alias ORDER BY ta.alias) FROM topic_aliases ta WHERE ta.topic_id=t.topic_id),ARRAY[]::text[]) AS aliases
                       FROM topics t JOIN article_topics at ON at.topic_id=t.topic_id
                       JOIN news_articles a ON a.article_id=at.article_id
                       WHERE t.status='active' AND t.topic_type='event' AND a.is_excluded=false
                         AND coalesce(a.published_at,a.collected_at)>=now()-(%s * interval '1 hour')
                       GROUP BY t.topic_id,t.name,t.description,t.keywords,t.topic_type,t.created_at
                       ORDER BY max(coalesce(a.published_at,a.collected_at)) DESC,count(DISTINCT a.article_id) DESC,t.topic_id
                       LIMIT %s""", (WINDOW_HOURS, MAX_TOPICS))
        rows = cur.fetchall()
    return [dict(row) for row in rows if not is_broad_field_topic(row.get('name'))]


def load_recent_entities() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT e.entity_id AS id,e.canonical_name AS name,e.entity_type,e.normalized_name,e.created_at,
                              count(DISTINCT a.article_id)::int AS article_count,
                              max(coalesce(a.published_at,a.collected_at)) AS last_seen_at,
                              (array_agg(DISTINCT a.title))[1:3] AS sample_titles,
                              coalesce((SELECT array_agg(ea.alias ORDER BY ea.alias) FROM entity_aliases ea WHERE ea.entity_id=e.entity_id),ARRAY[]::text[]) AS aliases
                       FROM entities e JOIN article_entities ae ON ae.entity_id=e.entity_id
                       JOIN news_articles a ON a.article_id=ae.article_id
                       WHERE e.status='active' AND a.is_excluded=false
                         AND coalesce(a.published_at,a.collected_at)>=now()-(%s * interval '1 hour')
                       GROUP BY e.entity_id,e.canonical_name,e.entity_type,e.normalized_name,e.created_at
                       ORDER BY max(coalesce(a.published_at,a.collected_at)) DESC,count(DISTINCT a.article_id) DESC,e.entity_id
                       LIMIT %s""", (WINDOW_HOURS, MAX_ENTITIES))
        return [dict(row) for row in cur.fetchall()]


def _fingerprint(kind: str, item: dict) -> str:
    payload = {
        'kind': kind, 'id': int(item['id']), 'name': item.get('name') or '',
        'type': item.get('entity_type') or item.get('topic_type') or '',
        'aliases': sorted(str(x) for x in _json_list(item.get('aliases'))),
        'description': item.get('description') or '',
        'keywords': sorted(str(x) for x in _json_list(item.get('keywords'))),
        'sample_titles': [str(x) for x in _json_list(item.get('sample_titles'))[:3]],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def _pair_score(kind: str, left: dict, right: dict) -> float:
    if kind == 'entity':
        lt = str(left.get('entity_type') or 'UNKNOWN').upper()
        rt = str(right.get('entity_type') or 'UNKNOWN').upper()
        if lt != rt and 'UNKNOWN' not in {lt, rt}:
            return 0.0
    names_left = [str(left.get('name') or ''), *map(str, _json_list(left.get('aliases')))]
    names_right = [str(right.get('name') or ''), *map(str, _json_list(right.get('aliases')))]
    score = 0.0
    has_shared_token = False
    for left_name in names_left:
        left_fold = _fold(left_name)
        left_tokens = _tokens(left_name)
        for right_name in names_right:
            right_fold = _fold(right_name)
            right_tokens = _tokens(right_name)
            if not left_fold or not right_fold:
                continue
            if left_fold == right_fold:
                return 1.0
            score = max(score, SequenceMatcher(None, left_fold, right_fold).ratio())
            shared = left_tokens & right_tokens
            has_shared_token = has_shared_token or bool(shared)
            if shared:
                score = max(score, 0.34 + 0.07 * min(len(shared), 3))
    left_context = ' '.join(map(str, _json_list(left.get('sample_titles'))[:2]))
    right_context = ' '.join(map(str, _json_list(right.get('sample_titles'))[:2]))
    shared_context = _tokens(left_context) & _tokens(right_context)
    if len(shared_context) >= 2:
        score = max(score, 0.30 + min(0.25, len(shared_context) * 0.035))
    return score if score >= 0.34 or (has_shared_token and score >= 0.25) else 0.0


def _blocking_keys(item: dict) -> set[str]:
    """Create high-signal blocks so reconciliation does not compare every catalog pair."""
    names = [str(item.get('name') or ''), *map(str, _json_list(item.get('aliases')))]
    keys: set[str] = set()
    for name in names:
        folded = _fold(name)
        if folded:
            keys.add('exact:' + folded)
        for token in _tokens(name):
            keys.add('token:' + token)
            if len(token) >= 5:
                keys.add('prefix:' + token[:4])
    return keys


def candidate_pairs(kind: str, items: list[dict]) -> list[dict]:
    """Build candidate pairs by name/alias blocks, avoiding O(n²) catalog scans.

    Exact normalized names are always compared. Non-exact token/prefix blocks that are too
    common are skipped; a pair with another more specific shared token can still be found.
    """
    by_id = {int(item['id']): item for item in items}
    if len(by_id) < 2:
        return []
    blocks: dict[str, list[int]] = defaultdict(list)
    # Preserve caller ordering (recently seen first) for deterministic caps on common blocks.
    for item in items:
        ident = int(item['id'])
        for key in _blocking_keys(item):
            blocks[key].append(ident)
    possible: set[tuple[int, int]] = set()
    for key, bucket in blocks.items():
        if len(bucket) < 2:
            continue
        if key.startswith('exact:'):
            # Duplicate exact names are high-value. Cap extreme collisions to recent items.
            bucket = bucket[:500]
        elif len(bucket) > 100:
            # Generic/high-frequency tokens (for example a common surname) must not create
            # tens of thousands of meaningless candidate pairs on their own.
            continue
        for left_id, right_id in itertools.combinations(bucket, 2):
            if left_id == right_id:
                continue
            possible.add((min(left_id, right_id), max(left_id, right_id)))
    pairs = []
    for left_id, right_id in possible:
        item_a, item_b = by_id[left_id], by_id[right_id]
        score = _pair_score(kind, item_a, item_b)
        if score <= 0:
            continue
        fingerprint = hashlib.sha256((kind + '|' + _fingerprint(kind, item_a) + '|' + _fingerprint(kind, item_b)).encode()).hexdigest()[:32]
        pairs.append({'object_type': kind, 'left_id': left_id, 'right_id': right_id,
                      'left': item_a, 'right': item_b, 'score': score, 'fingerprint': fingerprint})
    pairs.sort(key=lambda pair: (-pair['score'], pair['left_id'], pair['right_id']))
    return pairs


def known_decisions(pairs: list[dict]) -> dict[tuple[str, int, int], str]:
    if not pairs:
        return {}
    ids_by_type: dict[str, set[int]] = {}
    for pair in pairs:
        ids_by_type.setdefault(pair['object_type'], set()).update((pair['left_id'], pair['right_id']))
    result = {}
    with get_conn() as conn, conn.cursor() as cur:
        for kind, ids in ids_by_type.items():
            id_list = list(ids)
            cur.execute("""SELECT object_type,left_id,right_id,input_fingerprint
                           FROM catalog_reconciliation_decisions
                           WHERE object_type=%s AND left_id=ANY(%s::bigint[]) AND right_id=ANY(%s::bigint[])""",
                        (kind, id_list, id_list))
            for row in cur.fetchall():
                result[(kind, int(row['left_id']), int(row['right_id']))] = row['input_fingerprint']
    return result


def _pair_context(item: dict, kind: str) -> dict:
    result = {
        'id': int(item['id']), 'name': str(item.get('name') or '')[:160],
        'aliases': [str(x)[:90] for x in _json_list(item.get('aliases'))[:6]],
        'recent_article_count': int(item.get('article_count') or 0),
        'sample_titles': [str(x)[:170] for x in _json_list(item.get('sample_titles'))[:2]],
    }
    if kind == 'entity':
        result['entity_type'] = str(item.get('entity_type') or 'UNKNOWN')
    else:
        result['description'] = str(item.get('description') or '')[:160]
        result['keywords'] = [str(x)[:60] for x in _json_list(item.get('keywords'))[:6]]
    return result


def call_openai_pairs(kind: str, pairs: list[dict]) -> list[dict]:
    if not settings.openai_api_key:
        raise RuntimeError('OPENAI_API_KEY is required for catalog reconciliation')
    if kind == 'entity':
        rules = ('Only mark true when both records refer to the exact same named real-world entity. Use entity_type, canonical names, aliases and sample headlines. '
                 'Do not merge different people/organizations because they share one word. Types must match, unless one side is UNKNOWN.')
    else:
        rules = ('Only mark true when both labels describe the same specific event/topic, not merely the same field, category, person, sector, or related event. '
                 'Broad field labels must never be treated as event topics. Prefer false when uncertain.')
    prompt = {
        'task': f'reconcile equivalent {kind} records', 'rules': rules,
        'merge_confidence_threshold': MERGE_CONFIDENCE,
        'response_schema': {'decisions': [{'pair_key': 'left_id:right_id', 'same_object': False, 'confidence': 0.0, 'reason': 'short explanation'}]},
        'pairs': [{'pair_key': f"{p['left_id']}:{p['right_id']}",
                   'left': _pair_context(p['left'], kind), 'right': _pair_context(p['right'], kind)} for p in pairs],
    }
    payload = {
        'model': settings.openai_model.strip() or 'gpt-4.1-mini',
        'messages': [
            {'role': 'system', 'content': 'Return only valid JSON, no markdown. Decide every supplied pair exactly once; never invent pair keys.'},
            {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)},
        ],
        'response_format': {'type': 'json_object'}, 'temperature': 0.0, 'max_completion_tokens': 3500,
    }
    response = requests.post(OPENAI_URL, headers={'Authorization': f'Bearer {settings.openai_api_key}', 'Content-Type': 'application/json'},
                             json=payload, timeout=(10, max(10, settings.ai_request_timeout_seconds)))
    if not response.ok:
        raise RuntimeError(f'OpenAI HTTP {response.status_code}: {response.text[:300]}')
    try:
        content = response.json()['choices'][0]['message']['content']
        parsed = json.loads(content)
    except Exception as exc:
        raise RuntimeError('OpenAI reconciliation returned invalid JSON') from exc
    decisions = parsed.get('decisions') if isinstance(parsed, dict) else None
    if not isinstance(decisions, list):
        raise RuntimeError('OpenAI response is missing decisions[]')
    return [d for d in decisions if isinstance(d, dict)]


def _resolve_canonical(cur, kind: str, ident: int) -> int:
    current = int(ident)
    seen = set()
    for _ in range(32):
        if current in seen:
            raise RuntimeError(f'Cycle in {kind} merge map at ID {current}')
        seen.add(current)
        cur.execute('SELECT target_id FROM catalog_reconciliation_merges WHERE object_type=%s AND source_id=%s', (kind, current))
        row = cur.fetchone()
        if not row:
            return current
        current = int(row['target_id'])
    raise RuntimeError(f'Too many chained {kind} merges for ID {ident}')


def _choose_target(kind: str, pair: dict) -> tuple[int, int]:
    a, b = pair['left'], pair['right']
    if kind == 'topic':
        # Stable canonical ID: most recent-window usage first, then older ID.
        ka = (int(a.get('article_count') or 0), -int(a['id']))
        kb = (int(b.get('article_count') or 0), -int(b['id']))
        return (int(a['id']), int(b['id'])) if ka >= kb else (int(b['id']), int(a['id']))
    type_a = str(a.get('entity_type') or 'UNKNOWN').upper()
    type_b = str(b.get('entity_type') or 'UNKNOWN').upper()
    weight_a = (type_a != 'UNKNOWN', len(_tokens(str(a.get('name') or ''))), len(str(a.get('name') or '')), int(a.get('article_count') or 0), -int(a['id']))
    weight_b = (type_b != 'UNKNOWN', len(_tokens(str(b.get('name') or ''))), len(str(b.get('name') or '')), int(b.get('article_count') or 0), -int(b['id']))
    return (int(a['id']), int(b['id'])) if weight_a >= weight_b else (int(b['id']), int(a['id']))


def _merge_topic(cur, source_id: int, target_id: int, reason: str) -> bool:
    source_id = _resolve_canonical(cur, 'topic', source_id)
    target_id = _resolve_canonical(cur, 'topic', target_id)
    if source_id == target_id:
        return False
    cur.execute('SELECT topic_id,name,topic_type,status,keywords FROM topics WHERE topic_id=ANY(%s::bigint[]) FOR UPDATE', ([source_id, target_id],))
    rows = {int(r['topic_id']): r for r in cur.fetchall()}
    if len(rows) != 2 or any(rows[x]['status'] != 'active' for x in (source_id, target_id)):
        return False
    source, target = rows[source_id], rows[target_id]
    if source['topic_type'] != 'event' or target['topic_type'] != 'event':
        return False
    source_name = str(source.get('name') or '')
    merged_keywords = list(dict.fromkeys([*(target.get('keywords') or []), *(source.get('keywords') or [])]))
    cur.execute("""INSERT INTO article_topics(article_id,topic_id,confidence,assignment_method,created_at)
                   SELECT article_id,%s,confidence,assignment_method,created_at FROM article_topics WHERE topic_id=%s
                   ON CONFLICT(article_id,topic_id) DO UPDATE SET
                     confidence=GREATEST(article_topics.confidence,excluded.confidence),
                     assignment_method=CASE WHEN article_topics.assignment_method='admin' THEN 'admin' WHEN excluded.assignment_method='admin' THEN 'admin' ELSE article_topics.assignment_method END,
                     created_at=LEAST(article_topics.created_at,excluded.created_at)""", (target_id, source_id))
    cur.execute('DELETE FROM article_topics WHERE topic_id=%s', (source_id,))
    cur.execute("""INSERT INTO topic_aliases(topic_id,alias,normalized_alias)
                   SELECT %s,alias,normalized_alias FROM topic_aliases WHERE topic_id=%s ON CONFLICT DO NOTHING""", (target_id, source_id))
    if source_name:
        cur.execute('INSERT INTO topic_aliases(topic_id,alias,normalized_alias) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                    (target_id, source_name, norm_name(source_name)))
    cur.execute('UPDATE topics SET keywords=%s,updated_at=now() WHERE topic_id=%s', (merged_keywords, target_id))
    cur.execute('UPDATE topics SET parent_topic_id=%s,updated_at=now() WHERE parent_topic_id=%s AND topic_id<>%s', (target_id, source_id, target_id))
    cur.execute("""INSERT INTO user_saved_timelines(user_id,timeline_type,topic_id,entity_id,display_order,created_at)
                   SELECT user_id,'topic',%s,NULL,min(display_order),min(created_at) FROM user_saved_timelines
                   WHERE timeline_type='topic' AND topic_id=%s GROUP BY user_id
                   ON CONFLICT(user_id,topic_id) WHERE timeline_type='topic' DO UPDATE SET
                     display_order=LEAST(user_saved_timelines.display_order,excluded.display_order),
                     created_at=LEAST(user_saved_timelines.created_at,excluded.created_at)""", (target_id, source_id))
    cur.execute("DELETE FROM user_saved_timelines WHERE timeline_type='topic' AND topic_id=%s", (source_id,))
    cur.execute("""INSERT INTO fact_topic_daily(stat_date,topic_id,source_id,article_count,updated_at)
                   SELECT stat_date,%s,source_id,sum(article_count),now() FROM fact_topic_daily WHERE topic_id=%s GROUP BY stat_date,source_id
                   ON CONFLICT(stat_date,topic_id,source_id) DO UPDATE SET article_count=fact_topic_daily.article_count+excluded.article_count,updated_at=now()""", (target_id, source_id))
    cur.execute('DELETE FROM fact_topic_daily WHERE topic_id=%s', (source_id,))
    cur.execute("""INSERT INTO fact_topic_trending(period_type,period_start,topic_id,article_count,source_count,previous_article_count,growth_pct,hotness_score,rank_position,updated_at)
                   SELECT period_type,period_start,%s,article_count,source_count,previous_article_count,growth_pct,hotness_score,rank_position,now()
                   FROM fact_topic_trending WHERE topic_id=%s
                   ON CONFLICT(period_type,period_start,topic_id) DO UPDATE SET
                     article_count=fact_topic_trending.article_count+excluded.article_count,
                     source_count=GREATEST(fact_topic_trending.source_count,excluded.source_count),
                     previous_article_count=fact_topic_trending.previous_article_count+excluded.previous_article_count,
                     growth_pct=CASE WHEN fact_topic_trending.previous_article_count+excluded.previous_article_count>0 THEN round(100.0*((fact_topic_trending.article_count+excluded.article_count)-(fact_topic_trending.previous_article_count+excluded.previous_article_count))/(fact_topic_trending.previous_article_count+excluded.previous_article_count),2) ELSE NULL END,
                     hotness_score=fact_topic_trending.hotness_score+excluded.hotness_score,
                     rank_position=LEAST(fact_topic_trending.rank_position,excluded.rank_position),updated_at=now()""", (target_id, source_id))
    cur.execute('DELETE FROM fact_topic_trending WHERE topic_id=%s', (source_id,))
    cur.execute("""UPDATE news_articles SET timeline_id=article_id WHERE article_id IN
                   (SELECT article_id FROM article_topics WHERE topic_id=%s) AND coalesce(published_at,collected_at)>=now()-interval '48 hours'""", (target_id,))
    cur.execute("UPDATE topics SET status='archived',updated_at=now() WHERE topic_id=%s", (source_id,))
    cur.execute("""INSERT INTO catalog_reconciliation_merges(object_type,source_id,target_id,reason)
                   VALUES('topic',%s,%s,%s) ON CONFLICT(object_type,source_id) DO UPDATE SET target_id=excluded.target_id,merged_at=now(),reason=excluded.reason""", (source_id, target_id, reason[:500]))
    cur.execute("""UPDATE news_articles SET ai_classification=jsonb_set(coalesce(ai_classification,'{}'::jsonb),'{existing_event_topic_id}',to_jsonb(%s::bigint),true)
                   WHERE ai_classification->>'existing_event_topic_id'=%s""", (target_id, str(source_id)))
    return True


def _merge_entity(cur, source_id: int, target_id: int, reason: str) -> bool:
    source_id = _resolve_canonical(cur, 'entity', source_id)
    target_id = _resolve_canonical(cur, 'entity', target_id)
    if source_id == target_id:
        return False
    cur.execute('SELECT entity_id,canonical_name,entity_type,status FROM entities WHERE entity_id=ANY(%s::bigint[]) FOR UPDATE', ([source_id, target_id],))
    rows = {int(r['entity_id']): r for r in cur.fetchall()}
    if len(rows) != 2 or any(rows[x]['status'] != 'active' for x in (source_id, target_id)):
        return False
    source, target = rows[source_id], rows[target_id]
    source_type = str(source.get('entity_type') or 'UNKNOWN').upper()
    target_type = str(target.get('entity_type') or 'UNKNOWN').upper()
    if source_type != target_type and 'UNKNOWN' not in {source_type, target_type}:
        return False
    if target_type == 'UNKNOWN' and source_type != 'UNKNOWN':
        cur.execute('UPDATE entities SET entity_type=%s,updated_at=now() WHERE entity_id=%s', (source_type, target_id))
    source_name = str(source.get('canonical_name') or '')
    cur.execute("""INSERT INTO article_entities(article_id,entity_id,mention_text,confidence,created_at)
                   SELECT article_id,%s,mention_text,max(confidence),min(created_at) FROM article_entities WHERE entity_id=%s
                   GROUP BY article_id,mention_text
                   ON CONFLICT(article_id,entity_id,mention_text) DO UPDATE SET
                     confidence=GREATEST(article_entities.confidence,excluded.confidence),created_at=LEAST(article_entities.created_at,excluded.created_at)""", (target_id, source_id))
    cur.execute('DELETE FROM article_entities WHERE entity_id=%s', (source_id,))
    cur.execute("""INSERT INTO entity_aliases(entity_id,alias,normalized_alias)
                   SELECT %s,alias,normalized_alias FROM entity_aliases WHERE entity_id=%s ON CONFLICT DO NOTHING""", (target_id, source_id))
    if source_name:
        cur.execute('INSERT INTO entity_aliases(entity_id,alias,normalized_alias) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (target_id, source_name, norm_name(source_name)))
    cur.execute("""INSERT INTO user_saved_timelines(user_id,timeline_type,topic_id,entity_id,display_order,created_at)
                   SELECT user_id,'entity',NULL,%s,min(display_order),min(created_at) FROM user_saved_timelines
                   WHERE timeline_type='entity' AND entity_id=%s GROUP BY user_id
                   ON CONFLICT(user_id,entity_id) WHERE timeline_type='entity' DO UPDATE SET
                     display_order=LEAST(user_saved_timelines.display_order,excluded.display_order),
                     created_at=LEAST(user_saved_timelines.created_at,excluded.created_at)""", (target_id, source_id))
    cur.execute("DELETE FROM user_saved_timelines WHERE timeline_type='entity' AND entity_id=%s", (source_id,))
    cur.execute("""INSERT INTO fact_entity_daily(stat_date,entity_id,source_id,article_count,updated_at)
                   SELECT stat_date,%s,source_id,sum(article_count),now() FROM fact_entity_daily WHERE entity_id=%s GROUP BY stat_date,source_id
                   ON CONFLICT(stat_date,entity_id,source_id) DO UPDATE SET article_count=fact_entity_daily.article_count+excluded.article_count,updated_at=now()""", (target_id, source_id))
    cur.execute('DELETE FROM fact_entity_daily WHERE entity_id=%s', (source_id,))
    cur.execute("""UPDATE news_articles SET timeline_id=article_id WHERE article_id IN
                   (SELECT article_id FROM article_entities WHERE entity_id=%s) AND coalesce(published_at,collected_at)>=now()-interval '48 hours'""", (target_id,))
    cur.execute("UPDATE entities SET status='archived',updated_at=now() WHERE entity_id=%s", (source_id,))
    cur.execute("""INSERT INTO catalog_reconciliation_merges(object_type,source_id,target_id,reason)
                   VALUES('entity',%s,%s,%s) ON CONFLICT(object_type,source_id) DO UPDATE SET target_id=excluded.target_id,merged_at=now(),reason=excluded.reason""", (source_id, target_id, reason[:500]))
    return True


def _record_decision(pair: dict, model_decision: dict, stats: dict) -> None:
    expected_key = f"{pair['left_id']}:{pair['right_id']}"
    if str(model_decision.get('pair_key') or '') != expected_key:
        return
    raw_same = bool(model_decision.get('same_object', model_decision.get('same', False)))
    try:
        confidence = max(0.0, min(1.0, float(model_decision.get('confidence', 0) or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(model_decision.get('reason') or model_decision.get('rationale') or '')[:500]
    approved = raw_same and confidence >= MERGE_CONFIDENCE
    target_id = source_id = None
    if approved:
        target_id, source_id = _choose_target(pair['object_type'], pair)
    with get_conn() as conn, conn.cursor() as cur:
        merged = False
        if approved:
            merged = (_merge_topic(cur, source_id, target_id, reason) if pair['object_type'] == 'topic'
                      else _merge_entity(cur, source_id, target_id, reason))
        # Store true only if the merge was safely applied. Low-confidence and incompatible cases
        # are cached as negative decisions so they do not consume tokens every schedule.
        cur.execute("""INSERT INTO catalog_reconciliation_decisions(object_type,left_id,right_id,input_fingerprint,same_object,confidence,reason,decided_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,now())
                       ON CONFLICT(object_type,left_id,right_id) DO UPDATE SET
                         input_fingerprint=excluded.input_fingerprint,same_object=excluded.same_object,
                         confidence=excluded.confidence,reason=excluded.reason,decided_at=now()""",
                    (pair['object_type'], pair['left_id'], pair['right_id'], pair['fingerprint'], merged, confidence,
                     reason or ('below confidence threshold or unsafe merge' if raw_same else 'not equivalent')))
        if merged:
            stats['topic_merges' if pair['object_type'] == 'topic' else 'entity_merges'] += 1
    stats['topic_pairs_checked' if pair['object_type'] == 'topic' else 'entity_pairs_checked'] += 1


def _process_kind(kind: str, items: list[dict], stats: dict) -> None:
    pairs = candidate_pairs(kind, items)
    known = known_decisions(pairs)
    pending = [p for p in pairs if known.get((kind, p['left_id'], p['right_id'])) != p['fingerprint']]
    pending = pending[:MAX_PAIRS_PER_TYPE]
    if not pending:
        log.info('Reconciliation %s: %s candidate pairs, no unreviewed pairs.', kind, len(pairs))
        return
    for offset in range(0, len(pending), PAIRS_PER_REQUEST):
        batch = pending[offset:offset + PAIRS_PER_REQUEST]
        stats['llm_calls'] += 1
        expected = {f"{p['left_id']}:{p['right_id']}": p for p in batch}
        returned = call_openai_pairs(kind, batch)
        seen = set()
        for decision in returned:
            key = str(decision.get('pair_key') or '')
            if key not in expected or key in seen:
                continue
            seen.add(key)
            _record_decision(expected[key], decision, stats)
        missing = set(expected) - seen
        if missing:
            # Missing decisions are not cached; the next run can retry these pairs.
            raise RuntimeError(f'GenAI omitted {len(missing)} {kind} candidate decision(s)')
        log.info('Reconciliation %s batch: %s pairs reviewed.', kind, len(batch))


def run_reconciliation() -> dict:
    ensure_schema()
    stats = {'status': 'running', 'window_hours': WINDOW_HOURS, 'topics_seen': 0, 'entities_seen': 0,
             'topic_pairs_checked': 0, 'entity_pairs_checked': 0, 'topic_merges': 0, 'entity_merges': 0,
             'llm_calls': 0, 'message': ''}
    if not settings.openai_api_key:
        raise RuntimeError('OPENAI_API_KEY is not configured for catalog reconciliation')
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO catalog_reconciliation_runs(status,window_hours) VALUES('running',%s) RETURNING run_id", (WINDOW_HOURS,))
        run_id = int(cur.fetchone()['run_id'])
    try:
        topics = load_recent_topics()
        entities = load_recent_entities()
        stats['topics_seen'], stats['entities_seen'] = len(topics), len(entities)
        log.info('Reconciliation run %s started: window=%sh topics=%s entities=%s', run_id, WINDOW_HOURS, len(topics), len(entities))
        _process_kind('topic', topics, stats)
        _process_kind('entity', entities, stats)
        stats['status'] = 'success'
        stats['message'] = 'Catalog reconciliation finished.'
        return stats
    except Exception as exc:
        stats['status'] = 'failed'
        stats['message'] = f'{type(exc).__name__}: {str(exc)[:600]}'
        raise
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE catalog_reconciliation_runs SET finished_at=now(),status=%s,
                         topics_seen=%s,entities_seen=%s,topic_pairs_checked=%s,entity_pairs_checked=%s,
                         topic_merges=%s,entity_merges=%s,llm_calls=%s,message=%s,details=%s::jsonb WHERE run_id=%s""",
                        (stats['status'], stats['topics_seen'], stats['entities_seen'], stats['topic_pairs_checked'], stats['entity_pairs_checked'],
                         stats['topic_merges'], stats['entity_merges'], stats['llm_calls'], stats['message'], json.dumps(stats, ensure_ascii=False), run_id))
        log.info('Reconciliation run %s finished: %s', run_id, stats)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s | %(message)s')
    if not settings.database_url:
        log.error('DATABASE_URL is not configured.')
        return 2
    try:
        # Session advisory lock ensures a manual run and scheduled run cannot reconcile concurrently.
        with psycopg.connect(settings.database_url, connect_timeout=12, row_factory=dict_row, autocommit=True) as lock_conn:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext('mini-lakehouse-catalog-reconciliation-v1')) AS locked")
                locked = bool(cur.fetchone()['locked'])
            if not locked:
                log.info('Another reconciliation run is active; this run is skipped.')
                return 0
            try:
                result = run_reconciliation()
                log.info('Catalog reconciliation summary: %s', result)
                return 0
            finally:
                with lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('mini-lakehouse-catalog-reconciliation-v1'))")
    except Exception as exc:
        log.exception('Catalog reconciliation failed: %s', str(exc)[:500])
        return 1


if __name__ == '__main__':
    sys.exit(main())
