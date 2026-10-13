"""Daily RSS -> Raw/Clean -> topics/entities -> FACTs pipeline."""
from __future__ import annotations
import calendar, hashlib, json, logging, re, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import feedparser
import requests
from dateutil import parser as date_parser
from psycopg.types.json import Jsonb
from .config import settings
from .db import get_conn
from .processing import (apply_cleaning_rules, canonicalize_url, clean_html, extract_entities,
                         hash_text, keyword_matches, norm_name, normalize_space, slugify, extract_event_signature)

log = logging.getLogger('mini-lakehouse.pipeline')
USER_AGENT = 'DoraemonMiniLakehouse/1.0 (+RSS feed reader; contact admin of the configured source)'

def parse_date(entry: dict):
    # Parse the original date text before feedparser's struct_time fields. Some feeds
    # (including Vietnamese publishers) use values such as "10/13/2026 12:00:00 PM"
    # without an offset. feedparser may leave published_parsed empty for these values,
    # and interpreting the naive text as UTC shifts the displayed time by +7 hours.
    # For naive text, assume the configured local timezone; explicit offsets still win.
    for key in ('published', 'updated', 'created', 'date'):
        val = entry.get(key)
        if val:
            try:
                dt = date_parser.parse(str(val))
                if dt.tzinfo is None:
                    try:
                        naive_tz = ZoneInfo(settings.timezone)
                    except (ZoneInfoNotFoundError, ValueError):
                        log.warning('Invalid TIMEZONE=%r; naive RSS timestamps will be treated as UTC', settings.timezone)
                        naive_tz = timezone.utc
                    dt = dt.replace(tzinfo=naive_tz)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    # Fall back to feedparser's normalized tuple only when no source date string
    # could be parsed. RSS parsed tuples do not preserve whether the source omitted TZ.
    for key in ('published_parsed', 'updated_parsed', 'created_parsed'):
        val = entry.get(key)
        if val:
            try:
                return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
    return None

def extract_feed_content(entry: dict) -> tuple[str,str]:
    html = ''
    contents = entry.get('content') or []
    for item in contents:
        if isinstance(item, dict) and item.get('value'):
            html = str(item['value'])
            break
    summary_html = entry.get('summary') or entry.get('description') or ''
    if not html: html = str(summary_html)
    content_text = clean_html(html)[:120000]
    summary = clean_html(summary_html)[:4000]
    return summary, content_text

def add_quality_issue(conn, article_id, source_id, issue_type, severity, details):
    with conn.cursor() as cur:
        cur.execute("""select issue_id from data_quality_issues where article_id=%s and issue_type=%s and resolved_at is null limit 1""", (article_id, issue_type))
        if cur.fetchone(): return
        cur.execute("""insert into data_quality_issues(article_id,source_id,issue_type,severity,details) values(%s,%s,%s,%s,%s)""", (article_id,source_id,issue_type,severity,details[:1000]))

def get_spacy_entity_matches(title, summary):
    try: return extract_entities(f'{title}. {summary[:2000]}')
    except Exception as exc:
        log.warning('NER failed; fallback result used: %s', exc)
        return []

def load_entity_lookup(conn) -> dict:
    """Load the approved entity catalog once per pipeline run, not once per article."""
    with conn.cursor() as cur:
        cur.execute("select entity_id, canonical_name, normalized_name, entity_type from entities where status='active'")
        known = cur.fetchall()
        cur.execute('select entity_id, normalized_alias from entity_aliases')
        aliases = cur.fetchall()
    return {
        'alias_to_entity': {r['normalized_alias']: r['entity_id'] for r in aliases},
        'known_map': {(r['normalized_name'], r['entity_type']): r['entity_id'] for r in known},
        'known_name_fallback': {r['normalized_name']: r['entity_id'] for r in known},
    }


def load_active_topics(conn) -> list[dict]:
    """Load category topics once per run; event topics are handled separately."""
    with conn.cursor() as cur:
        cur.execute("select topic_id, keywords, topic_type, event_key from topics where status='active' and (topic_type='category' or event_key is null)")
        return cur.fetchall()


def process_entities(conn, article_id, source_id, title, summary, entity_lookup=None):
    """Fallback entity extraction when GenAI is not configured; entities are auto-created."""
    found = get_spacy_entity_matches(title, summary)
    if entity_lookup is None:
        entity_lookup = load_entity_lookup(conn)
    alias_to_entity = entity_lookup['alias_to_entity']
    known_map = entity_lookup['known_map']
    known_name_fallback = entity_lookup['known_name_fallback']
    count_known = 0
    for mention, typ, confidence in found:
        normalized = norm_name(mention)
        if len(normalized) < 2:
            continue
        entity_id = known_map.get((normalized, typ)) or alias_to_entity.get(normalized) or known_name_fallback.get(normalized)
        if not entity_id:
            with conn.cursor() as cur:
                cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,description,origin)
                               values(%s,%s,%s,'Fallback entity extraction from article text.','discovered')
                               on conflict(normalized_name,entity_type) do update set updated_at=now()
                               returning entity_id""", (mention, normalized, typ))
                entity_id = cur.fetchone()['entity_id']
                cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                               values(%s,%s,%s) on conflict do nothing""", (entity_id, mention, normalized))
            known_map[(normalized, typ)] = entity_id
            known_name_fallback[normalized] = entity_id
            alias_to_entity[normalized] = entity_id
        else:
            count_known += 1
        with conn.cursor() as cur:
            cur.execute("""insert into article_entities(article_id,entity_id,mention_text,confidence)
                           values(%s,%s,%s,%s) on conflict do nothing""", (article_id, entity_id, mention, confidence))
    return len(found), count_known

