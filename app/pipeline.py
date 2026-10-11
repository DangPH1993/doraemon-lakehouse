"""Daily RSS -> Raw/Clean -> topics/entities -> FACTs pipeline."""
from __future__ import annotations
import calendar, hashlib, json, logging, re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import feedparser
import requests
from dateutil import parser as date_parser
from psycopg.types.json import Jsonb
from .config import settings
from .db import get_conn
from .processing import (apply_cleaning_rules, canonicalize_url, clean_html, extract_entities,
                         hash_text, keyword_matches, norm_name, normalize_space, slugify)

log = logging.getLogger('mini-lakehouse.pipeline')
USER_AGENT = 'DoraemonMiniLakehouse/1.0 (+RSS feed reader; contact admin of the configured source)'

def parse_date(entry: dict):
    for key in ('published_parsed','updated_parsed','created_parsed'):
        val = entry.get(key)
        if val:
            try: return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError): pass
    for key in ('published','updated','created','date'):
        val = entry.get(key)
        if val:
            try:
                dt = date_parser.parse(str(val))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception: pass
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

def process_entities(conn, article_id, source_id, title, summary):
    found = get_spacy_entity_matches(title, summary)
    with conn.cursor() as cur:
        cur.execute("select entity_id, canonical_name, normalized_name, entity_type from entities where status='active'")
        known = cur.fetchall()
        cur.execute('select entity_id, normalized_alias from entity_aliases')
        aliases = cur.fetchall()
    alias_to_entity = {r['normalized_alias']: r['entity_id'] for r in aliases}
    known_map = {(r['normalized_name'], r['entity_type']): r['entity_id'] for r in known}
    known_name_fallback = {r['normalized_name']: r['entity_id'] for r in known}
    count_known = 0
    for mention, typ, confidence in found:
        normalized = norm_name(mention)
        if len(normalized) < 2: continue
        entity_id = known_map.get((normalized, typ)) or alias_to_entity.get(normalized) or known_name_fallback.get(normalized)
        if entity_id:
            with conn.cursor() as cur:
                cur.execute("""insert into article_entities(article_id,entity_id,mention_text,confidence) values(%s,%s,%s,%s) on conflict do nothing""", (article_id,entity_id,mention,confidence))
            count_known += 1
            continue
        # Unknown entities are candidates; nothing is silently added to the approved catalog.
        with conn.cursor() as cur:
            cur.execute("""insert into entity_candidates(canonical_name,normalized_name,entity_type,status)
                           values(%s,%s,%s,'pending')
                           on conflict(normalized_name,entity_type) do update set last_seen_at=now()
                           returning candidate_id,status""", (mention,normalized,typ))
            cand = cur.fetchone()
            if cand['status'] != 'pending': continue
            cur.execute("""insert into entity_candidate_occurrences(candidate_id,article_id,source_id,mention_text)
                           values(%s,%s,%s,%s) on conflict do nothing""", (cand['candidate_id'],article_id,source_id,mention))
    return len(found), count_known

