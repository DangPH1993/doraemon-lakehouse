"""GenAI-based automatic event-topic normalization and entity extraction.

Uses the OpenAI Chat Completions API with JSON output. Classification is batched and idempotent;
new event topics and entities are added automatically, without a moderation queue.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .config import settings
from .db import get_conn
from .processing import hash_text, norm_name, normalize_space, slugify
from .entity_rules import should_track_entity, is_grounded_entity, is_numeric_or_date_entity
from .named_infrastructure import (
    augment_entities_with_named_infrastructure, extract_named_infrastructure_entities,
)
from .entity_resolution import entity_name_variants, resolve_entity_in_database
from .entity_names import entity_normalized_key, named_head_and_tail
from .field_categories import (
    FIELD_CATEGORIES, classify_field_category, normalize_field_category,
    is_broad_field_topic, requires_event_topic,
)
from .named_people import extract_named_person_entities
from .named_proper_nouns import extract_named_proper_noun_entities

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
1) field_category phải là ĐÚNG MỘT nhãn trong danh sách lĩnh vực cố định ở cuối prompt. Đây là trường phân loại biên tập của bài báo, KHÔNG tạo Topic trong database và KHÔNG dùng tên lĩnh vực làm event_topic_name.
2) event_topic_name là NHÃN TOPIC mô tả khái quát nhất nội dung chính của BÀI BÁO, đủ cụ thể để phân biệt câu chuyện nhưng không phải lĩnh vực biên tập. Không dùng tên quá rộng như "Văn hóa - Giải trí", "Kinh doanh", "Thời sự", "Đời sống", "Văn hóa & du lịch". Luôn trả nhãn topic cho bài; nếu chưa đủ thông tin về sự kiện, dùng một nhãn ngắn dựa trên tiêu đề/mô tả thay vì để null. ĐẶC BIỆT, bài thuộc các lĩnh vực Thời sự, Thế giới, Thể thao, Kinh doanh, Pháp luật hoặc Văn hóa - Giải trí BẮT BUỘC phải có topic cụ thể, không bao giờ để null/rỗng. Không cần cố gộp topic với bài khác: pipeline đối soát riêng sẽ hợp nhất nhãn tương đương sau đó.
3) existing_event_topic_id có thể để null; không cần tìm topic hiện có trong danh mục. Pipeline đối soát định kỳ sẽ hợp nhất topic tương đương về cùng topic_id.
4) Trích xuất TẤT CẢ danh từ riêng khác nhau xuất hiện trong TIÊU ĐỀ và TÓM TẮT: người, tổ chức, thương hiệu, sản phẩm/model, công trình, địa điểm cụ thể, giải đấu/vụ án/sự kiện cụ thể. Không chỉ chọn 1-3 entity nổi bật. Có thể dùng content_excerpt để phân biệt loại/ngữ cảnh, nhưng không thêm entity chỉ xuất hiện ngoài tiêu đề/tóm tắt.
5) Không bỏ tên nằm trong cụm dài: "Hoa hậu Phan Thị Mơ" -> PERSON "Phan Thị Mơ"; "Đường Võ Nguyên Giáp", "Cao tốc Mỹ Thuận - Cần Thơ", "Cầu vượt Võ Văn Kiệt", "Siêu thị Big C" và "Bộ Công an" phải giữ đủ tên loại + tên riêng. Bổ sung các tên sản phẩm/tổ chức như "Hyundai Bayon 2027", "Santa Fe", "Intel", "CXMT", "Deep Blue Aerospace", "Starship", "SpaceX" khi được nhắc trong tiêu đề/tóm tắt.
6) Danh từ chung thiếu tên riêng không phải entity: "một con đường", "siêu thị", "một bệnh viện", "người dân", "người Dao". Không tạo entity cho quốc gia/tỉnh-thành/vùng rộng khi chỉ là bối cảnh. Địa danh cấp hành chính nằm trong tên công trình có tên riêng vẫn là một phần entity đầy đủ.
7) Tên entity chuẩn phải giữ đủ cấu trúc loại + tên nếu văn bản nêu đủ; normalized key theo cấu trúc loại*_<tên>, ví dụ "đường*_võ nguyên giáp", "siêu thị*_big c". Bí danh chỉ là biến thể trực tiếp có căn cứ trong văn bản.
8) entity_type thuộc PERSON, ORG, LOCATION, PRODUCT, EVENT, GROUP, UNKNOWN. Dùng EVENT cho một sự kiện/giải đấu/vụ án cụ thể có danh tính riêng, không dùng EVENT cho lĩnh vực chung.
9) Không cần tìm động từ chính để ghép Timeline. Backend sử dụng entity sự kiện và entity chuẩn theo quy tắc riêng.
10) confidence là số 0..1. Không bịa entity, nhãn, diễn biến hoặc quan hệ. Trả về tối đa 50 entity/bài; nếu bài có hơn 50 thì ưu tiên tất cả các tên riêng đầy đủ trong tiêu đề và tóm tắt theo thứ tự xuất hiện, không chỉ lấy entity nổi tiếng nhất.

Định dạng JSON bắt buộc:
{{"results":[{{"article_id":123,"field_category":"Văn hóa - Giải trí","existing_event_topic_id":null,"event_topic_name":"Phan Thị Mơ chia sẻ về cuộc sống","event_confidence":0.91,"entities":[{{"name":"Phan Thị Mơ","type":"PERSON","aliases":[]}}]}}]}}

Danh sách field_category hợp lệ (chọn đúng một nhãn, không tự tạo nhãn mới):
{json.dumps(list(FIELD_CATEGORIES), ensure_ascii=False)}

Sự kiện đã tồn tại để tránh tạo topic trùng (chỉ chọn ID nếu cùng một sự kiện):
{json.dumps(existing_events, ensure_ascii=False)}

Các bài cần phân tích (xem cả title, summary và content_excerpt để tìm đủ tên công trình/hạ tầng; không chỉ lấy tên người đứng trong tên công trình):
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
        'max_completion_tokens': 8192,
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


def _upsert_event_topic(conn, name: str, confidence: float, category_topic_id: int | None) -> int | None:
    name = normalize_space(name)[:180]
    if is_broad_field_topic(name):
        log.info('Skipped broad field/category label as event topic: %r', name)
        return None
    normalized = norm_name(name)
    if len(normalized) < 5:
        raise ValueError('Event topic name is too short')
    event_key = 'genai:' + hash_text(normalized)[:32]
    base_slug = 'event-' + slugify(name)
    slug = base_slug[:180]
    with conn.cursor() as cur:
        cur.execute("""select topic_id,status from topics
                       where topic_type='event' and (event_key=%s or slug=%s) limit 1""", (event_key, slug))
        row = cur.fetchone()
        if row:
            if row.get('status') != 'active':
                cur.execute("update topics set status='active',updated_at=now() where topic_id=%s", (row['topic_id'],))
            return row['topic_id']
        cur.execute("select topic_id,status from topics where topic_type='event' and lower(name)=lower(%s) limit 1", (name,))
        row = cur.fetchone()
        if row:
            if row.get('status') != 'active':
                cur.execute("update topics set status='active',updated_at=now() where topic_id=%s", (row['topic_id'],))
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
    """Resolve canonical names, titles and unambiguous aliases before creating an entity."""
    name = normalize_space(name)[:180]
    entity_type = str(entity_type or 'UNKNOWN').upper().strip()
    if entity_type not in ALLOWED_ENTITY_TYPES:
        entity_type = 'UNKNOWN'
    canonical_name = strip_title_for_entity(name, entity_type)
    # In this Radar, bare “Trump” in PERSON context means Donald Trump; keep the
    # canonical ID and follow target stable even when the source headline omits the first name.
    if entity_type == 'PERSON' and norm_name(canonical_name) == 'trump':
        canonical_name = 'Donald Trump'
    normalized = entity_normalized_key(canonical_name, entity_type)
    if len(normalized) < 2 or len(normalized) > 180:
        return None
    if not should_track_entity(canonical_name, entity_type):
        log.debug('Skipping non-followable entity name=%r type=%s', name, entity_type)
        return None

    supplied_aliases = [normalize_space(str(x))[:180] for x in (aliases or []) if normalize_space(str(x))]
    resolved_row, ambiguous = resolve_entity_in_database(conn, name, entity_type, supplied_aliases)
    if ambiguous:
        log.warning('Ambiguous entity alias; not auto-merging or creating duplicate name=%r type=%s', name, entity_type)
        return None
    entity_id = int(resolved_row['entity_id']) if resolved_row else None
    # Upgrade a deterministic UNKNOWN recovery in-place when GenAI later recognizes its type.
    # Preserve entity_id and follows; avoid violating the unique (normalized_name, entity_type) key.
    if entity_id is not None and resolved_row and str(resolved_row.get('entity_type') or '').upper() == 'UNKNOWN' and entity_type != 'UNKNOWN':
        typed_key = entity_normalized_key(canonical_name, entity_type)
        with conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM entities WHERE status='active' AND normalized_name=%s
                           AND entity_type=%s AND entity_id<>%s LIMIT 1""", (typed_key, entity_type, entity_id))
            if not cur.fetchone():
                cur.execute("UPDATE entities SET canonical_name=%s,normalized_name=%s,entity_type=%s,updated_at=now() WHERE entity_id=%s",
                            (canonical_name, typed_key, entity_type, entity_id))
                resolved_row['canonical_name'] = canonical_name
                resolved_row['normalized_name'] = typed_key
                resolved_row['entity_type'] = entity_type
                log.info('Typed recovered entity in place: entity_id=%s type=%s name=%r', entity_id, entity_type, canonical_name)
    if entity_id is not None and entity_type == 'PERSON' and resolved_row:
        current_norm = norm_name(str(resolved_row.get('canonical_name') or ''))
        preferred = canonical_name
        preferred_norm = norm_name(preferred)
        # If a short-form-only row was created first (e.g. "Trump"), upgrade its display
        # name when a full person name is later seen, preserving entity_id and follows.
        if ' ' not in current_norm and ' ' in preferred_norm:
            with conn.cursor() as cur:
                preferred_key = entity_normalized_key(preferred, entity_type)
                cur.execute("""SELECT 1 FROM entities WHERE status='active' AND normalized_name=%s
                               AND entity_id<>%s AND entity_type IN ('PERSON','UNKNOWN') LIMIT 1""",
                            (preferred_key, entity_id))
                if not cur.fetchone():
                    cur.execute("UPDATE entities SET canonical_name=%s,normalized_name=%s,updated_at=now() WHERE entity_id=%s",
                                (preferred, preferred_key, entity_id))
                    resolved_row['canonical_name'] = preferred
                    resolved_row['normalized_name'] = preferred_key
    if entity_id is not None and resolved_row and named_head_and_tail(canonical_name):
        old_display = norm_name(str(resolved_row.get('canonical_name') or ''))
        supplied_norms = {norm_name(x) for x in supplied_aliases if norm_name(x)}
        structured_key = entity_normalized_key(canonical_name, entity_type)
        if old_display and old_display in supplied_norms and old_display != norm_name(canonical_name):
            with conn.cursor() as cur:
                cur.execute("""SELECT 1 FROM entities WHERE status='active' AND normalized_name=%s
                               AND entity_type=%s AND entity_id<>%s LIMIT 1""",
                            (structured_key, entity_type, entity_id))
                if not cur.fetchone():
                    cur.execute("UPDATE entities SET canonical_name=%s,normalized_name=%s,updated_at=now() WHERE entity_id=%s",
                                (canonical_name, structured_key, entity_id))
                    resolved_row['canonical_name'] = canonical_name
                    resolved_row['normalized_name'] = structured_key

    with conn.cursor() as cur:
        if entity_id is None:
            # If no candidate was found, create a new canonical entity.
            cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,description,origin)
                           values(%s,%s,%s,'Tự động trích xuất và chuẩn hóa từ tin tức bằng GenAI.','discovered')
                           on conflict(normalized_name,entity_type) do update set updated_at=now()
                           returning entity_id""", (canonical_name, normalized, entity_type))
            entity_id = int(cur.fetchone()['entity_id'])

        if resolved_row and resolved_row.get('_resolution_method') == 'fuzzy':
            # A fuzzy match may be a spelling variation, not a real alias; only add
            # established canonical variants rather than persisting an accidental typo.
            all_aliases = entity_name_variants(canonical_name, entity_type)
        else:
            all_aliases = entity_name_variants(name, entity_type, supplied_aliases)
        for alias in all_aliases[:30]:
            alias_norm = norm_name(alias)
            if not alias_norm:
                continue
            cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                           values(%s,%s,%s) on conflict do nothing""", (entity_id, alias, alias_norm))
    return entity_id


