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
from .entity_rules import should_track_entity

log = logging.getLogger('mini-lakehouse.openai')
OPENAI_CHAT_COMPLETIONS_URL = 'https://api.openai.com/v1/chat/completions'
ALLOWED_ENTITY_TYPES = {'PERSON', 'ORG', 'LOCATION', 'PRODUCT', 'EVENT', 'GROUP', 'UNKNOWN'}

_RECORD_CLASSIFICATION_FAILURE_SQL = """update news_articles
    set ai_classification_error=%s,
        ai_classification=jsonb_set(
            coalesce(ai_classification, '{}'::jsonb),
            '{ai_retry_count}',
            to_jsonb(case
                when coalesce(ai_classification->>'ai_retry_count','') ~ '^[0-9]+$'
                    then (ai_classification->>'ai_retry_count')::int + 1
                else 1
            end), true),
        updated_at=now()
    where article_id=%s"""


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
2) Với mỗi bài, đặt event_topic_name là tên ngắn, cụ thể cho sự kiện/hành động đang xảy ra, nêu chủ thể + hành động + đối tượng/địa điểm khi có thể. Không dùng tên quá rộng kiểu 'Chính trị', 'Quốc phòng', 'Tình hình Iran'. Với tin nền, danh sách, lịch, dự báo hoặc không đủ bằng chứng thì event_topic_name=null.
3) existing_event_topic_id chỉ được dùng khi có bằng chứng rõ rằng bài nói về CÙNG VỤ VIỆC CỤ THỂ, cùng hành động/sự việc chính, với topic hiện có. So sánh event name và sample_titles. Cùng danh mục, cùng ngành, cùng loại tội phạm, cùng quốc gia, cùng tổ chức hay cùng cụm từ KHÔNG có nghĩa là cùng sự kiện. Một vụ án về ma túy, vụ lừa đảo ngân hàng và vụ chuyển tiền trái phép ra nước ngoài là ba sự kiện khác nhau kể cả khi cùng xuất hiện trong mục pháp luật.
4) Khi so khớp, phải so sánh cả hành động chính, đối tượng/mục tiêu, người/tổ chức liên quan, địa điểm và dấu hiệu định danh vụ việc. Nếu hành động chính khác nhau (ví dụ lừa đảo vs tuồn/chuyển tiền, đe dọa tấn công vs tấn công thực tế, đàm phán vs không kích), hoặc là các vụ án/đối tượng/số tiền khác nhau, KHÔNG chọn existing_event_topic_id. Khi không chắc, tạo event_topic_name riêng hoặc null; ưu tiên tách nhầm hơn gộp nhầm.
5) Luôn trả event_topic_name đề xuất cho bài nếu bài có thể theo dõi như một sự kiện, kể cả khi đề xuất dùng existing_event_topic_id. Nếu chọn sự kiện có sẵn, event_topic_name phải lặp lại chính xác tên chuẩn của sự kiện đó. Điều này giúp hệ thống kiểm chứng kết quả trước khi nối bài.
6) Đọc các bài trong batch. Chỉ gộp hai bài khi chúng nói về cùng một vụ việc cụ thể, không chỉ có từ khóa/entity trùng nhau. Không gộp hai vụ án riêng biệt, hai động thái chính sách riêng biệt hay hai diễn biến quân sự khác nhau.
7) Chỉ trích xuất entity là đối tượng CỤ THỂ có danh tính riêng, thực sự xuất hiện trong tiêu đề/mô tả; không tạo entity suy đoán. entity_type thuộc PERSON, ORG, LOCATION, PRODUCT, EVENT, GROUP, UNKNOWN. Chỉ tối đa 10 entity/bài; aliases phải là biến thể trực tiếp được thể hiện trong văn bản.
8) KHÔNG tạo entity cho tên quốc gia, tỉnh/thành phố hoặc vùng hành chính lớn, ví dụ Việt Nam, Mỹ, Trung Quốc, Hà Nội, Đà Nẵng, TP.HCM, California, Tokyo. Những địa danh này chỉ là bối cảnh, không phải entity followable của Radar. Cũng không tạo entity cho danh từ chung như "siêu thị", "người dân", "bệnh viện", "chùa" nếu không có tên riêng.
9) ĐỊA ĐIỂM CỤ THỂ có tên riêng vẫn là entity type LOCATION, ví dụ Chùa Hương, Chùa Bái Đính, Vịnh Hạ Long, Sân vận động Mỹ Đình, Bệnh viện Bạch Mai, Nhà thờ Đức Bà. Phân biệt địa điểm có tên riêng với tỉnh/thành phố chứa địa điểm đó.
10) Sự kiện cụ thể/giải đấu/vụ án có danh tính riêng (ví dụ World Cup 2026, một vụ án cụ thể) có thể là entity type EVENT; không nhầm nó với danh từ chung hay tên quốc gia.
11) confidence là số 0..1. Nếu không chắc sự kiện, để null thay vì đoán. Không bịa diễn biến, ngày tháng hoặc quan hệ.

