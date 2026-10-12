"""GenAI-based automatic event-topic normalization and entity extraction.

Uses the OpenAI Chat Completions API with JSON output. Classification is batched and idempotent;
new event topics and entities are added automatically, without a moderation queue.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from .config import settings
from .db import get_conn
from .processing import hash_text, norm_name, normalize_space, slugify

log = logging.getLogger('mini-lakehouse.openai')
OPENAI_CHAT_COMPLETIONS_URL = 'https://api.openai.com/v1/chat/completions'
ALLOWED_ENTITY_TYPES = {'PERSON', 'ORG', 'LOCATION', 'PRODUCT', 'EVENT', 'GROUP', 'UNKNOWN'}


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('OpenAI response did not contain a JSON object')
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('OpenAI response must be a JSON object')
    return value


def _build_prompt(articles: list[dict], categories: list[dict], existing_events: list[dict]) -> str:
    return f"""Bạn là bộ chuẩn hóa dữ liệu tin tức cho một news lakehouse tiếng Việt.
Phân tích TỪNG bài riêng biệt, trả về CHỈ JSON hợp lệ, không markdown.

Mục tiêu:
1) Chọn đúng 1 category_topic_id trong danh sách category được cung cấp, hoặc null nếu không phù hợp.
2) Nhận diện event_topic cụ thể nếu tiêu đề/mô tả nói về sự kiện/hành động/diễn biến có thể theo dõi trên timeline. Tên ngắn, tự nhiên bằng tiếng Việt, nêu chủ thể + hành động + đối tượng/địa điểm khi có thể, ví dụ 'Mỹ không kích Iran', 'NATO tập trận tại Italy', 'Bộ Công an khởi tố vụ án lừa đảo'. Không dùng tên quá rộng kiểu 'Chính trị', 'Quốc phòng', 'Tình hình Iran'. Nếu là tin nền chung, lịch thi đấu, dự báo, bài danh sách hoặc không đủ bằng chứng thì event_topic_name=null.
3) So sánh với existing_event_topics. Chỉ chọn existing_event_topic_id khi đó THỰC SỰ là cùng sự kiện/diễn biến đang theo dõi (cùng hành động/sự việc chính), không chỉ cùng quốc gia, nhân vật hay chủ đề. Đe dọa tấn công khác với tấn công thật; phủ nhận khác với xác nhận; một cuộc đàm phán khác một cuộc không kích. Nếu cùng sự kiện thì chọn id hiện có và event_topic_name có thể null. Nếu là sự kiện khác, trả event_topic_name mới.
4) Đọc toàn bộ các bài trong cùng batch để bảo đảm các bài nói về CHÍNH CÙNG một sự kiện mới có cùng event_topic_name chuẩn hóa. Không tạo nhiều tên gần giống cho cùng sự kiện; ngược lại không gộp các diễn biến khác nhau chỉ vì cùng nhân vật/quốc gia.
5) Trích xuất entity thực sự có trong tiêu đề/mô tả. Tên chuẩn, không tạo entity suy đoán. entity_type thuộc PERSON, ORG, LOCATION, PRODUCT, EVENT, GROUP, UNKNOWN. Chỉ tối đa 10 entity/bài; aliases là danh sách biến thể trực tiếp được thể hiện trong văn bản, nếu có.
6) confidence là số 0..1. Nếu không chắc sự kiện, để null thay vì đoán.
7) Không bịa diễn biến, ngày tháng hoặc quan hệ không có trong văn bản. Chỉ dùng title/summary dưới đây.

Định dạng JSON bắt buộc:
{{"results":[{{"article_id":123,"category_topic_id":1,"existing_event_topic_id":null,"event_topic_name":"Mỹ không kích Iran","event_confidence":0.91,"entities":[{{"name":"Mỹ","type":"LOCATION","aliases":["Hoa Kỳ"]}}]}}]}}

Categories có thể chọn (chỉ dùng id có trong danh sách):
{json.dumps(categories, ensure_ascii=False)}

Sự kiện đã tồn tại để tránh tạo topic trùng (chỉ chọn ID nếu cùng một sự kiện):
{json.dumps(existing_events, ensure_ascii=False)}

