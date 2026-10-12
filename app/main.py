from __future__ import annotations
import json, logging, threading
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, Depends, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from .config import settings
from .db import get_conn, fetch_all, fetch_one
from .security import issue_token, require_admin
from .processing import canonicalize_url, hash_text, norm_name, slugify

logging.basicConfig(level=logging.INFO)
log=logging.getLogger('mini-lakehouse.api')
app=FastAPI(title='Doraemon Mini Lakehouse',version='0.4.0',description='Admin portal for RSS ingestion, catalog, data quality and analytics.')
app.add_middleware(CORSMiddleware,allow_origins=list(settings.cors_origins) or ['*'],allow_credentials=False,allow_methods=['GET','POST','PATCH','DELETE','OPTIONS'],allow_headers=['Authorization','Content-Type'])
app.mount('/static',StaticFiles(directory='static'),name='static')
_pipeline_thread_lock=threading.Lock()

class LoginBody(BaseModel):
    username: str
    password: str
class SourceBody(BaseModel):
    name: str = Field(min_length=2,max_length=180)
    feed_url: str = Field(min_length=8,max_length=2000)
    language: str = Field(default='en',max_length=12)
    category: str = Field(default='general',max_length=100)
    license_name: str = Field(default='check_required',max_length=180)
    license_url: str | None = None
    enabled: bool = True
    @field_validator('feed_url')
    @classmethod
    def valid_url(cls,v):
        p=urlparse(v)
        if p.scheme not in ('http','https') or not p.netloc: raise ValueError('feed_url must be a valid http(s) URL')
        return v.strip()
class TopicBody(BaseModel):
    name: str = Field(min_length=2,max_length=180)
    description: str = Field(default='',max_length=2000)
    keywords: list[str] = Field(default_factory=list,max_length=100)
    topic_type: str = 'category'
    status: str = 'active'
class EntityBody(BaseModel):
    canonical_name: str = Field(min_length=2,max_length=180)
    entity_type: str = Field(default='UNKNOWN',max_length=40)
    external_id: str | None = None
    description: str = Field(default='',max_length=2000)
    aliases: list[str] = Field(default_factory=list,max_length=100)
class ArticleClassificationBody(BaseModel):
    topic_names: list[str] = Field(default_factory=list, max_length=30)
    entity_names: list[str] = Field(default_factory=list, max_length=30)

class RuleBody(BaseModel):
    name: str = Field(min_length=2,max_length=180)
    rule_type: str
    config: dict = Field(default_factory=dict)
    enabled: bool = True

@app.get('/')
def index(): return FileResponse('static/index.html')
@app.get('/health')
def health(): return {'status':'ok','app':'doraemon-mini-lakehouse','version':'0.4.0'}
@app.get('/api/health/ready')
def ready():
    try:
        row=fetch_one('select now() as database_time')
        return {'status':'ready','database':str(row['database_time'])}
    except Exception as exc:
        raise HTTPException(status_code=503,detail=f'Database unavailable: {str(exc)[:180]}')
@app.post('/api/auth/login')
def login(body: LoginBody):
    import hmac
    if not settings.admin_password or not settings.session_secret:
        raise HTTPException(status_code=503,detail='Set ADMIN_PASSWORD and SESSION_SECRET environment variables.')
    if not hmac.compare_digest(body.username,settings.admin_username) or not hmac.compare_digest(body.password,settings.admin_password):
        raise HTTPException(status_code=401,detail='Invalid username or password')
    return {'token':issue_token(body.username),'username':body.username,'expires_in':28800}
@app.get('/api/auth/me',dependencies=[Depends(require_admin)])
def me(): return {'username':settings.admin_username,'role':'admin'}