def apply_topics(conn, article_id, title, summary, topic_rows=None):
    text = f'{title} {summary}'
    if topic_rows is None:
        topic_rows = load_active_topics(conn)
    matched = 0
    with conn.cursor() as cur:
        for topic in topic_rows:
            kws = topic['keywords'] or []
            hits = keyword_matches(text,kws)
            if not hits: continue
            confidence = min(0.99, 0.65 + 0.08 * min(len(hits),4))
            cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                           values(%s,%s,%s,'keyword') on conflict(article_id,topic_id) do update set confidence=greatest(article_topics.confidence,excluded.confidence)""", (article_id,topic['topic_id'],confidence))
            matched += 1
    return matched


def assign_event_topic(conn, article_id: int, title: str) -> bool:
    """Assign a high-confidence event topic such as “Mỹ tấn công Iran”."""
    event = extract_event_signature(title)
    if not event:
        with conn.cursor() as cur:
            cur.execute("update news_articles set event_topic_scanned_at=now() where article_id=%s", (article_id,))
        return False
    with conn.cursor() as cur:
        cur.execute("select topic_id from topics where slug=%s and topic_type='category' and status='active' limit 1", (event['parent_slug'],))
        parent = cur.fetchone()
        parent_id = parent['topic_id'] if parent else None
        slug = 'event-' + slugify(event['name'])
        cur.execute("""insert into topics(slug,event_key,name,description,keywords,parent_topic_id,topic_type,status,origin)
                       values(%s,%s,%s,%s,%s,%s,'event','active','discovered')
                       on conflict(event_key) do update set updated_at=now()
                       returning topic_id""",
                    (slug, event['event_key'], event['name'],
                     f"Sự kiện phát hiện từ tiêu đề; bên liên quan: {event['actor']} – {event['target']}.",
                     event['keywords'], parent_id))
        topic_id = cur.fetchone()['topic_id']
        cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                       values(%s,%s,%s,'event_rule')
                       on conflict(article_id,topic_id) do update
                       set confidence=greatest(article_topics.confidence,excluded.confidence)""",
                    (article_id, topic_id, event['confidence']))
        cur.execute("update news_articles set event_topic_scanned_at=now() where article_id=%s", (article_id,))
    return True