Định dạng JSON bắt buộc:
{{"results":[{{"article_id":123,"category_topic_id":1,"existing_event_topic_id":null,"event_topic_name":"OpenAI ra mắt mô hình AI","event_confidence":0.91,"entities":[{{"name":"OpenAI","type":"ORG","aliases":[]}}]}}]}}

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
    if not should_track_entity(name, entity_type):
        log.debug('Skipping non-followable entity name=%r type=%s', name, entity_type)
        return None
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


# High-specificity action families prevent unrelated stories sharing a generic category
# or a broad entity (for example, several different bank crimes) from being merged.
_EVENT_ACTION_FAMILIES = {
    'drug_crime': (r'\bma t[uú]y\b', r'heroin', r'cocaine', r'fentanyl', r'buôn bán chất cấm', r'tàng trữ ma túy'),
    'fraud': (r'lừa đảo', r'chiếm đoạt', r'gian lận', r'giả mạo', r'fraud', r'scam'),
    'illegal_money_transfer': (r'tuồn.{0,70}(tiền|tỷ|tỉ|đồng|ra nước ngoài)', r'chuyển.{0,50}(tiền|tỷ|tỉ|đồng).{0,40}ra nước ngoài', r'chuyển.{0,35}tiền.{0,35}trái phép', r'vận chuyển trái phép tiền tệ', r'rửa tiền', r'money laundering'),
    'embezzlement': (r'tham ô', r'biển thủ', r'chiếm dụng', r'embezzl'),
    'bribery_corruption': (r'hối lộ', r'tham nhũng', r'nhận tiền chạy án', r'bribery', r'corruption'),
    'military_attack': (r'không kích', r'oanh tạc', r'tập kích', r'tấn công', r'đánh bom', r'air.?strike', r'bomb(?:ed|ing)?', r'missile strike'),
    'military_exercise': (r'tập trận', r'diễn tập quân sự', r'military exercise', r'drill'),
    'threat': (r'đe dọa', r'đe doạ', r'dọa tấn công', r'threaten', r'threatens', r'threatened'),
    'negotiation': (r'đàm phán', r'thương lượng', r'negotiat', r'peace talks'),
    'sanctions': (r'trừng phạt', r'cấm vận', r'sanction'),
    'election': (r'bầu cử', r'ứng cử', r'election', r'vote'),
    'product_launch': (r'ra mắt', r'tung ra', r'giới thiệu sản phẩm', r'launches', r'unveils'),
}
_EVENT_GENERIC_WORDS = set("""
    và là của có cho với trong trên dưới tại từ đến khi những các một này đó được đang sẽ đã
    người ngày năm tháng theo về việc không cũng nhưng nếu thì mà để bởi vào ra như hơn nhất
    vụ án bị cáo cựu nhân viên ngân hàng công ty doanh nghiệp báo tin mới nhất hôm nay
    hôm qua sáng chiều tối lúc theo nguồn cho biết tuyên bố nói nói rằng
    the a an and or but for with from after before over under this that those these
    said says report reports news new latest today yesterday people person group company bank
    employee former allegedly according to about amid after before as of
    tỉ tỷ đồng triệu nghìn
""".split())
_EVENT_ACTION_WORDS = set("""
    lừa đảo chiếm đoạt gian lận giả mạo ma túy tuy heroin cocaine fentanyl
    tuồn chuyển tiền trái phép rửa laundering tham ô biển thủ chiếm dụng hối lộ tham nhũng
    không kích oanh tạc tập trận diễn tập tấn công đánh bom strike attack bomb
    đe dọa doạ dọa threaten threatened đàm phán thương lượng negotiation talks
    trừng phạt cấm vận sanctions sanction bầu cử ứng cử election vote ra mắt tung giới thiệu
    khởi tố truy tố bắt giữ xét xử tuyên án
""".split())