@app.get('/api/dashboard',dependencies=[Depends(require_admin)])
def dashboard():
    counts=fetch_one("""select
      (select count(*) from data_sources) as sources_total,
      (select count(*) from data_sources where enabled) as sources_enabled,
      (select count(*) from news_articles) as articles_total,
      (select count(*) from news_articles where collected_at >= now()-interval '24 hours') as articles_24h,
      (select count(*) from topics where status='active') as topics_active,
      (select count(*) from topics where status='active' and topic_type='event') as event_topics_active,
      (select count(*) from entities where status='active') as entities_active,
      (select count(*) from news_articles where ai_classified_at is null and is_excluded=false and coalesce(published_at,collected_at)>=now()-interval '30 days') as ai_pending_articles,
      (select count(*) from news_articles where ai_classification_error is not null and is_excluded=false) as ai_classification_errors,
      (select count(*) from data_quality_issues where resolved_at is null) as open_issues,
      (select count(*) from pipeline_runs where status in ('failed','partial') and started_at >= now()-interval '7 days') as failed_runs""")
    trend=fetch_all("""select to_char(date_trunc('day',a.collected_at),'YYYY-MM-DD') as day, count(*) as count
                      from news_articles a where a.collected_at >= now()-interval '14 days' and a.is_excluded=false
                      group by 1 order by 1""")
    top_topics=fetch_all("""select t.topic_id,t.name,sum(f.article_count)::int as article_count
                          from fact_topic_daily f join topics t on t.topic_id=f.topic_id
                          where f.stat_date >= (current_date - 6) and t.status='active' and t.topic_type='event'
                          group by t.topic_id,t.name order by article_count desc limit 10""")
    top_sources=fetch_all("""select s.source_id,s.name,count(a.article_id)::int as article_count
                           from data_sources s left join news_articles a on a.source_id=s.source_id and a.collected_at>=now()-interval '7 days'
                           group by s.source_id,s.name order by article_count desc limit 8""")
    recent_runs=fetch_all('select run_id,trigger_type,status,started_at,finished_at,entries_seen,articles_inserted,articles_updated,message from pipeline_runs order by started_at desc limit 5')
    hot_topics=fetch_all("""WITH clock AS (SELECT (now() at time zone %s)::date AS today), periods AS (
      SELECT 'day'::text AS period_type, today AS period_start, 'Hôm nay'::text AS period_label FROM clock
      UNION ALL SELECT 'week', date_trunc('week',today::timestamp)::date, 'Tuần này' FROM clock
      UNION ALL SELECT 'month', date_trunc('month',today::timestamp)::date, 'Tháng này' FROM clock
    )
    SELECT p.period_type,p.period_start,p.period_label,t.topic_id,t.name AS topic_name,
           f.article_count,f.source_count,f.previous_article_count,f.growth_pct,f.hotness_score,f.rank_position
    FROM periods p
    LEFT JOIN fact_topic_trending f ON f.period_type=p.period_type AND f.period_start=p.period_start
    LEFT JOIN topics t ON t.topic_id=f.topic_id AND t.status='active' AND t.topic_type='event'
    WHERE f.topic_id IS NOT NULL AND t.topic_id IS NOT NULL
    ORDER BY CASE p.period_type WHEN 'day' THEN 1 WHEN 'week' THEN 2 ELSE 3 END, f.rank_position
    """, (settings.timezone,))
    return {'counts':counts,'collection_trend':trend,'top_topics':top_topics,'top_sources':top_sources,'recent_runs':recent_runs,'hot_topics':hot_topics}

@app.get('/api/sources',dependencies=[Depends(require_admin)])
def list_sources():
    return fetch_all("""select s.*, (select count(*) from news_articles a where a.source_id=s.source_id) as article_count
                       from data_sources s order by s.source_id""")
@app.post('/api/sources',dependencies=[Depends(require_admin)])
def add_source(body: SourceBody):
    try:
        return fetch_one("""insert into data_sources(name,feed_url,language,category,license_name,license_url,enabled)
                             values(%s,%s,%s,%s,%s,%s,%s) returning *""",(body.name.strip(),body.feed_url,body.language,body.category,body.license_name,body.license_url,body.enabled))
    except Exception as exc:
        if 'unique' in str(exc).lower(): raise HTTPException(status_code=409,detail='This feed URL already exists.')
        raise HTTPException(status_code=500,detail=str(exc)[:300])
@app.patch('/api/sources/{source_id}',dependencies=[Depends(require_admin)])
def update_source(source_id:int, patch:dict=Body(...)):
    allowed={'name','feed_url','language','category','license_name','license_url','enabled'}
    fields={k:v for k,v in patch.items() if k in allowed}
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields to update')
    if 'feed_url' in fields and not canonicalize_url(fields['feed_url']): raise HTTPException(status_code=400,detail='Invalid feed URL')
    fields['updated_at']=None
    sets=[]; vals=[]
    for key,value in fields.items():
        if key=='updated_at': sets.append('updated_at=now()')
        else: sets.append(f'{key}=%s'); vals.append(value)
    vals.append(source_id)
    row=fetch_one(f"update data_sources set {', '.join(sets)} where source_id=%s returning *",vals)
    if not row: raise HTTPException(status_code=404,detail='Source not found')
    return row