Các bài cần phân tích:
{json.dumps(articles, ensure_ascii=False)}
"""


def _call_openai(batch: list[dict], categories: list[dict], existing_events: list[dict]) -> list[dict]:
    """Call OpenAI Chat Completions and require a JSON-object response."""
    if not settings.openai_api_key:
        raise RuntimeError('OPENAI_API_KEY is not configured')
    model = settings.openai_model.strip() or 'gpt-4.1-mini'
    payload = {
        'model': model,
        'messages': [
            {
                'role': 'system',
                'content': 'Return only a valid JSON object. Do not invent facts. Follow the schema and constraints in the user prompt.',
            },
            {'role': 'user', 'content': _build_prompt(batch, categories, existing_events)},
        ],
        'response_format': {'type': 'json_object'},
        'temperature': 0.1,
        'max_completion_tokens': 4096,
    }
    response = requests.post(
        OPENAI_CHAT_COMPLETIONS_URL,
        headers={
            'Authorization': f'Bearer {settings.openai_api_key}',
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=(10, max(10, settings.ai_request_timeout_seconds)),
    )
    if not response.ok:
        # Do not log the API key; only include a short provider response for troubleshooting.
        raise RuntimeError(f'OpenAI HTTP {response.status_code}: {response.text[:350]}')
    body = response.json()
    try:
        text = body['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError('OpenAI returned no message content') from exc
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError('OpenAI returned empty message content')
    parsed = _extract_json(text)
    results = parsed.get('results')
    if not isinstance(results, list):
        raise ValueError('OpenAI JSON is missing results[]')
    return [x for x in results if isinstance(x, dict)]


def _upsert_event_topic(conn, name: str, confidence: float, category_topic_id: int | None) -> int:
    name = normalize_space(name)[:180]
    normalized = norm_name(name)
    if len(normalized) < 5:
        raise ValueError('Event topic name is too short')
    event_key = 'genai:' + hash_text(normalized)[:32]
    base_slug = 'event-' + slugify(name)
    slug = base_slug[:180]
    with conn.cursor() as cur:
        cur.execute('select topic_id from topics where event_key=%s or slug=%s limit 1', (event_key, slug))
        row = cur.fetchone()
        if row:
            return row['topic_id']
        cur.execute('select topic_id from topics where topic_type=\'event\' and lower(name)=lower(%s) limit 1', (name,))
        row = cur.fetchone()
        if row:
            return row['topic_id']
        cur.execute('select topic_id from topics where slug=%s limit 1', (slug,))
        collision = cur.fetchone()
        if collision:
            slug = f'{base_slug[:160]}-{hash_text(normalized)[:8]}'
        cur.execute("""insert into topics(slug,event_key,name,description,keywords,parent_topic_id,topic_type,status,origin)
                       values(%s,%s,%s,%s,%s,%s,'event','active','discovered')
                       on conflict(event_key) do update set updated_at=now(),name=excluded.name
                       returning topic_id""",
                    (slug, event_key, name, 'Tự động chuẩn hóa từ bài báo bằng GenAI.', [name], category_topic_id))
        return cur.fetchone()['topic_id']


def _upsert_entity(conn, name: str, entity_type: str, aliases: list[str] | None = None) -> int | None:
    """Resolve canonical names and known aliases before creating an entity."""
    name = normalize_space(name)[:180]
    normalized = norm_name(name)
    if len(normalized) < 2 or len(normalized) > 180:
        return None
    entity_type = str(entity_type or 'UNKNOWN').upper().strip()
    if entity_type not in ALLOWED_ENTITY_TYPES:
        entity_type = 'UNKNOWN'
    aliases = aliases or []
    entity_id = None
    with conn.cursor() as cur:
        cur.execute("""select entity_id from entities
                       where normalized_name=%s
                       order by case when status='active' then 0 else 1 end, entity_id limit 1""", (normalized,))
        row = cur.fetchone()
        if row:
            entity_id = row['entity_id']
        else:
            cur.execute("""select e.entity_id from entity_aliases a
                           join entities e on e.entity_id=a.entity_id
                           where a.normalized_alias=%s
                           order by case when e.status='active' then 0 else 1 end, e.entity_id limit 1""", (normalized,))
            row = cur.fetchone()
            if row:
                entity_id = row['entity_id']
        if entity_id is None:
            cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,description,origin)
                           values(%s,%s,%s,'Tự động trích xuất và chuẩn hóa từ tin tức bằng GenAI.','discovered')
                           on conflict(normalized_name,entity_type) do update set updated_at=now()
                           returning entity_id""", (name, normalized, entity_type))
            entity_id = cur.fetchone()['entity_id']
        all_aliases = [name] + aliases
        for alias in all_aliases[:12]:
            alias = normalize_space(str(alias))[:180]
            alias_norm = norm_name(alias)
            if not alias_norm:
                continue
            cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                           values(%s,%s,%s) on conflict do nothing""", (entity_id, alias, alias_norm))
    return entity_id


def _apply_classification(article: dict, result: dict, category_ids: set[int], event_ids: set[int]):
    article_id = int(article['article_id'])
    try:
        category_id = result.get('category_topic_id')
        category_id = int(category_id) if category_id is not None else None
    except (TypeError, ValueError):
        category_id = None
    try:
        existing_event_id = result.get('existing_event_topic_id')
        existing_event_id = int(existing_event_id) if existing_event_id is not None else None
    except (TypeError, ValueError):
        existing_event_id = None
    event_name = normalize_space(str(result.get('event_topic_name') or ''))[:180]
    try:
        confidence = float(result.get('event_confidence', 0.75) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    entities = result.get('entities') if isinstance(result.get('entities'), list) else []
    ai_result = {
        'category_topic_id': category_id,
        'existing_event_topic_id': existing_event_id,
        'event_topic_name': event_name or None,
        'event_confidence': confidence,
        'entities': entities[:10],
    }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('select article_id from news_articles where article_id=%s and is_excluded=false', (article_id,))
            if not cur.fetchone():
                return False
            # AI returns one primary category. Replace prior machine category assignments,
            # while respecting manual links edited by the admin.
            cur.execute("""delete from article_topics at using topics t
                           where at.topic_id=t.topic_id and at.article_id=%s
                             and t.topic_type='category' and at.assignment_method <> 'admin'""", (article_id,))
            if category_id in category_ids:
                cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                               values(%s,%s,%s,'genai') on conflict(article_id,topic_id) do update
                               set confidence=greatest(article_topics.confidence,excluded.confidence),assignment_method='genai'""",
                            (article_id, category_id, max(0.55, confidence)))
            event_topic_id = None
            if existing_event_id in event_ids:
                event_topic_id = existing_event_id
            elif event_name and confidence >= 0.55:
                parent_id = category_id if category_id in category_ids else None
                event_topic_id = _upsert_event_topic(conn, event_name, confidence, parent_id)
            # Replace machine-generated event assignments so one article is not attached to
            # several conflicting event topics. Preserve admin-maintained article associations.
            with conn.cursor() as cur:
                cur.execute("""delete from article_topics at using topics t
                               where at.topic_id=t.topic_id and at.article_id=%s
                                 and t.topic_type='event' and at.assignment_method <> 'admin'""", (article_id,))
                if event_topic_id is not None:
                    cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                                   values(%s,%s,%s,'genai') on conflict(article_id,topic_id) do update
                                   set confidence=excluded.confidence,assignment_method='genai'""",
                                (article_id, event_topic_id, max(0.55, confidence)))
                # GenAI is the canonical entity extractor; clear old machine-generated NER links
                # before writing its normalized list. Manual overrides are preserved by their marker.
                cur.execute("""select coalesce(ai_classification->>'manual_override','false') as manual_override
                               from news_articles where article_id=%s""", (article_id,))
                manual = cur.fetchone()['manual_override'] == 'true'
                if not manual:
                    cur.execute('delete from article_entities where article_id=%s', (article_id,))
                for entity in entities[:10]:
                    if not isinstance(entity, dict):
                        continue
                    entity_name = entity.get('name') or entity.get('canonical_name')
                    if not entity_name:
                        continue
                    entity_type = entity.get('type') or entity.get('entity_type') or 'UNKNOWN'
                    aliases = entity.get('aliases') if isinstance(entity.get('aliases'), list) else []
                    entity_id = _upsert_entity(conn, str(entity_name), str(entity_type), aliases)
                    if entity_id and not manual:
                        cur.execute("""insert into article_entities(article_id,entity_id,mention_text,confidence)
                                       values(%s,%s,%s,%s) on conflict do nothing""",
                                    (article_id, entity_id, normalize_space(str(entity_name))[:180], max(0.55, confidence)))
                cur.execute("""update news_articles set ai_classified_at=now(),ai_classification=%s::jsonb,
                               ai_classification_error=null,event_topic_scanned_at=now(),updated_at=now()
                               where article_id=%s""", (json.dumps(ai_result, ensure_ascii=False), article_id))
    return True


def classify_unprocessed_articles(limit: int | None = None) -> dict[str, int]:
    """Classify recent unprocessed articles in batches; unreviewed entities/topics become active automatically."""
    stats = {'selected': 0, 'classified': 0, 'events_assigned': 0, 'entities_assigned': 0, 'failed': 0, 'disabled': 0}
    if not settings.openai_api_key:
        stats['disabled'] = 1
        log.warning('OPENAI_API_KEY missing; GenAI event normalization skipped. Configure the key in Render and GitHub Actions.')
        return stats
    limit = max(1, int(limit or settings.ai_classification_run_limit))
    batch_size = max(1, min(20, int(settings.ai_classification_batch_size)))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select article_id,title,coalesce(summary,'') as summary,language,published_at,collected_at
                       from news_articles
                       where ai_classified_at is null and is_excluded=false
                         and coalesce(published_at,collected_at) >= now()-interval '30 days'
                       order by (coalesce(published_at,collected_at) >= now()-interval '24 hours') desc,
                                coalesce(published_at,collected_at) desc
                       limit %s""", (limit,))
        articles = cur.fetchall()
        cur.execute("""select topic_id,name,description,keywords from topics
                       where status='active' and topic_type='category' order by name limit 80""")
        categories = cur.fetchall()
    stats['selected'] = len(articles)
    if not articles:
        log.info('GenAI event normalization: no unclassified articles in the 30-day window.')
        return stats
    category_ids = {int(r['topic_id']) for r in categories}
    started = time.monotonic()
    for start in range(0, len(articles), batch_size):
        batch_rows = articles[start:start + batch_size]
        prompt_rows = [{'article_id': int(a['article_id']), 'title': a['title'][:600],
                        'summary': (a['summary'] or '')[:1200], 'language': a.get('language') or 'vi'} for a in batch_rows]
        # Refresh active event topics between batches so a topic created in one batch
        # can be reused by the next batch instead of producing near-duplicate labels.
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""select t.topic_id,t.name,t.description,t.keywords,
                                  coalesce(x.article_count,0) as article_count
                           from topics t left join (
                             select at.topic_id,count(*)::int as article_count
                             from article_topics at join news_articles a on a.article_id=at.article_id
                             where coalesce(a.published_at,a.collected_at)>=now()-interval '60 days' and a.is_excluded=false
                             group by at.topic_id
                           ) x on x.topic_id=t.topic_id
                           where t.status='active' and t.topic_type='event'
                           order by x.article_count desc,t.updated_at desc limit 160""")
            batch_existing_events = cur.fetchall()
        batch_event_ids = {int(r['topic_id']) for r in batch_existing_events}
        try:
            output = _call_openai(prompt_rows, categories, batch_existing_events)
            by_id = {int(x.get('article_id')): x for x in output if x.get('article_id') is not None}
            for article in batch_rows:
                row = by_id.get(int(article['article_id']))
                if row is None:
                    stats['failed'] += 1
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute("update news_articles set ai_classification_error=%s where article_id=%s",
                                    ('OpenAI omitted this article from results[]', article['article_id']))
                    continue
                try:
                    ok = _apply_classification(article, row, category_ids, batch_event_ids)
                    if ok:
                        stats['classified'] += 1
                        stats['events_assigned'] += int(bool(row.get('event_topic_name') or row.get('existing_event_topic_id')))
                        stats['entities_assigned'] += len(row.get('entities') or []) if isinstance(row.get('entities'), list) else 0
                except Exception as exc:
                    stats['failed'] += 1
                    log.exception('GenAI assignment failed for article_id=%s', article['article_id'])
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute('update news_articles set ai_classification_error=%s where article_id=%s',
                                    (f'{type(exc).__name__}: {str(exc)[:1200]}', article['article_id']))
        except Exception as exc:
            stats['failed'] += len(batch_rows)
            log.exception('GenAI batch failed for article ids %s-%s: %s', batch_rows[0]['article_id'], batch_rows[-1]['article_id'], exc)
            with get_conn() as conn, conn.cursor() as cur:
                cur.executemany('update news_articles set ai_classification_error=%s where article_id=%s',
                                [(f'{type(exc).__name__}: {str(exc)[:1200]}', r['article_id']) for r in batch_rows])
    log.info('GenAI event normalization complete: selected=%s classified=%s events=%s entities=%s failed=%s elapsed=%.1fs model=%s',
             stats['selected'], stats['classified'], stats['events_assigned'], stats['entities_assigned'], stats['failed'], time.monotonic()-started, settings.openai_model)
    return stats