def backfill_unscanned_event_topics(limit: int | None = None) -> int:
    """Backfill a bounded batch of recent articles; remaining rows continue next run."""
    if limit is None:
        limit = max(1, settings.event_topic_backfill_batch_size)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select article_id,title,is_excluded from news_articles
                       where event_topic_scanned_at is null
                         and coalesce(published_at,collected_at) >= now() - interval '30 days'
                       order by coalesce(published_at,collected_at) desc limit %s""", (limit,))
        rows = cur.fetchall()
    scanned = 0
    assigned = 0
    with get_conn() as conn:
        for row in rows:
            if not row['is_excluded'] and assign_event_topic(conn, row['article_id'], row['title']):
                assigned += 1
            else:
                with conn.cursor() as cur:
                    cur.execute("update news_articles set event_topic_scanned_at=now() where article_id=%s", (row['article_id'],))
            scanned += 1
    if scanned:
        log.info('Event-topic backfill: scanned=%s assigned=%s', scanned, assigned)
    return assigned


def conditional_feed_headers(source: dict) -> dict[str, str]:
    """Build HTTP validators so RSS providers can return 304 when unchanged."""
    headers = {'User-Agent': USER_AGENT}
    etag = source.get('etag')
    last_modified = source.get('last_modified')
    if etag:
        headers['If-None-Match'] = str(etag)
    if last_modified:
        headers['If-Modified-Since'] = str(last_modified)
    return headers


def process_source(source: dict, rules: list[dict], entity_lookup=None, active_topics=None) -> dict:
    source_started = time.monotonic()
    log.info('Source started: %s; feed=%s', source['name'], source['feed_url'])
    result = {'seen': 0, 'inserted': 0, 'updated': 0, 'skipped_existing': 0,
              'skipped_outside_window': 0, 'skipped_undated': 0,
              'feed_not_modified': False, 'entity_mentions': 0, 'new_entities': 0,
              'event_topics_assigned': 0, 'quality_issues': 0, 'error': None}
    resp = requests.get(source['feed_url'], timeout=(10, settings.request_timeout_seconds),
                        headers=conditional_feed_headers(source))
    if resp.status_code == 304:
        result['feed_not_modified'] = True
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("update data_sources set last_run_at=now(),last_status='success',last_error=null,updated_at=now() where source_id=%s",
                        (source['source_id'],))
        log.info('RSS unchanged (HTTP 304): %s; elapsed=%.1fs', source['name'], time.monotonic()-source_started)
        return result
    resp.raise_for_status()
    response_etag = resp.headers.get('ETag')
    response_last_modified = resp.headers.get('Last-Modified')
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""update data_sources
                       set etag=coalesce(%s,etag), last_modified=coalesce(%s,last_modified), updated_at=now()
                       where source_id=%s""",
                    (response_etag, response_last_modified, source['source_id']))
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f'RSS parsing failed: {str(parsed.bozo_exception)[:300]}')
    entries = parsed.entries[:settings.max_feed_entries_per_source]
    if not entries:
        raise RuntimeError('RSS feed contains no entries')

    # Filter by a rolling publication window before any database enrichment.
    # Undated items are skipped by default because their age cannot be verified.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, settings.pipeline_lookback_hours))
    prepared = []
    for entry in entries:
        result['seen'] += 1
        title = normalize_space(clean_html(entry.get('title', '')))
        raw_url = str(entry.get('link') or '').strip()
        canon = canonicalize_url(raw_url)
        pub_date = parse_date(entry)
        if not title or not canon:
            result['quality_issues'] += 1
            continue
        if pub_date is None:
            result['skipped_undated'] += 1
            continue
        if pub_date < cutoff:
            result['skipped_outside_window'] += 1
            continue
        summary, content_text = extract_feed_content(entry)
        author = normalize_space(str(entry.get('author') or ''))[:250] or None
        external_id = str(entry.get('id') or entry.get('guid') or '')[:1000] or None
        entry_key = external_id or canon or hash_text(title)
        payload = {str(k): entry.get(k) for k in entry.keys()}
        payload = json.loads(json.dumps(payload, default=str))
        prepared.append({'title':title,'raw_url':raw_url,'canonical_url':canon,'published_at':pub_date,
                         'summary':summary,'content_text':content_text,'author':author,
                         'external_id':external_id,'entry_key':entry_key,'payload':payload})

    if not prepared:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("update data_sources set last_run_at=now(),last_status='success',last_error=null,updated_at=now() where source_id=%s",
                        (source['source_id'],))
        log.info('Source %s: feed entries=%s, within_24h=0, skipped_old=%s, skipped_undated=%s, elapsed=%.1fs',
                 source['name'], result['seen'], result['skipped_outside_window'], result['skipped_undated'], time.monotonic()-source_started)
        return result

    # One batch lookup per source replaces SELECT-per-entry. Unique constraint remains the
    # final concurrency-safe guard when inserting rows.
    urls = list(dict.fromkeys(item['canonical_url'] for item in prepared))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select canonical_url from news_articles where source_id=%s and canonical_url = any(%s)",
                    (source['source_id'], urls))
        known_urls = {row['canonical_url'] for row in cur.fetchall()}

    with get_conn() as conn:
        for processed_index, item in enumerate(prepared, start=1):
            if processed_index == 1 or processed_index % 25 == 0:
                log.info('Source %s processing prepared entry %s/%s', source['name'], processed_index, len(prepared))
            canon = item['canonical_url']
            if canon in known_urls:
                result['skipped_existing'] += 1
                continue
            # Also catches duplicate entries repeated within a single RSS response.
            known_urls.add(canon)
            title, raw_url, pub_date = item['title'], item['raw_url'], item['published_at']
            summary, content_text = item['summary'], item['content_text']
            author, external_id, entry_key, payload = item['author'], item['external_id'], item['entry_key'], item['payload']
            with conn.cursor() as cur:
                cur.execute("""insert into raw_feed_items(source_id,entry_key,item_url,raw_payload,fetched_at)
                               values(%s,%s,%s,%s::jsonb,now())
                               on conflict(source_id,entry_key) do update set item_url=excluded.item_url,raw_payload=excluded.raw_payload,fetched_at=now()
                               returning raw_id""",
                            (source['source_id'], entry_key, raw_url, json.dumps(payload, ensure_ascii=False)))
                raw_id = cur.fetchone()['raw_id']
            excluded, reason = apply_cleaning_rules(title, canon, source['source_id'], rules)
            quality = 100
            if len(title) < 15: quality -= 15
            if len(content_text) < 80: quality -= 20
            if not author: quality -= 5
            with conn.cursor() as cur:
                cur.execute("""insert into news_articles(source_id,raw_id,external_id,url,canonical_url,title,summary,content_text,author,language,published_at,headline_hash,content_hash,is_excluded,exclude_reason,quality_score)
                               values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               on conflict(source_id,canonical_url) do nothing
                               returning article_id""",
                            (source['source_id'], raw_id, external_id, raw_url, canon, title, summary,
                             content_text, author, source.get('language') or 'vi', pub_date,
                             hash_text(title), hash_text(content_text) if content_text else None,
                             excluded, reason, max(0, quality)))
                inserted_row = cur.fetchone()
            if not inserted_row:
                result['skipped_existing'] += 1
                continue
            article_id = inserted_row['article_id']
            result['inserted'] += 1
            if len(title) < 15:
                add_quality_issue(conn, article_id, source['source_id'], 'short_title', 'warning',
                                  f'Title length is {len(title)} characters.')
                result['quality_issues'] += 1
            if len(content_text) < 80:
                add_quality_issue(conn, article_id, source['source_id'], 'short_or_missing_content', 'warning',
                                  f'RSS content text contains only {len(content_text)} characters.')
                result['quality_issues'] += 1
            with conn.cursor() as cur:
                cur.execute("select article_id,source_id from news_articles where headline_hash=%s and article_id<>%s limit 1",
                            (hash_text(title), article_id))
                duplicate = cur.fetchone()
            if duplicate:
                add_quality_issue(conn, article_id, source['source_id'], 'duplicate_headline', 'info',
                                  f'Exact normalized headline also exists in article {duplicate["article_id"]} from source {duplicate["source_id"]}.')
                result['quality_issues'] += 1
            if not excluded:
                apply_topics(conn, article_id, title, summary, topic_rows=active_topics)
                if not settings.openai_api_key:
                    if assign_event_topic(conn, article_id, title):
                        result['event_topics_assigned'] += 1
                    found_count, known_count = process_entities(conn, article_id, source['source_id'], title, summary, entity_lookup=entity_lookup)
                    result['entity_mentions'] += found_count
                    result['new_entities'] += max(0, found_count - known_count)
            else:
                with conn.cursor() as cur:
                    cur.execute("update news_articles set event_topic_scanned_at=now(),ai_classified_at=coalesce(ai_classified_at,now()) where article_id=%s", (article_id,))
        with conn.cursor() as cur:
            cur.execute("update data_sources set last_run_at=now(),last_status='success',last_error=null,updated_at=now() where source_id=%s",
                        (source['source_id'],))
    log.info('Source %s: entries=%s, within_window=%s, inserted=%s, skipped_existing=%s, skipped_old=%s, skipped_undated=%s, event_topics=%s',
             source['name'], result['seen'], len(prepared), result['inserted'], result['skipped_existing'],
             result['skipped_outside_window'], result['skipped_undated'], result['event_topics_assigned'])
    log.info('Source finished: %s; inserted=%s; elapsed=%.1fs', source['name'], result['inserted'], time.monotonic()-source_started)
    return result