@app.delete('/api/sources/{source_id}',dependencies=[Depends(require_admin)])
def delete_source(source_id:int):
    row=fetch_one('delete from data_sources where source_id=%s returning source_id',(source_id,))
    if not row: raise HTTPException(status_code=404,detail='Source not found')
    return {'deleted':row['source_id']}
@app.post('/api/sources/{source_id}/test',dependencies=[Depends(require_admin)])
def test_source(source_id:int):
    source=fetch_one('select * from data_sources where source_id=%s',(source_id,))
    if not source: raise HTTPException(status_code=404,detail='Source not found')
    try:
        r=requests.get(source['feed_url'],timeout=(8,15),headers={'User-Agent':'DoraemonMiniLakehouse/0.1 RSS validator'})
        r.raise_for_status()
        import feedparser
        feed=feedparser.parse(r.content)
        return {'ok':bool(feed.entries),'http_status':r.status_code,'entry_count':len(feed.entries),'feed_title':getattr(feed.feed,'title','') or '', 'sample_titles':[str(e.get('title','')) for e in feed.entries[:3]],'warning':str(feed.bozo_exception)[:200] if feed.bozo else None}
    except Exception as exc: return {'ok':False,'error':f'{type(exc).__name__}: {str(exc)[:300]}'}

@app.get('/api/articles',dependencies=[Depends(require_admin)])
def list_articles(q:str|None=None,source_id:int|None=None,excluded:bool|None=None,limit:int=Query(default=100,ge=1,le=500),offset:int=Query(default=0,ge=0)):
    where=[]; vals=[]
    if q:
        where.append('(a.title ilike %s or a.summary ilike %s or a.url ilike %s)'); term=f'%{q[:150]}%'; vals.extend([term,term,term])
    if source_id is not None: where.append('a.source_id=%s'); vals.append(source_id)
    if excluded is not None: where.append('a.is_excluded=%s'); vals.append(excluded)
    sql="""select a.article_id,a.title,a.url,a.canonical_url,a.summary,left(a.content_text,500) as content_preview,a.author,a.language,a.published_at,a.collected_at,a.quality_score,a.is_excluded,a.exclude_reason,s.name as source_name,
           coalesce((select string_agg(t.name,', ' order by t.name) from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id),'') as topics,
           coalesce((select string_agg(e.canonical_name,', ' order by e.canonical_name) from article_entities ae join entities e on e.entity_id=ae.entity_id where ae.article_id=a.article_id),'') as entities,
           coalesce((select json_agg(t.name order by t.name) from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id),'[]'::json) as topic_names,
           coalesce((select json_agg(e.canonical_name order by e.canonical_name) from article_entities ae join entities e on e.entity_id=ae.entity_id where ae.article_id=a.article_id),'[]'::json) as entity_names
           from news_articles a join data_sources s on s.source_id=a.source_id"""
    if where: sql+=' where '+' and '.join(where)
    sql+=' order by a.collected_at desc limit %s offset %s'; vals.extend([limit,offset])
    return fetch_all(sql,vals)

@app.get('/api/articles/{article_id}/classifications', dependencies=[Depends(require_admin)])
def get_article_classifications(article_id: int):
    article = fetch_one("select article_id,title from news_articles where article_id=%s", (article_id,))
    if not article:
        raise HTTPException(status_code=404, detail='Article not found')
    topics = fetch_all("""select t.topic_id,t.name,t.topic_type from article_topics at
                         join topics t on t.topic_id=at.topic_id where at.article_id=%s order by t.name""", (article_id,))
    entities = fetch_all("""select distinct e.entity_id,e.canonical_name,e.entity_type from article_entities ae
                           join entities e on e.entity_id=ae.entity_id where ae.article_id=%s order by e.canonical_name""", (article_id,))
    return {'article': article, 'topics': topics, 'entities': entities}