def _event_families(text: str) -> set[str]:
    text = normalize_space(text or '').casefold()
    return {family for family, patterns in _EVENT_ACTION_FAMILIES.items()
            if any(re.search(pattern, text, flags=re.I) for pattern in patterns)}


def _event_tokens(text: str) -> set[str]:
    text = norm_name(text or '')
    tokens = set(re.findall(r'[^\W_]+', text, flags=re.UNICODE))
    return {t for t in tokens if ((len(t) >= 3 and t not in _EVENT_GENERIC_WORDS) or (t.isdigit() and len(t) >= 2))}


def _event_titles_compatible(article_title: str, event_title: str) -> bool:
    """Conservative guard: reject clear action-family conflicts and weak title overlaps."""
    article_title = normalize_space(article_title)
    event_title = normalize_space(event_title)
    if not article_title or not event_title:
        return False
    if norm_name(article_title) == norm_name(event_title):
        return True
    article_families = _event_families(article_title)
    event_families = _event_families(event_title)
    if article_families and event_families and article_families.isdisjoint(event_families):
        return False
    a_tokens, e_tokens = _event_tokens(article_title), _event_tokens(event_title)
    # Keep identifying entities, places, case markers and numbers; don't count generic action
    # verbs or category words as proof that two stories describe the same incident.
    a_specific = a_tokens - _EVENT_ACTION_WORDS
    e_specific = e_tokens - _EVENT_ACTION_WORDS
    overlap = a_specific & e_specific
    union = a_specific | e_specific
    jaccard = len(overlap) / max(1, len(union))
    shared_action = bool(article_families & event_families)
    shared_number = any(token.isdigit() for token in overlap)
    # Same action family still needs at least one identifying detail in common (person,
    # organization, place, case marker, or amount). Different stories about the same industry
    # or crime type therefore stay separate.
    if shared_action and len(overlap) >= 1 and (jaccard >= 0.18 or shared_number):
        return True
    # Without matching event-action signatures, require substantially stronger identity overlap.
    if len(overlap) >= 3 and jaccard >= 0.30:
        return True
    if len(overlap) >= 2 and jaccard >= 0.45:
        return True
    return False


def _clearly_incompatible_assignment(article_title: str, event_title: str) -> bool:
    """Only auto-repair unmistakable action-family conflicts; uncertainty is not enough."""
    article_families = _event_families(article_title)
    event_families = _event_families(event_title)
    return bool(article_families and event_families and article_families.isdisjoint(event_families))


def _fallback_event_name(title: str) -> str:
    # Preserve a specific factual headline when a model-proposed event ID is rejected.
    return normalize_space(title)[:180]