def discover_topic_candidates():
    """Cluster recent headlines (including articles already assigned to broad categories).

    Candidate labels use a representative real headline instead of a bag of generic words,
    so an admin can approve a specific event label. High-confidence actor/action/target events
    are handled automatically by extract_event_signature().
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.cluster import DBSCAN
        import numpy as np
    except Exception as exc:
        log.warning('Topic discovery skipped; scikit-learn unavailable: %s', exc)
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select a.article_id,a.source_id,a.title,a.summary,
                              exists(select 1 from article_topics at join topics t on t.topic_id=at.topic_id
                                     where at.article_id=a.article_id and t.topic_type='event' and t.status='active') as has_event_topic
                       from news_articles a
                       where a.is_excluded=false
                         and coalesce(a.published_at,a.collected_at) >= now() - (%s || ' days')::interval
                       order by coalesce(a.published_at,a.collected_at) desc limit %s""", (settings.pipeline_lookback_days, max(100, settings.topic_discovery_article_limit)))
        articles = cur.fetchall()
    if len(articles) < 5:
        return 0
    texts = [normalize_space(a['title']) for a in articles]
    try:
        multilingual_stop_words = (
            'the a an and or but for with from after before over under this that those these ' 
            'là và của có cho với trong trên dưới tại từ đến khi những các một này đó được đang sẽ đã ' 
            'người ngày năm tháng theo về việc không cũng nhưng nếu thì mà để bởi tại do vào ra như hơn nhất ' 
            'tin bài báo mới cập nhật hôm nay qua sáng chiều tối thứ về sau trước một số nhiều giữa'
        ).split()
        vectorizer = TfidfVectorizer(stop_words=multilingual_stop_words, ngram_range=(1,2), min_df=2,
                                     max_features=3500,
                                     token_pattern=r'(?u)\b[^\W\d_][^\W_]{1,}\b', sublinear_tf=True)
        matrix = vectorizer.fit_transform(texts)
        if matrix.shape[1] < 2:
            return 0
        labels = DBSCAN(eps=0.58, min_samples=2, metric='cosine').fit_predict(matrix)
        terms = vectorizer.get_feature_names_out()
    except Exception as exc:
        log.warning('Topic clustering skipped: %s', exc)
        return 0
    discovered = 0
    for label in set(labels.tolist()):
        if label < 0:
            continue
        indices = [i for i, value in enumerate(labels) if value == label]
        if len(indices) < 2:
            continue
        cluster_sources = {articles[i]['source_id'] for i in indices}
        if len(cluster_sources) < 2 and len(indices) < 3:
            continue
        if all(articles[i]['has_event_topic'] for i in indices):
            continue
        weights = np.asarray(matrix[indices].sum(axis=0)).ravel()
        keywords = []
        for term in terms[weights.argsort()[::-1]]:
            if len(term) > 2 and term not in keywords:
                keywords.append(str(term))
            if len(keywords) >= 8:
                break
        if not keywords:
            continue
        # Representative title: choose the headline with highest average similarity to cluster.
        submatrix = matrix[indices]
        similarities = np.asarray((submatrix @ submatrix.T).sum(axis=1)).ravel()
        representative_idx = indices[int(similarities.argmax())]
        name = normalize_space(articles[representative_idx]['title'])[:180]
        cluster_key = hash_text('|'.join(sorted(keywords[:6])))
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""insert into topic_candidates(cluster_key,suggested_name,keywords,candidate_type,status)
                           values(%s,%s,%s,'event','pending')
                           on conflict(cluster_key) do update set last_seen_at=now(),
                             suggested_name=case when topic_candidates.status='pending' then excluded.suggested_name else topic_candidates.suggested_name end
                           returning candidate_id,status""", (cluster_key, name, keywords))
            row = cur.fetchone()
            if row['status'] != 'pending':
                continue
            for idx in indices:
                article = articles[idx]
                cur.execute("""insert into topic_candidate_occurrences(candidate_id,article_id,source_id)
                               values(%s,%s,%s) on conflict do nothing""",
                            (row['candidate_id'], article['article_id'], article['source_id']))
            discovered += 1
    return discovered

def refresh_facts():
    """Refresh recent facts and insert any historical fact rows that do not exist yet.

    This avoids rewriting every historical fact row and avoids per-article correlated count
    subqueries on each scheduled run. Backfilled recent articles are refreshed in the window.
    """
    started = time.monotonic()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH target_articles AS MATERIALIZED (
              SELECT a.*
              FROM news_articles a
              LEFT JOIN fact_news_article f ON f.article_id=a.article_id
              WHERE f.article_id IS NULL
                 OR coalesce(a.published_at,a.collected_at) >= now() - interval '30 days'
            ), entity_counts AS (
              SELECT ae.article_id, count(*)::int AS n
              FROM article_entities ae JOIN target_articles ta ON ta.article_id=ae.article_id
              GROUP BY ae.article_id
            ), topic_counts AS (
              SELECT at.article_id, count(*)::int AS n
              FROM article_topics at JOIN target_articles ta ON ta.article_id=at.article_id
              GROUP BY at.article_id
            )
            INSERT INTO fact_news_article(
              article_id,source_id,published_date,title_length,word_count,
              detected_entity_count,detected_topic_count,quality_score,is_excluded,loaded_at
            )
            SELECT ta.article_id,ta.source_id,
              coalesce((ta.published_at at time zone %s)::date,(ta.collected_at at time zone %s)::date),
              char_length(ta.title),
              CASE WHEN trim(ta.content_text)='' THEN 0
                   ELSE cardinality(regexp_split_to_array(trim(ta.content_text),'\\s+')) END,
              coalesce(ec.n,0),coalesce(tc.n,0),ta.quality_score,ta.is_excluded,now()
            FROM target_articles ta
            LEFT JOIN entity_counts ec ON ec.article_id=ta.article_id
            LEFT JOIN topic_counts tc ON tc.article_id=ta.article_id
            ON CONFLICT(article_id) DO UPDATE SET
              source_id=excluded.source_id,published_date=excluded.published_date,
              title_length=excluded.title_length,word_count=excluded.word_count,
              detected_entity_count=excluded.detected_entity_count,
              detected_topic_count=excluded.detected_topic_count,
              quality_score=excluded.quality_score,is_excluded=excluded.is_excluded,
              loaded_at=now()
        """, (settings.timezone, settings.timezone))
        facts_rows = cur.rowcount
        cur.execute("delete from fact_topic_daily where stat_date >= (current_date - 30)")
        cur.execute("""insert into fact_topic_daily(stat_date,topic_id,source_id,article_count)
                       select coalesce((a.published_at at time zone %s)::date,(a.collected_at at time zone %s)::date),at.topic_id,a.source_id,count(*)
                       from news_articles a join article_topics at on at.article_id=a.article_id join topics t on t.topic_id=at.topic_id
                       where a.is_excluded=false and t.status='active' and coalesce((a.published_at at time zone %s)::date,(a.collected_at at time zone %s)::date) >= current_date-30
                       group by 1,2,3""",(settings.timezone,settings.timezone,settings.timezone,settings.timezone))
        cur.execute("delete from fact_entity_daily where stat_date >= current_date-30")
        cur.execute("""insert into fact_entity_daily(stat_date,entity_id,source_id,article_count)
                       select coalesce((a.published_at at time zone %s)::date,(a.collected_at at time zone %s)::date),ae.entity_id,a.source_id,count(distinct a.article_id)
                       from news_articles a join article_entities ae on ae.article_id=a.article_id join entities e on e.entity_id=ae.entity_id
                       where a.is_excluded=false and e.status='active' and coalesce((a.published_at at time zone %s)::date,(a.collected_at at time zone %s)::date) >= current_date-30
                       group by 1,2,3""",(settings.timezone,settings.timezone,settings.timezone,settings.timezone))
        cur.execute("delete from fact_source_daily where stat_date >= current_date-30")
        cur.execute("""insert into fact_source_daily(stat_date,source_id,article_count,excluded_count,avg_quality_score)
                       select coalesce((published_at at time zone %s)::date,(collected_at at time zone %s)::date),source_id,count(*),count(*) filter(where is_excluded),avg(quality_score)
                       from news_articles where coalesce((published_at at time zone %s)::date,(collected_at at time zone %s)::date)>=current_date-30 group by 1,2""",(settings.timezone,settings.timezone,settings.timezone,settings.timezone))
    log.info('FACT base and daily aggregates refreshed; fact_article_rows=%s; elapsed=%.1fs', facts_rows, time.monotonic()-started)
    refresh_started = time.monotonic()
    refresh_topic_trending()
    log.info('FACT topic trends refreshed; elapsed=%.1fs', time.monotonic()-refresh_started)