@app.patch('/api/articles/{article_id}/classifications', dependencies=[Depends(require_admin)])
def patch_article_classifications(article_id: int, body: ArticleClassificationBody):
    article = fetch_one("select article_id,title from news_articles where article_id=%s", (article_id,))
    if not article:
        raise HTTPException(status_code=404, detail='Article not found')
    topic_names = list(dict.fromkeys(str(x).strip()[:180] for x in body.topic_names if str(x).strip()))
    entity_names = list(dict.fromkeys(str(x).strip()[:180] for x in body.entity_names if str(x).strip()))
    topic_ids: list[int] = []
    entity_records: list[tuple[int, str]] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select topic_id,name,slug,event_key,topic_type from topics where status='active'")
        all_topics = cur.fetchall()
        by_topic_norm = {norm_name(t['name']): t for t in all_topics}
        by_slug = {t['slug']: t for t in all_topics}
        for name in topic_names:
            normalized = norm_name(name)
            if not normalized:
                continue
            topic = by_topic_norm.get(normalized)
            if not topic:
                event_key = 'admin:' + hash_text(normalized)[:32]
                slug = 'event-' + slugify(name)
                existing_slug = by_slug.get(slug)
                if existing_slug and norm_name(existing_slug['name']) != normalized:
                    slug = f"{slug[:155]}-{hash_text(normalized)[:8]}"
                cur.execute("""insert into topics(slug,event_key,name,description,keywords,topic_type,status,origin)
                               values(%s,%s,%s,'Topic được tạo/chỉnh trực tiếp từ phân loại bài báo.',%s,'event','active','manual')
                               on conflict(event_key) do update set name=excluded.name,updated_at=now()
                               returning topic_id,name,slug,event_key,topic_type""",
                            (slug,event_key,name,[name]))
                topic = cur.fetchone()
                all_topics.append(topic); by_topic_norm[norm_name(topic['name'])] = topic; by_slug[topic['slug']] = topic
            if topic['topic_id'] not in topic_ids:
                topic_ids.append(topic['topic_id'])
        cur.execute("select entity_id,canonical_name,normalized_name,entity_type from entities where status='active'")
        all_entities = cur.fetchall()
        by_entity_norm = {norm_name(e['canonical_name']): e for e in all_entities}
        cur.execute("select entity_id,alias,normalized_alias from entity_aliases")
        alias_rows = cur.fetchall()
        by_alias = {r['normalized_alias']: r for r in alias_rows}
        for name in entity_names:
            normalized = norm_name(name)
            if not normalized:
                continue
            entity = by_entity_norm.get(normalized)
            if not entity and normalized in by_alias:
                alias_entity_id = by_alias[normalized]['entity_id']
                entity = next((e for e in all_entities if e['entity_id'] == alias_entity_id), None)
            if not entity:
                cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,description,origin)
                               values(%s,%s,'UNKNOWN','Entity được tạo/chỉnh trực tiếp từ phân loại bài báo.','manual')
                               on conflict(normalized_name,entity_type) do update set updated_at=now()
                               returning entity_id,canonical_name,normalized_name,entity_type""", (name,normalized))
                entity = cur.fetchone()
                cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                               values(%s,%s,%s) on conflict do nothing""", (entity['entity_id'],name,normalized))
                all_entities.append(entity); by_entity_norm[normalized] = entity
            if not any(eid == entity['entity_id'] for eid, _ in entity_records):
                entity_records.append((entity['entity_id'], name))
        cur.execute('delete from article_topics where article_id=%s', (article_id,))
        cur.execute('delete from article_entities where article_id=%s', (article_id,))
        for topic_id in topic_ids:
            cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                           values(%s,%s,1.0,'admin') on conflict do nothing""", (article_id,topic_id))
        for entity_id, mention in entity_records:
            cur.execute("""insert into article_entities(article_id,entity_id,mention_text,confidence)
                           values(%s,%s,%s,1.0) on conflict do nothing""", (article_id,entity_id,mention))
        cur.execute("""update news_articles set ai_classified_at=now(),ai_classification='{"manual_override":true}'::jsonb,
                       ai_classification_error=null,event_topic_scanned_at=now(),updated_at=now() where article_id=%s""", (article_id,))
    try:
        from .pipeline import refresh_facts
        refresh_facts()
    except Exception as exc:
        log.warning('FACT refresh after article classification edit failed: %s', exc)
    return {'article_id': article_id, 'topic_ids': topic_ids, 'entity_ids': [e[0] for e in entity_records], 'saved': True}

@app.get('/api/topics/{topic_id}/timeline', dependencies=[Depends(require_admin)])
def topic_timeline(topic_id: int, limit: int = Query(default=100, ge=1, le=300)):
    topic = fetch_one("select topic_id,name,description,topic_type,parent_topic_id from topics where topic_id=%s", (topic_id,))
    if not topic:
        raise HTTPException(status_code=404, detail='Topic not found')
    rows = fetch_all("""select recent.article_id,recent.title,recent.url,recent.summary,recent.author,
                             recent.source_name,recent.published_at,recent.collected_at,recent.timeline_at,
                             coalesce((select string_agg(e.canonical_name,', ' order by e.canonical_name)
                                       from article_entities ae join entities e on e.entity_id=ae.entity_id
                                       where ae.article_id=recent.article_id),'') as entities
                      from (
                        select a.article_id,a.title,a.url,a.summary,a.author,s.name as source_name,
                               a.published_at,a.collected_at,coalesce(a.published_at,a.collected_at) as timeline_at
                        from article_topics at
                        join news_articles a on a.article_id=at.article_id
                        join data_sources s on s.source_id=a.source_id
                        where at.topic_id=%s and a.is_excluded=false
                        order by coalesce(a.published_at,a.collected_at) desc
                        limit %s
                      ) recent
                      order by recent.timeline_at asc""", (topic_id, limit))
    total = fetch_one('select count(*)::int as total from article_topics at join news_articles a on a.article_id=at.article_id where at.topic_id=%s and a.is_excluded=false', (topic_id,))
    return {'topic': topic, 'count': len(rows), 'total_count': (total or {}).get('total', len(rows)), 'items': rows}

@app.get('/api/raw-items',dependencies=[Depends(require_admin)])
def list_raw_items(q:str|None=None,source_id:int|None=None,limit:int=Query(default=100,ge=1,le=500)):
    where=[]; vals=[]
    if q:
        where.append("(r.raw_payload->>'title' ilike %s or r.item_url ilike %s)")
        term=f'%{q[:150]}%'; vals.extend([term,term])
    if source_id is not None: where.append('r.source_id=%s'); vals.append(source_id)
    sql="""select r.raw_id,r.entry_key,r.item_url,r.fetched_at,s.name as source_name,
               coalesce(r.raw_payload->>'title','') as raw_title,
               coalesce(r.raw_payload->>'published','') as raw_published,
               coalesce(r.raw_payload->>'summary',r.raw_payload->>'description','') as raw_summary
               from raw_feed_items r join data_sources s on s.source_id=r.source_id"""
    if where: sql+=' where '+' and '.join(where)
    sql+=' order by r.fetched_at desc limit %s'; vals.append(limit)
    return fetch_all(sql,vals)

@app.get('/api/topics',dependencies=[Depends(require_admin)])
def list_topics():
    topics=fetch_all("""select t.*, (select count(*) from article_topics at where at.topic_id=t.topic_id) as article_count
                       from topics t order by t.status, t.name""")
    # Topic discovery is automatic; no manual approval queue is exposed in the admin UI.
    return {'topics':topics,'candidates':[]}
@app.post('/api/topics',dependencies=[Depends(require_admin)])
def create_topic(body:TopicBody):
    if body.status not in {'active','archived'}: raise HTTPException(status_code=400,detail='status must be active or archived')
    if body.topic_type not in {'category','event'}: raise HTTPException(status_code=400,detail='topic_type must be category or event')
    slug=slugify(body.name)
    row=fetch_one("""insert into topics(slug,name,description,keywords,topic_type,status,origin) values(%s,%s,%s,%s,%s,%s,'manual') returning *""",(slug,body.name.strip(),body.description, [x.strip() for x in body.keywords if x.strip()],body.topic_type,body.status))
    return row
@app.patch('/api/topics/{topic_id}',dependencies=[Depends(require_admin)])
def patch_topic(topic_id:int,patch:dict=Body(...)):
    allowed={'name','description','keywords','status','topic_type'}; fields={k:v for k,v in patch.items() if k in allowed}
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields')
    if 'status' in fields and fields['status'] not in {'active','archived'}: raise HTTPException(status_code=400,detail='Invalid status')
    if 'topic_type' in fields and fields['topic_type'] not in {'category','event'}: raise HTTPException(status_code=400,detail='topic_type must be category or event')
    if 'name' in fields: fields['slug']=slugify(fields['name'])
    sets=[]; vals=[]
    for k,v in fields.items(): sets.append(f'{k}=%s'); vals.append(v)
    sets.append('updated_at=now()'); vals.append(topic_id)
    try: row=fetch_one(f"update topics set {', '.join(sets)} where topic_id=%s returning *",vals)
    except Exception as exc: raise HTTPException(status_code=409,detail=f'Could not update topic: {str(exc)[:200]}')
    if not row: raise HTTPException(status_code=404,detail='Topic not found')
    return row
@app.post('/api/topic-candidates/{candidate_id}/approve',dependencies=[Depends(require_admin)])
def approve_topic(candidate_id:int):
    candidate=fetch_one("select * from topic_candidates where candidate_id=%s",(candidate_id,))
    if not candidate: raise HTTPException(status_code=404,detail='Candidate not found')
    if candidate['status']!='pending': raise HTTPException(status_code=409,detail='Candidate is not pending')
    with get_conn() as conn, conn.cursor() as cur:
        slug=slugify(candidate['suggested_name'])
        candidate_type = candidate.get('candidate_type') or 'event'
        cur.execute("""insert into topics(slug,name,description,keywords,topic_type,origin) values(%s,%s,%s,%s,%s,'discovered')
                       on conflict(slug) do update set name=excluded.name,topic_type=excluded.topic_type returning topic_id""",(slug,candidate['suggested_name'],'Discovered from recurring headline cluster',candidate['keywords'] or [],candidate_type))
        topic_id=cur.fetchone()['topic_id']
        cur.execute("""insert into article_topics(article_id,topic_id,confidence,assignment_method)
                       select article_id,%s,0.55,'approved_cluster' from topic_candidate_occurrences where candidate_id=%s on conflict do nothing""",(topic_id,candidate_id))
        cur.execute("update topic_candidates set status='approved',last_seen_at=now() where candidate_id=%s",(candidate_id,))
    try:
        from .pipeline import refresh_facts
        refresh_facts()
    except Exception as exc: log.warning('Facts refresh after topic approval failed: %s',exc)
    return {'approved':candidate_id,'topic_id':topic_id}
@app.post('/api/topic-candidates/{candidate_id}/reject',dependencies=[Depends(require_admin)])
def reject_topic(candidate_id:int):
    row=fetch_one("update topic_candidates set status='rejected' where candidate_id=%s returning candidate_id",(candidate_id,))
    if not row: raise HTTPException(status_code=404,detail='Candidate not found')
    return {'rejected':candidate_id}

@app.get('/api/entities',dependencies=[Depends(require_admin)])
def list_entities():
    ents=fetch_all("""select e.*, (select count(*) from article_entities ae where ae.entity_id=e.entity_id) as article_count,
                     coalesce((select array_agg(a.alias order by a.alias) from entity_aliases a where a.entity_id=e.entity_id),'{}') as aliases
                     from entities e order by e.status,e.canonical_name limit 1000""")
    # Entities are automatically inserted by GenAI; the admin edits article links as needed.
    return {'entities':ents,'candidates':[]}
@app.post('/api/entities',dependencies=[Depends(require_admin)])
def create_entity(body:EntityBody):
    row=fetch_one("""insert into entities(canonical_name,normalized_name,entity_type,external_id,description,origin)
                     values(%s,%s,%s,%s,%s,'manual') returning *""",(body.canonical_name.strip(),norm_name(body.canonical_name),body.entity_type.upper(),body.external_id,body.description))
    for alias in body.aliases:
        if norm_name(alias):
            fetch_one("insert into entity_aliases(entity_id,alias,normalized_alias) values(%s,%s,%s) on conflict do nothing returning alias_id",(row['entity_id'],alias.strip(),norm_name(alias)))
    return row
@app.patch('/api/entities/{entity_id}',dependencies=[Depends(require_admin)])
def patch_entity(entity_id:int,patch:dict=Body(...)):
    allowed={'canonical_name','entity_type','external_id','description','status'}; fields={k:v for k,v in patch.items() if k in allowed}
    if 'canonical_name' in fields: fields['normalized_name']=norm_name(fields['canonical_name'])
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields')
    sets=[]; vals=[]
    for k,v in fields.items(): sets.append(f'{k}=%s'); vals.append(v)
    sets.append('updated_at=now()'); vals.append(entity_id)
    try: row=fetch_one(f"update entities set {', '.join(sets)} where entity_id=%s returning *",vals)
    except Exception as exc: raise HTTPException(status_code=409,detail=f'Could not update entity: {str(exc)[:200]}')
    if not row: raise HTTPException(status_code=404,detail='Entity not found')
    return row
@app.post('/api/entity-candidates/{candidate_id}/approve',dependencies=[Depends(require_admin)])
def approve_entity(candidate_id:int):
    candidate=fetch_one('select * from entity_candidates where candidate_id=%s',(candidate_id,))
    if not candidate: raise HTTPException(status_code=404,detail='Candidate not found')
    if candidate['status']!='pending': raise HTTPException(status_code=409,detail='Candidate is not pending')
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,origin) values(%s,%s,%s,'discovered')
                       on conflict(normalized_name,entity_type) do update set canonical_name=excluded.canonical_name returning entity_id""",(candidate['canonical_name'],candidate['normalized_name'],candidate['entity_type']))
        entity_id=cur.fetchone()['entity_id']
        cur.execute("""insert into article_entities(article_id,entity_id,mention_text,confidence)
                       select article_id,%s,mention_text,0.65 from entity_candidate_occurrences where candidate_id=%s on conflict do nothing""",(entity_id,candidate_id))
        cur.execute("update entity_candidates set status='approved',last_seen_at=now() where candidate_id=%s",(candidate_id,))
    try:
        from .pipeline import refresh_facts
        refresh_facts()
    except Exception as exc: log.warning('Facts refresh after entity approval failed: %s',exc)
    return {'approved':candidate_id,'entity_id':entity_id}