def repair_obvious_event_mismatches(limit: int = 500) -> int:
    """Detach machine assignments whose explicit event-action family conflicts with topic.

    Manual admin assignments are not touched. Articles are queued for the classifier during
    the same pipeline run, so previously incorrect timelines can be repaired automatically.
    """
    limit = max(1, min(int(limit), 2000))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select a.article_id,a.title,t.topic_id,t.name as event_name
                       from article_topics at
                       join news_articles a on a.article_id=at.article_id
                       join topics t on t.topic_id=at.topic_id
                       where t.topic_type='event' and t.status='active'
                         and at.assignment_method='genai' and a.is_excluded=false
                         and coalesce(a.published_at,a.collected_at)>=now()-interval '30 days'
                         and coalesce(a.ai_classification->>'manual_override','false') <> 'true'
                         and coalesce(a.ai_classification->>'mismatch_repair_attempted','false') <> 'true'
                       order by coalesce(a.published_at,a.collected_at) desc
                       limit %s""", (limit,))
        rows = cur.fetchall()
        mismatches = [r for r in rows if _clearly_incompatible_assignment(r['title'], r['event_name'])]
        for row in mismatches:
            cur.execute("delete from article_topics where article_id=%s and topic_id=%s and assignment_method='genai'",
                        (row['article_id'], row['topic_id']))
            cur.execute("""update news_articles
                           set ai_classified_at=null,
                               event_topic_scanned_at=null,
                               ai_classification = coalesce(ai_classification, '{}'::jsonb)
                                   || jsonb_build_object('mismatch_repair_attempted', true),
                               ai_classification_error='Reprocessing: event action does not match assigned topic',
                               updated_at=now()
                           where article_id=%s""", (row['article_id'],))
    if mismatches:
        log.warning('Detached %s obviously mismatched GenAI event assignment(s); queued for reclassification.', len(mismatches))
    return len(mismatches)


def _apply_classification(article: dict, result: dict, category_ids: set[int], event_topics: dict[int, dict]):
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
    filtered_entities = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_name = entity.get('name') or entity.get('canonical_name')
        entity_type = entity.get('type') or entity.get('entity_type') or 'UNKNOWN'
        if entity_name and should_track_entity(str(entity_name), str(entity_type)):
            filtered_entities.append(entity)
    entities = filtered_entities[:10]
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
            if existing_event_id in event_topics:
                candidate = event_topics[existing_event_id]
                # Use the canonical event name for the deterministic gate. Sample titles are
                # prompt context only: allowing any sample to authorize a match could perpetuate
                # an already-corrupted timeline by matching the very wrong article it contains.
                reference_title = candidate.get('name') or ''
                matched = _event_titles_compatible(article.get('title') or '', reference_title)
                if matched:
                    event_topic_id = existing_event_id
                else:
                    canonical_existing = norm_name(candidate.get('name') or '')
                    if not event_name or norm_name(event_name) == canonical_existing:
                        event_name = _fallback_event_name(article.get('title') or '')
                    log.warning('Rejected existing event match: article_id=%s proposed_topic_id=%s topic=%r article=%r',
                                article_id, existing_event_id, candidate.get('name'), article.get('title'))
            if event_topic_id is None and event_name and confidence >= 0.55:
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
                cur.execute("""select
                                   coalesce(ai_classification->>'manual_override','false') as manual_override,
                                   coalesce(ai_classification->>'mismatch_repair_attempted','false') as mismatch_repair_attempted
                               from news_articles where article_id=%s""", (article_id,))
                classification_flags = cur.fetchone()
                manual = classification_flags['manual_override'] == 'true'
                if manual:
                    ai_result['manual_override'] = True
                if classification_flags['mismatch_repair_attempted'] == 'true':
                    ai_result['mismatch_repair_attempted'] = True
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
                ai_result['event_topic_name'] = event_name or None
                ai_result['existing_event_topic_id'] = existing_event_id if event_topic_id == existing_event_id else None
                cur.execute("""update news_articles set ai_classified_at=now(),ai_classification=%s::jsonb,
                               ai_classification_error=null,event_topic_scanned_at=now(),updated_at=now()
                               where article_id=%s""", (json.dumps(ai_result, ensure_ascii=False), article_id))
    return True


def classify_unprocessed_articles(limit: int | None = None, since=None) -> dict[str, int]:
    """Classify recent unprocessed articles and due retries; older backlog is opt-in.

    The rolling 24-hour collection window lets an hourly run finish fresh articles that
    exceeded the per-run limit, without repeatedly draining 30 days of historical backlog.
    Set AI_CLASSIFICATION_BACKFILL_ENABLED=true temporarily for a deliberate backfill.
    """
    stats = {'selected': 0, 'classified': 0, 'events_assigned': 0, 'entities_assigned': 0, 'failed': 0, 'disabled': 0, 'repaired_mismatches': 0}
    if not settings.openai_api_key:
        stats['disabled'] = 1
        log.warning('OPENAI_API_KEY missing; GenAI event normalization skipped. Configure the key in Render and GitHub Actions.')
        return stats
    # Repair unmistakable old action-family mismatches before processing unclassified articles.
    stats['repaired_mismatches'] = repair_obvious_event_mismatches()
    limit = max(1, int(limit or settings.ai_classification_run_limit))
    batch_size = max(1, min(20, int(settings.ai_classification_batch_size)))
    with get_conn() as conn, conn.cursor() as cur:
        # Only work on recent unclassified rows in the normal hourly schedule; the separate
        # backfill flag is required to process older backlog intentionally. The five-minute
        # ordering preference prioritizes items from this run, while the 24-hour window allows
        # the next hourly run to drain any fresh rows that exceeded this run's per-run limit.
        # Failed rows have a cooldown and retry cap; mismatch repairs are allowed through once and carry a
        # JSON marker to prevent repeated repair loops.
        if since is None:
            from datetime import datetime, timezone
            since = datetime.now(timezone.utc)
        cur.execute("""select article_id,title,coalesce(summary,'') as summary,language,published_at,collected_at
                       from news_articles
                       where ai_classified_at is null and is_excluded=false
                         and coalesce(published_at,collected_at) >= now()-interval '30 days'
                         and (
                           (ai_classification_error is null
                             and collected_at >= now()-interval '24 hours')
                           or ai_classification_error like 'Reprocessing:%%'
                           or (ai_classification_error is not null
                               and updated_at <= now() - (%s * interval '1 hour')
                               and (case
                                      when coalesce(ai_classification->>'ai_retry_count','') ~ '^[0-9]+$'
                                        then (ai_classification->>'ai_retry_count')::int
                                      else 0
                                    end) < %s)
                           or (%s = true and ai_classification_error is null)
                         )
                       order by (collected_at >= %s::timestamptz - interval '5 minutes') desc,
                                coalesce(published_at,collected_at) desc
                       limit %s""",
                    (settings.ai_classification_retry_cooldown_hours,
                     settings.ai_classification_max_retries,
                     settings.ai_classification_backfill_enabled, since, limit))
        articles = cur.fetchall()
        cur.execute("""select topic_id,name,description,keywords from topics
                       where status='active' and topic_type='category' order by name limit 80""")
        categories = cur.fetchall()
    stats['selected'] = len(articles)
    if not articles:
        log.info('GenAI skipped: no recent unclassified articles in the rolling 24-hour window, no retry-ready failures, and no enabled historical backfill; no OpenAI request sent.')
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
                                  coalesce(x.article_count,0) as article_count,
                                  coalesce(x.sample_titles,ARRAY[]::text[]) as sample_titles
                           from topics t left join (
                             select at.topic_id,count(*)::int as article_count,
                                    (array_agg(a.title order by coalesce(a.published_at,a.collected_at) desc))[1:4] as sample_titles
                             from article_topics at join news_articles a on a.article_id=at.article_id
                             where coalesce(a.published_at,a.collected_at)>=now()-interval '60 days'
                               and a.is_excluded=false and at.assignment_method <> 'admin'
                             group by at.topic_id
                           ) x on x.topic_id=t.topic_id
                           where t.status='active' and t.topic_type='event'
                           order by x.article_count desc,t.updated_at desc limit 160""")
            batch_existing_events = cur.fetchall()
        batch_event_topics = {int(r['topic_id']): r for r in batch_existing_events}
        try:
            output = _call_openai(prompt_rows, categories, batch_existing_events)
            by_id = {int(x.get('article_id')): x for x in output if x.get('article_id') is not None}
            for article in batch_rows:
                row = by_id.get(int(article['article_id']))
                if row is None:
                    stats['failed'] += 1
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute(_RECORD_CLASSIFICATION_FAILURE_SQL,
                                    ('OpenAI omitted this article from results[]', article['article_id']))
                    continue
                try:
                    ok = _apply_classification(article, row, category_ids, batch_event_topics)
                    if ok:
                        stats['classified'] += 1
                        stats['events_assigned'] += int(bool(row.get('event_topic_name') or row.get('existing_event_topic_id')))
                        stats['entities_assigned'] += len(row.get('entities') or []) if isinstance(row.get('entities'), list) else 0
                except Exception as exc:
                    stats['failed'] += 1
                    log.exception('GenAI assignment failed for article_id=%s', article['article_id'])
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute(_RECORD_CLASSIFICATION_FAILURE_SQL,
                                    (f'{type(exc).__name__}: {str(exc)[:1200]}', article['article_id']))
        except Exception as exc:
            stats['failed'] += len(batch_rows)
            log.exception('GenAI batch failed for article ids %s-%s: %s', batch_rows[0]['article_id'], batch_rows[-1]['article_id'], exc)
            with get_conn() as conn, conn.cursor() as cur:
                cur.executemany(_RECORD_CLASSIFICATION_FAILURE_SQL,
                                [(f'{type(exc).__name__}: {str(exc)[:1200]}', r['article_id']) for r in batch_rows])
    log.info('GenAI event normalization complete: selected=%s classified=%s events=%s entities=%s repaired_mismatches=%s failed=%s elapsed=%.1fs model=%s',
             stats['selected'], stats['classified'], stats['events_assigned'], stats['entities_assigned'], stats['repaired_mismatches'], stats['failed'], time.monotonic()-started, settings.openai_model)
    return stats