def refresh_topic_trending():
    """Recompute topic rankings for today, rolling weeks and calendar months in the configured timezone.

    Ranking favors article volume, then cross-source coverage, with a small bonus for positive
    period-over-period growth. It is a descriptive heat score, not a truth or importance score.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select (now() at time zone %s)::date as local_today""", (settings.timezone,))
        today = cur.fetchone()['local_today']
        cur.execute("""delete from fact_topic_trending
                       where (period_type='day' and period_start >= %s - 60)
                          or (period_type='week' and period_start >= %s - 183)
                          or (period_type='month' and period_start >= date_trunc('month', %s::timestamp)::date - interval '12 months')""",
                    (today, today, today))
        cur.execute(r"""
            WITH cfg AS (
              SELECT %s::text AS tz, (now() at time zone %s)::date AS today
            ), article_topic_base AS (
              SELECT a.article_id, a.source_id, at.topic_id,
                     (coalesce(a.published_at,a.collected_at) at time zone cfg.tz) AS local_ts,
                     ((coalesce(a.published_at,a.collected_at) at time zone cfg.tz)::date) AS local_date,
                     cfg.today
              FROM news_articles a
              JOIN article_topics at ON at.article_id=a.article_id
              JOIN topics t ON t.topic_id=at.topic_id AND t.status='active' AND t.topic_type='event'
              CROSS JOIN cfg
              WHERE a.is_excluded=false
                AND ((coalesce(a.published_at,a.collected_at) at time zone cfg.tz)::date) >= cfg.today - 400
            ), period_rows AS (
              SELECT 'day'::text AS period_type, local_date AS period_start, topic_id, article_id, source_id, today
              FROM article_topic_base WHERE local_date >= today - 60 AND local_date <= today
              UNION ALL
              SELECT 'week'::text, date_trunc('week',local_ts)::date, topic_id, article_id, source_id, today
              FROM article_topic_base WHERE local_date >= today - 183 AND local_date <= today
              UNION ALL
              SELECT 'month'::text, date_trunc('month',local_ts)::date, topic_id, article_id, source_id, today
              FROM article_topic_base
              WHERE local_date >= (date_trunc('month',today::timestamp) - interval '12 months')::date
                AND local_date <= today
            ), aggregated AS (
              SELECT period_type, period_start, topic_id,
                     count(DISTINCT article_id)::int AS article_count,
                     count(DISTINCT source_id)::int AS source_count
              FROM period_rows GROUP BY period_type, period_start, topic_id
            ), with_previous AS (
              SELECT a.*,
                coalesce(p.article_count,0)::int AS previous_article_count,
                CASE WHEN coalesce(p.article_count,0) > 0
                     THEN round(100.0 * (a.article_count - p.article_count) / p.article_count, 2)
                     ELSE NULL END AS growth_pct
              FROM aggregated a
              LEFT JOIN aggregated p ON p.period_type=a.period_type AND p.topic_id=a.topic_id
                AND p.period_start = CASE
                  WHEN a.period_type='day' THEN a.period_start - 1
                  WHEN a.period_type='week' THEN a.period_start - 7
                  WHEN a.period_type='month' THEN (a.period_start - interval '1 month')::date
                END
            ), scored AS (
              SELECT *,
                (article_count + (2.0 * ln(1 + source_count))
                  + (least(greatest(coalesce(growth_pct,0),0),200) / 100.0))::numeric(12,3) AS hotness_score
              FROM with_previous
            ), ranked AS (
              SELECT *, row_number() OVER (PARTITION BY period_type,period_start
                            ORDER BY hotness_score DESC, article_count DESC, source_count DESC, topic_id)::int AS rank_position
              FROM scored
            )
            INSERT INTO fact_topic_trending(
              period_type,period_start,topic_id,article_count,source_count,
              previous_article_count,growth_pct,hotness_score,rank_position,updated_at
            )
            SELECT period_type,period_start,topic_id,article_count,source_count,
                   previous_article_count,growth_pct,hotness_score,rank_position,now()
            FROM ranked
            ON CONFLICT(period_type,period_start,topic_id) DO UPDATE SET
              article_count=excluded.article_count, source_count=excluded.source_count,
              previous_article_count=excluded.previous_article_count, growth_pct=excluded.growth_pct,
              hotness_score=excluded.hotness_score, rank_position=excluded.rank_position, updated_at=now()
        """, (settings.timezone, settings.timezone))