@app.post('/api/entity-candidates/{candidate_id}/reject',dependencies=[Depends(require_admin)])
def reject_entity(candidate_id:int):
    row=fetch_one("update entity_candidates set status='rejected' where candidate_id=%s returning candidate_id",(candidate_id,))
    if not row: raise HTTPException(status_code=404,detail='Candidate not found')
    return {'rejected':candidate_id}

@app.get('/api/cleaning-rules',dependencies=[Depends(require_admin)])
def list_rules(): return fetch_all('select * from cleaning_rules order by rule_id desc')
@app.post('/api/cleaning-rules',dependencies=[Depends(require_admin)])
def add_rule(body:RuleBody):
    if body.rule_type not in {'exclude_title_contains','exclude_url_contains','min_title_length','exclude_source'}: raise HTTPException(status_code=400,detail='Unsupported rule type')
    return fetch_one("insert into cleaning_rules(name,rule_type,config,enabled) values(%s,%s,%s::jsonb,%s) returning *",(body.name,body.rule_type,json.dumps(body.config),body.enabled))
@app.patch('/api/cleaning-rules/{rule_id}',dependencies=[Depends(require_admin)])
def patch_rule(rule_id:int,patch:dict=Body(...)):
    allowed={'name','rule_type','config','enabled'}; fields={k:v for k,v in patch.items() if k in allowed}
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields')
    sets=[]; vals=[]
    for k,v in fields.items():
        if k=='config': sets.append('config=%s::jsonb'); vals.append(json.dumps(v))
        else: sets.append(f'{k}=%s'); vals.append(v)
    sets.append('updated_at=now()'); vals.append(rule_id)
    row=fetch_one(f"update cleaning_rules set {', '.join(sets)} where rule_id=%s returning *",vals)
    if not row: raise HTTPException(status_code=404,detail='Rule not found')
    return row