def apply_topics(conn, article_id, title, summary):
    text = f'{title} {summary}'
    with conn.cursor() as cur:
        cur.execute("select topic_id, keywords from topics where status='active'")
        topic_rows = cur.fetchall()
        matched = 0
        for topic in topic_rows:
            kws = topic['keywords'] or []
            hits = keyword_matches(text,kws)
            if not hits: continue
            confidence = min(0.99, 0.65 + 0.08 * min(len(hits),4))
            cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                           values(%s,%s,%s,'keyword') on conflict(article_id,topic_id) do update set confidence=greatest(article_topics.confidence,excluded.confidence)""", (article_id,topic['topic_id'],confidence))
            matched += 1
        return matched

def process_source(source: dict, rules: list[dict]) -> dict:
    result = {'seen':0,'inserted':0,'updated':0,'entity_mentions':0,'new_entities':0,'quality_issues':0,'error':None}
    resp = requests.get(source['feed_url'], timeout=(10, settings.request_timeout_seconds), headers={'User-Agent':USER_AGENT})
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f'RSS parsing failed: {str(parsed.bozo_exception)[:300]}')
    entries = parsed.entries[:settings.max_feed_entries_per_source]
    if not entries:
        raise RuntimeError('RSS feed contains no entries')
    with get_conn() as conn:
        for entry in entries:
            result['seen'] += 1
            title = normalize_space(clean_html(entry.get('title','')))
            raw_url = str(entry.get('link') or '').strip()
            canon = canonicalize_url(raw_url)
            if not title or not canon:
                result['quality_issues'] += 1
                continue
            summary, content_text = extract_feed_content(entry)
            author = normalize_space(str(entry.get('author') or ''))[:250] or None
            pub_date = parse_date(entry)
            external_id = str(entry.get('id') or entry.get('guid') or '')[:1000] or None
            entry_key = external_id or canon or hash_text(title)
            payload = {str(k): entry.get(k) for k in entry.keys()}
            payload = json.loads(json.dumps(payload, default=str))
            with conn.cursor() as cur:
                cur.execute("""insert into raw_feed_items(source_id,entry_key,item_url,raw_payload,fetched_at)
                               values(%s,%s,%s,%s::jsonb,now())
                               on conflict(source_id,entry_key) do update set item_url=excluded.item_url,raw_payload=excluded.raw_payload,fetched_at=now()
                               returning raw_id""",(source['source_id'],entry_key,raw_url,json.dumps(payload,ensure_ascii=False)))
                raw_id = cur.fetchone()['raw_id']
                cur.execute("select article_id from news_articles where source_id=%s and canonical_url=%s",(source['source_id'],canon))
                existing = cur.fetchone()
                article_id = existing['article_id'] if existing else None
            excluded, reason = apply_cleaning_rules(title,canon,source['source_id'],rules)
            quality = 100
            if not pub_date: quality -= 10
            if len(title) < 15: quality -= 15
            if len(content_text) < 80: quality -= 20
            if not author: quality -= 5
            with conn.cursor() as cur:
                cur.execute("""insert into news_articles(source_id,raw_id,external_id,url,canonical_url,title,summary,content_text,author,language,published_at,headline_hash,content_hash,is_excluded,exclude_reason,quality_score)
                               values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               on conflict(source_id,canonical_url) do update set raw_id=excluded.raw_id,external_id=excluded.external_id,url=excluded.url,title=excluded.title,summary=excluded.summary,content_text=excluded.content_text,author=excluded.author,language=excluded.language,published_at=coalesce(excluded.published_at,news_articles.published_at),updated_at=now(),is_excluded=excluded.is_excluded,exclude_reason=excluded.exclude_reason,quality_score=excluded.quality_score
                               returning article_id""",(source['source_id'],raw_id,external_id,raw_url,canon,title,summary,content_text,author,source.get('language') or 'en',pub_date,hash_text(title),hash_text(content_text) if content_text else None,excluded,reason, max(0,quality)))
                article_id = cur.fetchone()['article_id']
            if existing: result['updated'] += 1
            else: result['inserted'] += 1
            if not pub_date:
                add_quality_issue(conn,article_id,source['source_id'],'missing_published_at','warning','Feed entry has no parseable publication timestamp.')
                result['quality_issues'] += 1
            if len(title) < 15:
                add_quality_issue(conn,article_id,source['source_id'],'short_title','warning',f'Title length is {len(title)} characters.')
                result['quality_issues'] += 1
            if len(content_text) < 80:
                add_quality_issue(conn,article_id,source['source_id'],'short_or_missing_content','warning',f'RSS content text contains only {len(content_text)} characters.')
                result['quality_issues'] += 1
            # Record near-exact duplicate headlines across other rows; keep raw evidence, don't auto-delete.
            with conn.cursor() as cur:
                cur.execute("select article_id,source_id from news_articles where headline_hash=%s and article_id<>%s limit 1",(hash_text(title),article_id))
                duplicate = cur.fetchone()
            if duplicate:
                add_quality_issue(conn,article_id,source['source_id'],'duplicate_headline','info',f'Exact normalized headline also exists in article {duplicate["article_id"]} from source {duplicate["source_id"]}.')
                result['quality_issues'] += 1
            if not excluded:
                matched_topics = apply_topics(conn,article_id,title,summary)
                found_count, known_count = process_entities(conn,article_id,source['source_id'],title,summary)
                result['entity_mentions'] += found_count
                result['new_entities'] += max(0,found_count-known_count)
        with conn.cursor() as cur:
            cur.execute("update data_sources set last_run_at=now(),last_status='success',last_error=null,updated_at=now() where source_id=%s",(source['source_id'],))
    return result

def discover_topic_candidates():
    """Cluster recent currently-unassigned articles; treat results as candidates, never auto-approve."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.cluster import DBSCAN
        import numpy as np
    except Exception as exc:
        log.warning('Topic discovery skipped; scikit-learn unavailable: %s',exc)
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""select a.article_id,a.source_id,a.title,a.summary from news_articles a
                       where a.is_excluded=false and coalesce(a.published_at,a.collected_at) >= now() - (%s || ' days')::interval
                       and not exists (select 1 from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id and t.status='active')
                       order by a.collected_at desc limit 1200""",(settings.pipeline_lookback_days,))
        articles = cur.fetchall()
        cur.execute("select keywords from topics where status='active'")
        known_kw = [kw for r in cur.fetchall() for kw in (r['keywords'] or [])]
    if len(articles) < 5: return 0
    texts = [f"{a['title']} {a['summary']}".strip() for a in articles]
    try:
        vectorizer = TfidfVectorizer(stop_words='english',ngram_range=(1,2),min_df=2,max_features=3500,token_pattern=r'(?u)\b[a-zA-Z][a-zA-Z0-9-]{2,}\b', sublinear_tf=True)
        matrix = vectorizer.fit_transform(texts)
        if matrix.shape[1] < 2: return 0
        labels = DBSCAN(eps=0.72,min_samples=3,metric='cosine').fit_predict(matrix)
        terms = vectorizer.get_feature_names_out()
    except Exception as exc:
        log.warning('Topic clustering skipped: %s',exc)
        return 0
    discovered = 0
    for label in set(labels.tolist()):
        if label < 0: continue
        indices = [i for i,v in enumerate(labels) if v == label]
        if len(indices) < 3: continue
        cluster_sources = {articles[i]['source_id'] for i in indices}
        if len(cluster_sources) < 2 and len(indices) < 5: continue
        weights = np.asarray(matrix[indices].sum(axis=0)).ravel()
        top_idx = weights.argsort()[::-1]
        keywords=[]
        for term in terms[top_idx]:
            if term not in keywords and len(term) > 2:
                keywords.append(str(term))
            if len(keywords) >= 6: break
        if not keywords: continue
        combined = ' '.join(texts[i] for i in indices)
        # Don't create a novelty candidate if an active topic's keywords explain the cluster.
        if len(keyword_matches(combined,known_kw)) >= 2: continue
        name = ' '.join(x.title() for x in keywords[:3])
        cluster_key = hash_text('|'.join(sorted(keywords[:5])))
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""insert into topic_candidates(cluster_key,suggested_name,keywords,status) values(%s,%s,%s,'pending')
                               on conflict(cluster_key) do update set last_seen_at=now()
                               returning candidate_id,status""",(cluster_key,name,keywords))
                row = cur.fetchone()
                if row['status'] != 'pending': continue
                for idx in indices:
                    a=articles[idx]
                    cur.execute("""insert into topic_candidate_occurrences(candidate_id,article_id,source_id) values(%s,%s,%s) on conflict do nothing""",(row['candidate_id'],a['article_id'],a['source_id']))
                discovered += 1
    return discovered