def strip_title_for_entity(name: str, entity_type: str) -> str:
    # Keep the shared helper import in one place while avoiding title stripping for orgs/places.
    if str(entity_type or '').upper().strip() == 'PERSON':
        from .entity_resolution import strip_person_title
        return strip_person_title(name)
    return normalize_space(name)


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


def _fallback_event_name(title: str, summary: str = '', article_id: int | None = None) -> str:
    """Build a deterministic non-category Topic label when GenAI output is unusable."""
    for raw in (title, summary):
        candidate = normalize_space(str(raw or '')).strip(' .,:;|-')[:180]
        if (candidate and len(norm_name(candidate)) >= 5
                and not is_broad_field_topic(candidate)
                and not is_numeric_or_date_entity(candidate)):
            return candidate
    # Real articles normally have a title. Keep the required relationship invariant even
    # for malformed legacy rows with no useful headline/summary.
    if article_id is not None:
        return f'Bài báo {int(article_id)}'
    return 'Tin bài chưa phân loại'


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
    field_category_value = normalize_field_category(result.get('field_category'), article.get('title') or '', article.get('summary') or '')
    topic_required = requires_event_topic(field_category_value)
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
    topic_label_fallback = (
        not event_name or len(norm_name(event_name)) < 5
        or is_broad_field_topic(event_name) or is_numeric_or_date_entity(event_name)
    )
    if topic_label_fallback:
        event_name = _fallback_event_name(article.get('title') or '', article.get('summary') or '', article_id)
    if is_broad_field_topic(event_name):
        event_name = ''
    try:
        confidence = float(result.get('event_confidence', 0.75) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    model_entities = result.get('entities') if isinstance(result.get('entities'), list) else []
    entities, suppressed_person_names = augment_entities_with_named_infrastructure(
        article.get('title') or '', article.get('summary') or '', model_entities, ''
    )
    grounded_text = normalize_space(' '.join(str(article.get(k) or '') for k in ('title', 'summary'))[:20000])
    seen_entity_names = {norm_name(str((item or {}).get('name') or (item or {}).get('canonical_name') or '')) for item in entities if isinstance(item, dict)}
    recovered_priority = []
    for recovered in extract_named_person_entities(grounded_text) + extract_named_proper_noun_entities(grounded_text):
        recovered_norm = norm_name(recovered.get('name') or '')
        if recovered_norm and recovered_norm not in seen_entity_names:
            recovered_priority.append(recovered)
            seen_entity_names.add(recovered_norm)
    # Prioritize independently recovered names ahead of model output so truncation never
    # silently drops source-grounded proper nouns when the model returns many candidates.
    entities = recovered_priority + entities
    existing_entity_names = {norm_name(str((e or {}).get('name') or (e or {}).get('canonical_name') or '')) for e in entities if isinstance(e, dict)}
    for recovered_person in extract_named_person_entities(grounded_text):
        if norm_name(recovered_person['name']) not in existing_entity_names:
            entities.append(recovered_person)
            existing_entity_names.add(norm_name(recovered_person['name']))
    filtered_entities = []
    article_text = normalize_space(' '.join(str(article.get(k) or '') for k in ('title', 'summary'))[:20000])
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_name = entity.get('name') or entity.get('canonical_name')
        entity_type = entity.get('type') or entity.get('entity_type') or 'UNKNOWN'
        aliases = entity.get('aliases') if isinstance(entity.get('aliases'), list) else []
        if (entity_name and str(entity_type).upper().strip() == 'PERSON'
                and norm_name(str(entity_name)) in suppressed_person_names):
            log.debug('Rejected person name that is only part of a named-object name: article_id=%s name=%r', article_id, entity_name)
            continue
        if (entity_name and should_track_entity(str(entity_name), str(entity_type))
                and is_grounded_entity(str(entity_name), aliases, article_text, str(entity_type))):
            filtered_entities.append(entity)
        elif entity_name:
            log.debug('Rejected ungrounded/generic entity article_id=%s name=%r type=%s', article_id, entity_name, entity_type)
    entities = filtered_entities[:50]
    ai_result = {
        'field_category': field_category_value,
        'field_category_source': 'genai' if normalize_field_category(result.get('field_category'), article.get('title') or '', article.get('summary') or '') == field_category_value and result.get('field_category') else 'rule',
        'category_topic_id': None,
        'existing_event_topic_id': existing_event_id,
        'event_topic_name': event_name or None,
        'event_confidence': confidence,
        'entities': entities[:50],
        'named_infrastructure_enriched': True,
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
                        event_name = _fallback_event_name(article.get('title') or '', article.get('summary') or '', article_id)
                    log.warning('Rejected existing event match: article_id=%s proposed_topic_id=%s topic=%r article=%r',
                                article_id, existing_event_id, candidate.get('name'), article.get('title'))
            if event_topic_id is None and event_name and (confidence >= 0.55 or topic_label_fallback or topic_required):
                event_topic_id = _upsert_event_topic(conn, event_name, max(0.55, confidence), None)
            # Hard invariant: these editorial fields must never be saved without a specific
            # event Topic relationship, even when model confidence is low or its label is bad.
            if topic_required and event_topic_id is None:
                event_name = _fallback_event_name(article.get('title') or '', article.get('summary') or '', article_id)
                event_topic_id = _upsert_event_topic(conn, event_name, max(0.70, confidence), None)
                if event_topic_id is None:
                    raise RuntimeError(f'Required Topic could not be assigned for article_id={article_id}, field_category={field_category_value!r}')
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
                elif suppressed_person_names:
                    cur.execute("""DELETE FROM article_entities ae USING entities e
                                   WHERE ae.entity_id=e.entity_id AND ae.article_id=%s
                                     AND upper(e.entity_type)='PERSON' AND (norm_name(e.canonical_name)=ANY(%s) OR EXISTS (SELECT 1 FROM entity_aliases xa WHERE xa.entity_id=e.entity_id AND xa.normalized_alias=ANY(%s)))""",
                                (article_id, list(suppressed_person_names), list(suppressed_person_names)))
                for entity in entities[:50]:
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
                               field_category=%s,ai_classification_error=null,event_topic_scanned_at=now(),updated_at=now()
                               where article_id=%s""", (json.dumps(ai_result, ensure_ascii=False), field_category_value, article_id))
            # Unified Timeline is recomputed lazily by the Timeline API. Avoid running the
            # recursive graph query once per classified article during a batch.
    return True


def enrich_named_infrastructure_entities(limit: int = 100) -> dict[str, int]:
    """Efficient one-time quality repair and named-object backfill for recent articles.

    A single DB connection is reused for the batch (instead of opening one connection per
    article). Article bodies are bounded to 4 KB for deterministic entity rules. Timeline
    components are refreshed lazily when a user opens the timeline, not once per repaired row.
    """
    stats = {'scanned': 0, 'articles_changed': 0, 'entities_added': 0,
             'invalid_entity_links_removed': 0, 'entities_archived': 0, 'failed': 0}
    limit = max(1, min(int(limit or 100), 300))
    orphan_entity_ids: set[int] = set()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT article_id,title,coalesce(summary,'') AS summary,
                                      left(coalesce(content_text,''),4000) AS content_text,
                                      field_category,coalesce(ai_classification,'{}'::jsonb) AS ai_classification
                               FROM news_articles
                               WHERE is_excluded=false
                                 AND ((coalesce(published_at,collected_at) >= now()-interval '30 days'
                                       AND (coalesce(ai_classification->>'entity_quality_rules_v075_enriched','false') <> 'true'
                                            OR coalesce(ai_classification->>'field_category_rules_v075_enriched','false') <> 'true'
                                            OR coalesce(ai_classification->>'named_proper_nouns_v076_enriched','false') <> 'true'
                                            OR coalesce(ai_classification->>'named_proper_nouns_v0716_enriched','false') <> 'true'
                                            OR coalesce(ai_classification->>'entity_grounding_guard_v0731_enriched','false') <> 'true'))
                                      OR field_category IS NULL)
                               ORDER BY coalesce(published_at,collected_at) DESC
                               LIMIT %s""", (limit,))
            rows = cur.fetchall()

        for row in rows:
            stats['scanned'] += 1
            article_id = int(row['article_id'])
            title = row.get('title') or ''
            summary = row.get('summary') or ''
            content_text = row.get('content_text') or ''
            # Entity evidence is scoped to title + summary, as specified for Radar.
            # The article body is retained for general data quality, not entity invention.
            all_text = normalize_space('. '.join((title, summary)))[:20000]
            infra = extract_named_infrastructure_entities(all_text)[:50]
            named_people = extract_named_person_entities(all_text)
            proper_nouns = extract_named_proper_noun_entities(all_text)
            suppress = set()
            for item in infra:
                suffix = item.get('person_suffix')
                if not suffix:
                    continue
                occurrences = list(re.finditer(r'(?<!\w)' + re.escape(suffix) + r'(?!\w)', all_text, re.IGNORECASE | re.UNICODE))
                if occurrences and all(item['start'] <= m.start() and m.end() <= item['end'] for m in occurrences):
                    suppress.add(norm_name(suffix))

            changed = False
            try:
                # Savepoint isolates a malformed row so one bad article won't abort the batch.
                with conn.transaction():
                    with conn.cursor() as cur:
                        classification = row.get('ai_classification') or {}
                        if not isinstance(classification, dict):
                            classification = {}
                        stored_entities = classification.get('entities') if isinstance(classification.get('entities'), list) else []
                        valid_stored = []
                        for entity in stored_entities:
                            if not isinstance(entity, dict):
                                continue
                            entity_name = str(entity.get('name') or entity.get('canonical_name') or '').strip()
                            entity_type = str(entity.get('type') or entity.get('entity_type') or 'UNKNOWN').upper().strip()
                            aliases = entity.get('aliases') if isinstance(entity.get('aliases'), list) else []
                            if not entity_name or not should_track_entity(entity_name, entity_type):
                                continue
                            if entity_type == 'PERSON' and norm_name(entity_name) in suppress:
                                continue
                            if is_grounded_entity(entity_name, aliases, all_text, entity_type):
                                valid_stored.append(entity)

                        # Remove existing per-article links whose names are generic, ungrounded,
                        # administrative, or just a person's name embedded in a named bridge.
                        cur.execute("""SELECT ae.entity_id,ae.mention_text,e.canonical_name,e.entity_type,
                                              coalesce(array_agg(DISTINCT a.alias) FILTER (WHERE a.alias IS NOT NULL),ARRAY[]::text[]) AS aliases
                                       FROM article_entities ae
                                       JOIN entities e ON e.entity_id=ae.entity_id
                                       LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
                                       WHERE ae.article_id=%s AND e.status='active'
                                       GROUP BY ae.entity_id,ae.mention_text,e.canonical_name,e.entity_type""", (article_id,))
                        linked_entities = cur.fetchall()
                        for linked in linked_entities:
                            linked_type = str(linked.get('entity_type') or 'UNKNOWN').upper().strip()
                            linked_name = str(linked.get('canonical_name') or linked.get('mention_text') or '').strip()
                            aliases = list(linked.get('aliases') or [])
                            aliases.append(str(linked.get('mention_text') or ''))
                            invalid_person_suffix = linked_type == 'PERSON' and (
                                norm_name(linked_name) in suppress or norm_name(str(linked.get('mention_text') or '')) in suppress
                            )
                            if invalid_person_suffix or not should_track_entity(linked_name, linked_type) or not is_grounded_entity(linked_name, aliases, all_text, linked_type):
                                cur.execute("DELETE FROM article_entities WHERE article_id=%s AND entity_id=%s AND mention_text=%s",
                                            (article_id, linked['entity_id'], linked['mention_text']))
                                stats['invalid_entity_links_removed'] += max(0, cur.rowcount)
                                if cur.rowcount > 0:
                                    orphan_entity_ids.add(int(linked['entity_id']))
                                changed = changed or cur.rowcount > 0

                        recovered_entities = []
                        recovered_norms = set()
                        for item in infra:
                            entity_id = _upsert_entity(conn, item['name'], item.get('type') or 'LOCATION', item.get('aliases') or [])
                            if not entity_id:
                                continue
                            cur.execute("SELECT 1 FROM article_entities WHERE article_id=%s AND entity_id=%s AND mention_text=%s",
                                        (article_id, entity_id, item['name']))
                            was_missing = cur.fetchone() is None
                            cur.execute("""INSERT INTO article_entities(article_id,entity_id,mention_text,confidence)
                                           VALUES(%s,%s,%s,0.96)
                                           ON CONFLICT(article_id,entity_id,mention_text) DO UPDATE
                                           SET confidence=GREATEST(article_entities.confidence,excluded.confidence)""",
                                        (article_id, entity_id, item['name']))
                            if was_missing:
                                stats['entities_added'] += 1
                                changed = True
                            recovered_entities.append({'name': item['name'], 'type': item.get('type') or 'LOCATION', 'aliases': item.get('aliases') or []})
                            recovered_norms.add(norm_name(item['name']))

                        # Recover explicitly titled people missed by the English spaCy fallback/GenAI.
                        for person in named_people:
                            person_name = person['name']
                            if not is_grounded_entity(person_name, person.get('aliases') or [], all_text, 'PERSON'):
                                continue
                            person_id = _upsert_entity(conn, person_name, 'PERSON', person.get('aliases') or [])
                            if not person_id:
                                continue
                            cur.execute("""INSERT INTO article_entities(article_id,entity_id,mention_text,confidence)
                                           VALUES(%s,%s,%s,0.90) ON CONFLICT(article_id,entity_id,mention_text) DO UPDATE
                                           SET confidence=GREATEST(article_entities.confidence,excluded.confidence)""",
                                        (article_id, person_id, person_name))
                            if not any(norm_name(str(e.get('name') or e.get('canonical_name') or '')) == norm_name(person_name) for e in recovered_entities):
                                recovered_entities.append({'name': person_name, 'type': 'PERSON', 'aliases': person.get('aliases') or []})
                            recovered_norms.add(norm_name(person_name))

                        # Recover proper names missed by the model/NER, constrained to title + summary.
                        # Explicit PERSON recovery is authoritative, so don't create a second
                        # UNKNOWN/ORG row for the same person surface form.
                        named_people_norms = {norm_name(str(x.get('name') or '')) for x in named_people if x.get('name')}
                        for named in proper_nouns:
                            named_name = str(named.get('name') or '').strip()
                            named_type = str(named.get('type') or 'UNKNOWN').upper().strip()
                            if not named_name or norm_name(named_name) in named_people_norms:
                                continue
                            if not is_grounded_entity(named_name, named.get('aliases') or [], all_text, named_type):
                                continue
                            named_id = _upsert_entity(conn, named_name, named_type, named.get('aliases') or [])
                            if not named_id:
                                continue
                            cur.execute("SELECT 1 FROM article_entities WHERE article_id=%s AND entity_id=%s AND mention_text=%s", (article_id, named_id, named_name))
                            was_missing = cur.fetchone() is None
                            cur.execute("""INSERT INTO article_entities(article_id,entity_id,mention_text,confidence)
                                           VALUES(%s,%s,%s,0.78) ON CONFLICT(article_id,entity_id,mention_text) DO UPDATE
                                           SET confidence=GREATEST(article_entities.confidence,excluded.confidence)""",
                                        (article_id, named_id, named_name))
                            if norm_name(named_name) not in recovered_norms:
                                recovered_entities.append({'name': named_name, 'type': named_type, 'aliases': named.get('aliases') or []})
                                recovered_norms.add(norm_name(named_name))
                            if was_missing:
                                stats['entities_added'] += 1
                                changed = True

                        # Save a deterministic editorial field for historical articles, too.
                        existing_field = normalize_field_category(classification.get('field_category'), title, summary)
                        classification['field_category'] = existing_field
                        category_changed = row.get('field_category') != existing_field
                        changed = changed or category_changed
                        classification['field_category_source'] = classification.get('field_category_source') or 'rule'
                        classification['field_category_rules_v075_enriched'] = True

                        # Store recovered names first; discard old model outputs that failed the
                        # new grounding/eligibility rules. The marker prevents re-scanning this row.
                        ordered_entities = list(recovered_entities)
                        for entity in valid_stored:
                            ename = str(entity.get('name') or entity.get('canonical_name') or '').strip()
                            if norm_name(ename) in recovered_norms:
                                continue
                            if all(norm_name(ename) != norm_name(str(old.get('name') or old.get('canonical_name') or '')) for old in ordered_entities):
                                ordered_entities.append(entity)
                        classification['entities'] = ordered_entities[:50]
                        classification['named_entity_rules_v073_enriched'] = True
                        classification['named_proper_nouns_v076_enriched'] = True
                        classification['named_proper_nouns_v0716_enriched'] = True
                        classification['entity_quality_rules_v075_enriched'] = True
                        classification['entity_quality_rules_v075_enriched_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                        classification['entity_grounding_guard_v0731_enriched'] = True
                        cur.execute("""UPDATE news_articles SET ai_classification=%s::jsonb,field_category=%s,updated_at=now()
                                       WHERE article_id=%s""", (json.dumps(classification, ensure_ascii=False), existing_field, article_id))
                if changed:
                    stats['articles_changed'] += 1
            except Exception:
                stats['failed'] += 1
                log.exception('Entity quality/enrichment failed for article_id=%s', article_id)
                # Mark the row attempted so one persistently malformed record cannot starve
                # all older rows on each hourly batch. The error is visible in JSON for audit.
                try:
                    with conn.cursor() as cur:
                        fallback_field = classify_field_category(f'{title} {summary}')
                        cur.execute("""UPDATE news_articles SET ai_classification=coalesce(ai_classification,'{}'::jsonb)
                                       || jsonb_build_object('entity_quality_rules_v075_enriched',true,
                                                            'field_category',%s,
                                                            'field_category_rules_v075_enriched',true,
                                                            'named_proper_nouns_v076_enriched',true,
                                                            'entity_quality_rules_v075_error','row processing failed'),
                                       field_category=coalesce(field_category,%s),updated_at=now() WHERE article_id=%s""", (fallback_field, fallback_field, article_id))
                except Exception:
                    log.exception('Could not mark failed entity quality row article_id=%s', article_id)
        # Hide now-orphaned junk entities from the active catalog without deleting historical
        # article data or removing a user's explicit follow. If other article links remain, a later
        # batch will revisit the entity and archive it when the final invalid link is removed.
        if orphan_entity_ids:
            with conn.cursor() as cur:
                cur.execute("""UPDATE entities e SET status='archived',updated_at=now()
                             WHERE e.entity_id = ANY(%s::bigint[]) AND e.status='active'
                               AND NOT EXISTS (SELECT 1 FROM article_entities ae WHERE ae.entity_id=e.entity_id)
                               AND NOT EXISTS (SELECT 1 FROM user_saved_timelines ust
                                               WHERE ust.timeline_type='entity' AND ust.entity_id=e.entity_id)""",
                            (list(orphan_entity_ids),))
                stats['entities_archived'] = max(0, cur.rowcount)
    log.info('Entity quality/enrichment complete: %s; timeline refresh deferred to first timeline view.', stats)
    return stats

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
    # Smaller batches leave enough completion budget for all entity mentions after summaries
    # are passed without the old 1,200-character truncation.
    batch_size = max(1, min(10, int(settings.ai_classification_batch_size)))
    with get_conn() as conn, conn.cursor() as cur:
        # Only work on recent unclassified rows in the normal hourly schedule; the separate
        # backfill flag is required to process older backlog intentionally. The five-minute
        # ordering preference prioritizes items from this run, while the 24-hour window allows
        # the next hourly run to drain any fresh rows that exceeded this run's per-run limit.
        # Failed rows have a cooldown and retry cap; mismatch repairs are allowed through once and carry a
        # JSON marker to prevent repeated repair loops.
        if since is None:
            since = datetime.now(timezone.utc)
        cur.execute("""select article_id,title,coalesce(summary,'') as summary,coalesce(content_text,'') as content_text,language,published_at,collected_at
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
        categories = [{'label': label} for label in FIELD_CATEGORIES]
    stats['selected'] = len(articles)
    if not articles:
        log.info('GenAI skipped: no recent unclassified articles in the rolling 24-hour window, no retry-ready failures, and no enabled historical backfill; no OpenAI request sent.')
        return stats
    category_ids: set[int] = set()  # fields are stored on ai_classification; never create category Topics
    started = time.monotonic()
    # Topic reconciliation is a separate scheduled workflow. Do not load hundreds of
    # existing topics into each ingestion prompt; generate a specific article label here,
    # then let the reconciliation workflow merge equivalent labels later.
    for start in range(0, len(articles), batch_size):
        batch_rows = articles[start:start + batch_size]
        prompt_rows = [{'article_id': int(a['article_id']), 'title': a['title'][:600],
                        'summary': (a['summary'] or '')[:4000],
                        'content_excerpt': (a.get('content_text') or '')[:1200],
                        'language': a.get('language') or 'vi'} for a in batch_rows]
        # Existing topics are deliberately omitted. The independent reconciliation
        # workflow is responsible for joining equivalent event labels after ingestion.
        batch_existing_events = []
        batch_event_topics = {int(r['topic_id']): r for r in batch_existing_events}
        batch_started = time.monotonic()
        log.info('GenAI batch %s/%s started: articles=%s; existing_event_candidates=%s',
                 start // batch_size + 1, (len(articles) + batch_size - 1) // batch_size,
                 len(batch_rows), len(batch_existing_events))
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
        finally:
            log.info('GenAI batch %s complete; elapsed=%.1fs', start // batch_size + 1, time.monotonic()-batch_started)
    log.info('GenAI event normalization complete: selected=%s classified=%s events=%s entities=%s repaired_mismatches=%s failed=%s elapsed=%.1fs model=%s',
             stats['selected'], stats['classified'], stats['events_assigned'], stats['entities_assigned'], stats['repaired_mismatches'], stats['failed'], time.monotonic()-started, settings.openai_model)
    return stats