@app.post('/api/cleaning-rules/apply',dependencies=[Depends(require_admin)])
def apply_rules_to_existing_articles():
    from .processing import apply_cleaning_rules
    rules=fetch_all('select * from cleaning_rules where enabled=true order by rule_id')
    rows=fetch_all('select article_id,source_id,title,canonical_url from news_articles')
    updated=0; excluded=0
    with get_conn() as conn, conn.cursor() as cur:
        for article in rows:
            is_excluded, reason=apply_cleaning_rules(article['title'],article['canonical_url'],article['source_id'],rules)
            cur.execute('update news_articles set is_excluded=%s,exclude_reason=%s,updated_at=now() where article_id=%s and (is_excluded is distinct from %s or exclude_reason is distinct from %s)',(is_excluded,reason,article['article_id'],is_excluded,reason))
            updated+=cur.rowcount
            excluded+=1 if is_excluded else 0
    try:
        from .pipeline import refresh_facts
        refresh_facts()
    except Exception as exc:
        log.warning('Facts refresh after rule application failed: %s', exc)
    return {'scanned':len(rows),'updated':updated,'excluded_after_apply':excluded,'message':'Rules re-evaluated against existing clean-layer articles.'}

@app.delete('/api/cleaning-rules/{rule_id}',dependencies=[Depends(require_admin)])
def delete_rule(rule_id:int):
    row=fetch_one('delete from cleaning_rules where rule_id=%s returning rule_id',(rule_id,))
    if not row: raise HTTPException(status_code=404,detail='Rule not found')
    return {'deleted':rule_id}