def refresh_facts():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""insert into fact_news_article(article_id,source_id,published_date,title_length,word_count,detected_entity_count,detected_topic_count,quality_score,is_excluded,loaded_at)
                       select a.article_id,a.source_id,
                         coalesce((a.published_at at time zone %s)::date,(a.collected_at at time zone %s)::date),
                         char_length(a.title),
                         case when trim(a.content_text)='' then 0 else cardinality(regexp_split_to_array(trim(a.content_text),'\\s+')) end,
                         (select count(*) from article_entities ae where ae.article_id=a.article_id),
                         (select count(*) from article_topics at where at.article_id=a.article_id),
                         a.quality_score,a.is_excluded,now()
                       from news_articles a
                       on conflict(article_id) do update set source_id=excluded.source_id,published_date=excluded.published_date,title_length=excluded.title_length,word_count=excluded.word_count,detected_entity_count=excluded.detected_entity_count,detected_topic_count=excluded.detected_topic_count,quality_score=excluded.quality_score,is_excluded=excluded.is_excluded,loaded_at=now()""",(settings.timezone,settings.timezone))
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

def run_pipeline(trigger_type='scheduled', source_ids: list[int] | None = None) -> dict:
    started = datetime.now(timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("insert into pipeline_runs(trigger_type,status,started_at) values(%s,'running',now()) returning run_id",(trigger_type,))
        run_id = cur.fetchone()['run_id']
        cur.execute('select * from data_sources where enabled=true order by source_id')
        sources=cur.fetchall()
        cur.execute('select * from cleaning_rules where enabled=true order by rule_id')
        rules=cur.fetchall()
    if source_ids is not None:
        sources=[s for s in sources if s['source_id'] in source_ids]
    totals={'run_id':run_id,'sources':len(sources),'entries_seen':0,'articles_inserted':0,'articles_updated':0,'entity_mentions':0,'new_entity_mentions':0,'topic_candidates':0,'quality_issues':0,'source_errors':[]}
    try:
        for source in sources:
            try:
                stats=process_source(source,rules)
                for key in ('seen','inserted','updated','entity_mentions','new_entities','quality_issues'):
                    mapkey={'seen':'entries_seen','inserted':'articles_inserted','updated':'articles_updated','new_entities':'new_entity_mentions'}.get(key,key)
                    totals[mapkey]+=stats[key]
            except Exception as exc:
                msg=f'{type(exc).__name__}: {str(exc)[:400]}'
                log.exception('Source %s failed: %s', source['name'], msg)
                totals['source_errors'].append({'source_id':source['source_id'],'name':source['name'],'error':msg})
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("update data_sources set last_run_at=now(),last_status='error',last_error=%s,updated_at=now() where source_id=%s",(msg,source['source_id']))
        totals['topic_candidates']=discover_topic_candidates()
        refresh_facts()
        status='success' if not totals['source_errors'] else ('partial' if totals['articles_inserted']+totals['articles_updated']>0 else 'failed')
        end=datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""update pipeline_runs set status=%s,finished_at=now(),source_count=%s,entries_seen=%s,articles_inserted=%s,articles_updated=%s,entities_found=%s,entity_candidates=%s,topic_candidates=%s,quality_issues=%s,message=%s,details=%s::jsonb where run_id=%s""",
                (status,totals['sources'],totals['entries_seen'],totals['articles_inserted'],totals['articles_updated'],totals['entity_mentions'],totals['new_entity_mentions'],totals['topic_candidates'],totals['quality_issues'],f"Completed in {(end-started).total_seconds():.1f}s; {len(totals['source_errors'])} source error(s).",json.dumps(totals,ensure_ascii=False),run_id))
        log.info('Pipeline run %s finished: %s',run_id,totals)
        return totals
    except Exception as exc:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("update pipeline_runs set status='failed',finished_at=now(),message=%s,details=%s::jsonb where run_id=%s",(f'{type(exc).__name__}: {str(exc)[:700]}',json.dumps(totals,ensure_ascii=False),run_id))
        raise

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s | %(message)s')
    result=run_pipeline('scheduled')
    print(json.dumps(result,ensure_ascii=False,indent=2))