def purge_raw_feed_items_after_fact_refresh() -> int:
    """Delete processed RSS payloads only after every article has a base FACT row.

    news_articles is the durable clean layer and retains title/summary/url/classification.
    raw_feed_items is transient ingestion payload; deleting it sets news_articles.raw_id to NULL.
    If any article lacks fact_news_article, preserve all raw rows. Also preserve orphan raw rows
    that have no clean article yet, so a failed/interrupted ingestion can be investigated/retried.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select count(*)::int as missing
                       from news_articles a
                       left join fact_news_article f on f.article_id=a.article_id
                       where f.article_id is null""")
        missing = int(cur.fetchone()['missing'])
        if missing:
            log.warning('Raw payload cleanup skipped: %s article(s) have no fact_news_article row.', missing)
            return 0
        cur.execute("""delete from raw_feed_items r
                       where exists (
                         select 1 from news_articles a
                         join fact_news_article f on f.article_id=a.article_id
                         where a.raw_id=r.raw_id
                       )
                       and not exists (
                         select 1 from news_articles a
                         left join fact_news_article f on f.article_id=a.article_id
                         where a.raw_id=r.raw_id and f.article_id is null
                       )""")
        deleted = max(0, cur.rowcount)
        cur.execute("select count(*)::int as remaining from raw_feed_items")
        remaining = int(cur.fetchone()['remaining'])
    log.info('Raw payload cleanup complete: deleted=%s processed row(s); remaining_unlinked_or_pending=%s; clean articles and FACT rows retained.', deleted, remaining)
    return deleted