@app.get('/api/quality/issues',dependencies=[Depends(require_admin)])
def quality_issues(open_only:bool=True,limit:int=Query(default=200,ge=1,le=1000)):
    return fetch_all("""select q.*,a.title,s.name as source_name from data_quality_issues q
                       left join news_articles a on a.article_id=q.article_id left join data_sources s on s.source_id=q.source_id
                       where (%s=false or q.resolved_at is null) order by q.detected_at desc limit %s""",(open_only,limit))
@app.post('/api/quality/issues/{issue_id}/resolve',dependencies=[Depends(require_admin)])
def resolve_issue(issue_id:int):
    row=fetch_one('update data_quality_issues set resolved_at=now() where issue_id=%s returning issue_id,resolved_at',(issue_id,))
    if not row: raise HTTPException(status_code=404,detail='Issue not found')
    return row

@app.get('/api/data-models',dependencies=[Depends(require_admin)])
def list_data_models():
    return fetch_all("select * from data_model_catalog order by case model_type when 'fact' then 1 when 'dimension' then 2 when 'bridge' then 3 else 4 end, table_name")
@app.patch('/api/data-models/{model_id}',dependencies=[Depends(require_admin)])
def patch_data_model(model_id:int,patch:dict=Body(...)):
    allowed={'description','grain','owner','active'}; fields={k:v for k,v in patch.items() if k in allowed}
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields')
    sets=[]; vals=[]
    for k,v in fields.items(): sets.append(f'{k}=%s'); vals.append(v)
    sets.append('updated_at=now()'); vals.append(model_id)
    row=fetch_one(f"update data_model_catalog set {', '.join(sets)} where model_id=%s returning *",vals)
    if not row: raise HTTPException(status_code=404,detail='Data model not found')
    return row

FACT_QUERIES={
 'fact_news_article':"""select f.article_id,s.name as source_name,f.published_date,a.title,f.title_length,f.word_count,f.detected_entity_count,f.detected_topic_count,f.quality_score,f.is_excluded,f.loaded_at from fact_news_article f join data_sources s on s.source_id=f.source_id join news_articles a on a.article_id=f.article_id order by f.published_date desc nulls last limit %s""",
 'fact_topic_daily':"""select f.stat_date,t.name as topic,s.name as source,f.article_count,f.updated_at from fact_topic_daily f join topics t on t.topic_id=f.topic_id join data_sources s on s.source_id=f.source_id order by f.stat_date desc,f.article_count desc limit %s""",
 'fact_topic_trending':"""select f.period_type,f.period_start,t.name as topic,t.topic_type,f.article_count,f.source_count,f.previous_article_count,f.growth_pct,f.hotness_score,f.rank_position,f.updated_at from fact_topic_trending f join topics t on t.topic_id=f.topic_id order by case f.period_type when 'day' then 1 when 'week' then 2 else 3 end,f.period_start desc,f.rank_position limit %s""",
 'fact_entity_daily':"""select f.stat_date,e.canonical_name as entity,e.entity_type,s.name as source,f.article_count,f.updated_at from fact_entity_daily f join entities e on e.entity_id=f.entity_id join data_sources s on s.source_id=f.source_id order by f.stat_date desc,f.article_count desc limit %s""",
 'fact_source_daily':"""select f.stat_date,s.name as source,f.article_count,f.excluded_count,f.avg_quality_score,f.updated_at from fact_source_daily f join data_sources s on s.source_id=f.source_id order by f.stat_date desc,f.article_count desc limit %s"""
}
@app.get('/api/facts/{table_name}',dependencies=[Depends(require_admin)])
def read_fact(table_name:str,limit:int=Query(default=100,ge=1,le=500)):
    if table_name not in FACT_QUERIES: raise HTTPException(status_code=400,detail='Unknown or unsupported fact table')
    return fetch_all(FACT_QUERIES[table_name],(limit,))
@app.get('/api/pipeline/runs',dependencies=[Depends(require_admin)])
def pipeline_runs(limit:int=Query(default=50,ge=1,le=200)):
    return fetch_all('select * from pipeline_runs order by started_at desc limit %s',(limit,))

def _run_manual_pipeline():
    try:
        from .pipeline import run_pipeline
        run_pipeline(trigger_type='admin')
    except Exception:
        log.exception('Manual pipeline failed')
    finally:
        _pipeline_thread_lock.release()
@app.post('/api/pipeline/run',dependencies=[Depends(require_admin)])
def trigger_pipeline():
    if not _pipeline_thread_lock.acquire(blocking=False): raise HTTPException(status_code=409,detail='A pipeline run is already in progress on this instance.')
    thread=threading.Thread(target=_run_manual_pipeline,daemon=True)
    thread.start()
    return {'accepted':True,'message':'Pipeline started. Refresh Pipeline Runs in a few seconds.'}

@app.on_event('startup')
def log_startup():
    if not settings.database_url: log.warning('DATABASE_URL is not configured; API will not be usable until it is set.')
    if not settings.admin_password or not settings.session_secret: log.warning('ADMIN_PASSWORD / SESSION_SECRET is not configured.')