def run_pipeline(trigger_type='scheduled', source_ids: list[int] | None = None) -> dict:
    started = datetime.now(timezone.utc)
    run_clock = time.monotonic()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("insert into pipeline_runs(trigger_type,status,started_at) values(%s,'running',now()) returning run_id",(trigger_type,))
        run_id = cur.fetchone()['run_id']
        cur.execute('select * from data_sources where enabled=true order by source_id')
        sources=cur.fetchall()
        cur.execute('select * from cleaning_rules where enabled=true order by rule_id')
        rules=cur.fetchall()
        # Cache reference catalogs once per pipeline run instead of querying them per article.
        entity_lookup = load_entity_lookup(conn)
        active_topics = load_active_topics(conn)
    if source_ids is not None:
        sources=[s for s in sources if s['source_id'] in source_ids]
    totals={'run_id':run_id,'sources':len(sources),'entries_seen':0,'entries_skipped_existing':0,'entries_skipped_outside_window':0,'entries_skipped_undated':0,'feeds_not_modified':0,'articles_inserted':0,'articles_updated':0,'entity_mentions':0,'new_entity_mentions':0,'event_topics_assigned':0,'event_topics_backfilled':0,'topic_candidates':0,'quality_issues':0,'raw_rows_purged':0,'source_errors':[]}
    log.info('Pipeline run %s started: sources=%s; active_topics=%s; active_entity_cache=%s; lookback_hours=%s; backfill_batch=%s',
             run_id, len(sources), len(active_topics), len(entity_lookup['known_map']), settings.pipeline_lookback_hours,
             settings.event_topic_backfill_batch_size)
    try:
        for source in sources:
            source_run_started = time.monotonic()
            try:
                stats=process_source(source,rules,entity_lookup=entity_lookup,active_topics=active_topics)
                for key in ('seen','inserted','updated','skipped_existing','skipped_outside_window','skipped_undated','feed_not_modified','entity_mentions','new_entities','event_topics_assigned','quality_issues'):
                    mapkey={'seen':'entries_seen','inserted':'articles_inserted','updated':'articles_updated','skipped_existing':'entries_skipped_existing','skipped_outside_window':'entries_skipped_outside_window','skipped_undated':'entries_skipped_undated','feed_not_modified':'feeds_not_modified','new_entities':'new_entity_mentions'}.get(key,key)
                    totals[mapkey] += int(bool(stats[key])) if key == 'feed_not_modified' else stats[key]
                log.info('Stage source complete: %s; elapsed=%.1fs', source['name'], time.monotonic()-source_run_started)
            except Exception as exc:
                msg=f'{type(exc).__name__}: {str(exc)[:400]}'
                log.exception('Source %s failed: %s', source['name'], msg)
                totals['source_errors'].append({'source_id':source['source_id'],'name':source['name'],'error':msg})
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("update data_sources set last_run_at=now(),last_status='error',last_error=%s,updated_at=now() where source_id=%s",(msg,source['source_id']))
        stage_started = time.monotonic()
        if settings.openai_api_key:
            from .genai_events import classify_unprocessed_articles
            ai_stats = classify_unprocessed_articles(limit=settings.ai_classification_run_limit, since=started)
            totals['ai_articles_classified'] = ai_stats['classified']
            totals['ai_articles_failed'] = ai_stats['failed']
            totals['event_topics_assigned'] += ai_stats['events_assigned']
            totals['new_entity_mentions'] += ai_stats['entities_assigned']
            if ai_stats['disabled']:
                log.warning('AI classification is disabled.')
            log.info('Stage GPT normalization complete: %s; elapsed=%.1fs', ai_stats, time.monotonic()-stage_started)
        else:
            totals['event_topics_backfilled']=backfill_unscanned_event_topics()
            log.info('Stage rule-based event backfill complete: assigned=%s; elapsed=%.1fs', totals['event_topics_backfilled'], time.monotonic()-stage_started)
            log.warning('OPENAI_API_KEY is not set; running deterministic event fallback. No topic candidate approval queue is used.')
        # Event topics are normalized automatically by GenAI; the old TF-IDF approval queue
        # is deliberately not run because the admin no longer needs a moderation step.
        totals['topic_candidates'] = 0
        stage_started = time.monotonic()
        refresh_facts()
        log.info('Stage FACT refresh complete; elapsed=%.1fs', time.monotonic()-stage_started)
        # Send a compact Telegram digest from the same pipeline run. This is optional and
        # must never make successful ingestion fail if Telegram is not configured/unavailable.
        stage_started = time.monotonic()
        try:
            from .telegram_notifications import send_telegram_digests, cleanup_old_delivery_markers
            totals['telegram_notifications'] = send_telegram_digests()
            if totals['telegram_notifications'].get('status') != 'skipped_missing_token':
                try:
                    totals['telegram_delivery_markers_purged'] = cleanup_old_delivery_markers()
                except Exception:
                    log.exception('Telegram delivery-marker cleanup failed; continuing pipeline.')
            log.info('Stage Telegram digest complete: %s; elapsed=%.1fs', totals['telegram_notifications'], time.monotonic()-stage_started)
        except Exception as exc:
            totals['telegram_notifications'] = {'status': 'failed_non_blocking', 'error': f'{type(exc).__name__}: {str(exc)[:240]}'}
            log.exception('Telegram notification stage failed; ingestion will continue.')
        # Raw capture is temporary: after facts are persisted for every clean article,
        # purge RSS payloads to conserve the Supabase database quota.
        totals['raw_rows_purged'] = purge_raw_feed_items_after_fact_refresh()
        status='success' if not totals['source_errors'] else ('partial' if totals['articles_inserted']+totals['articles_updated']>0 else 'failed')
        end=datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""update pipeline_runs set status=%s,finished_at=now(),source_count=%s,entries_seen=%s,articles_inserted=%s,articles_updated=%s,entries_skipped_existing=%s,feeds_not_modified=%s,entities_found=%s,entity_candidates=%s,topic_candidates=%s,quality_issues=%s,message=%s,details=%s::jsonb where run_id=%s""",
                (status,totals['sources'],totals['entries_seen'],totals['articles_inserted'],totals['articles_updated'],totals['entries_skipped_existing'],totals['feeds_not_modified'],totals['entity_mentions'],totals['new_entity_mentions'],totals['topic_candidates'],totals['quality_issues'],f"Completed in {(end-started).total_seconds():.1f}s; {len(totals['source_errors'])} source error(s).",json.dumps(totals,ensure_ascii=False),run_id))
        log.info('Pipeline run %s finished in %.1fs: %s',run_id,time.monotonic()-run_clock,totals)
        return totals
    except Exception as exc:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("update pipeline_runs set status='failed',finished_at=now(),message=%s,details=%s::jsonb where run_id=%s",(f'{type(exc).__name__}: {str(exc)[:700]}',json.dumps(totals,ensure_ascii=False),run_id))
        raise

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s | %(message)s')
    result=run_pipeline('scheduled')
    print(json.dumps(result,ensure_ascii=False,indent=2))
