from __future__ import annotations
import json, logging, threading, secrets, hmac, re
from datetime import date, timedelta
from typing import Literal
import re
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, Depends, HTTPException, Query, Body, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from .config import settings
from .db import get_conn, fetch_all, fetch_one
from .security import (issue_token, require_admin, hash_password, verify_password, issue_user_token, require_user)
from .processing import canonicalize_url, hash_text, norm_name, slugify

logging.basicConfig(level=logging.INFO)
log=logging.getLogger('mini-lakehouse.api')
app=FastAPI(title='Doraemon Mini Lakehouse',version='0.6.4',description='News Radar with global archive search, topic/entity timelines, personal feed and Telegram notifications.')
app.add_middleware(CORSMiddleware,allow_origins=list(settings.cors_origins) or ['*'],allow_credentials=False,allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],allow_headers=['Authorization','Content-Type'])
app.mount('/static',StaticFiles(directory='static'),name='static')
_pipeline_thread_lock=threading.Lock()

class LoginBody(BaseModel):
    username: str
    password: str
class UserRegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        value = value.strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,32}', value):
            raise ValueError('Tên đăng nhập gồm 3–32 ký tự: chữ, số, dấu chấm, gạch ngang hoặc gạch dưới.')
        return value
    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value):
            raise ValueError('Email chưa đúng định dạng.')
        return value

class UserLoginBody(BaseModel):
    identifier: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

class SavedTimelineRef(BaseModel):
    type: Literal['topic', 'entity']
    id: int = Field(ge=1)

class SaveTimelinesBody(BaseModel):
    items: list[SavedTimelineRef] = Field(default_factory=list, max_length=10)

class TelegramSettingsBody(BaseModel):
    enabled: bool
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

def pagination_meta(page: int, page_size: int, total: int) -> dict:
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = 0 if total == 0 else (page - 1) * page_size + 1
    end = min(total, page * page_size)
    return {'page': page, 'page_size': page_size, 'total': total,
            'total_pages': total_pages, 'start': start, 'end': end}


@app.get('/')
def index(): return FileResponse('static/index.html')
@app.get('/news')
def public_news_page(): return FileResponse('static/news.html')

@app.get('/health')
def health(): return {'status':'ok','app':'doraemon-mini-lakehouse','version':'0.6.4'}
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


# End-user auth is separate from the existing administrator login.
def _set_user_cookie(response: Response, request: Request, token: str):
    forwarded_proto = request.headers.get('x-forwarded-proto', request.url.scheme).split(',')[0].strip().lower()
    response.set_cookie(
        key='news_radar_session', value=token, max_age=7 * 24 * 60 * 60,
        httponly=True, secure=(forwarded_proto == 'https'), samesite='lax', path='/'
    )


@app.post('/api/user-auth/register')
def register_user(body: UserRegisterBody, request: Request, response: Response):
    if not settings.session_secret:
        raise HTTPException(status_code=503, detail='Máy chủ chưa cấu hình SESSION_SECRET.')
    username = body.username.strip()
    username_normalized = username.lower()
    email = body.email.strip().lower()
    try:
        user = fetch_one("""INSERT INTO app_users(username, username_normalized, email, email_normalized, password_hash, last_login_at)
                             VALUES (%s, %s, %s, %s, %s, now())
                             ON CONFLICT DO NOTHING
                             RETURNING user_id, username, email""",
                         (username, username_normalized, email, email, hash_password(body.password)))
    except Exception as exc:
        log.exception('User registration failed')
        raise HTTPException(status_code=503, detail='Không thể tạo tài khoản lúc này. Vui lòng kiểm tra migration cơ sở dữ liệu.') from exc
    if not user:
        raise HTTPException(status_code=409, detail='Tên đăng nhập hoặc email đã được sử dụng.')
    _set_user_cookie(response, request, issue_user_token(user['user_id'], user['username']))
    return {'user': user, 'expires_in': 604800}


@app.post('/api/user-auth/login')
def login_user(body: UserLoginBody, request: Request, response: Response):
    if not settings.session_secret:
        raise HTTPException(status_code=503, detail='Máy chủ chưa cấu hình SESSION_SECRET.')
    identifier = body.identifier.strip().lower()
    try:
        user = fetch_one("""SELECT user_id, username, email, password_hash
                             FROM app_users
                             WHERE (username_normalized=%s OR email_normalized=%s) AND is_active=true""",
                         (identifier, identifier))
    except Exception as exc:
        log.exception('User login could not query account storage')
        raise HTTPException(status_code=503, detail='Chức năng tài khoản chưa sẵn sàng. Vui lòng kiểm tra migration SQL 007.') from exc
    if not user or not verify_password(body.password, user['password_hash']):
        raise HTTPException(status_code=401, detail='Tên đăng nhập/email hoặc mật khẩu không đúng.')
    try:
        fetch_one('UPDATE app_users SET last_login_at=now() WHERE user_id=%s RETURNING user_id', (user['user_id'],))
    except Exception as exc:
        log.exception('User login timestamp could not be updated')
        raise HTTPException(status_code=503, detail='Không thể cập nhật phiên đăng nhập lúc này.') from exc
    _set_user_cookie(response, request, issue_user_token(user['user_id'], user['username']))
    return {'user': {'user_id': user['user_id'], 'username': user['username'], 'email': user['email']}, 'expires_in': 604800}


@app.post('/api/user-auth/logout')
def logout_user(response: Response):
    response.delete_cookie('news_radar_session', path='/', httponly=True, samesite='lax')
    return {'ok': True}


@app.get('/api/user-auth/me')
def current_user(user: dict = Depends(require_user)):
    return {'user': user}


@app.get('/api/user/timeline-options')
def user_timeline_options(user: dict = Depends(require_user)):
    topics = fetch_all("""SELECT t.topic_id AS id, t.name, coalesce(t.description,'') AS description,
                                count(DISTINCT a.article_id)::int AS article_count,
                                count(DISTINCT a.source_id)::int AS source_count
                         FROM topics t
                         LEFT JOIN article_topics at ON at.topic_id=t.topic_id
                         LEFT JOIN news_articles a ON a.article_id=at.article_id AND a.is_excluded=false
                         WHERE t.status='active' AND t.topic_type='event'
                         GROUP BY t.topic_id, t.name, t.description
                         ORDER BY count(DISTINCT a.article_id) DESC, t.name
                         LIMIT 300""")
    entities = fetch_all("""SELECT e.entity_id AS id, e.canonical_name AS name,
                                  e.entity_type, coalesce(e.description,'') AS description,
                                  count(DISTINCT a.article_id)::int AS article_count,
                                  count(DISTINCT a.source_id)::int AS source_count
                           FROM entities e
                           LEFT JOIN article_entities ae ON ae.entity_id=e.entity_id
                           LEFT JOIN news_articles a ON a.article_id=ae.article_id AND a.is_excluded=false
                           WHERE e.status='active'
                           GROUP BY e.entity_id, e.canonical_name, e.entity_type, e.description
                           ORDER BY count(DISTINCT a.article_id) DESC, e.canonical_name
                           LIMIT 300""")
    return {'topics': topics, 'entities': entities}


@app.get('/api/user/saved-timelines')
def get_saved_timelines(user: dict = Depends(require_user)):
    items = fetch_all("""SELECT timeline_kind AS type, id, name, display_order, article_count
                         FROM (
                           SELECT 'topic'::text AS timeline_kind, t.topic_id::bigint AS id, t.name,
                                  ust.display_order,
                                  (SELECT count(DISTINCT at.article_id)::int FROM article_topics at
                                   JOIN news_articles a ON a.article_id=at.article_id
                                   WHERE at.topic_id=t.topic_id AND a.is_excluded=false) AS article_count
                           FROM user_saved_timelines ust JOIN topics t ON t.topic_id=ust.topic_id
                           WHERE ust.user_id=%s AND ust.timeline_type='topic' AND t.status='active' AND t.topic_type='event'
                           UNION ALL
                           SELECT 'entity'::text AS timeline_kind, e.entity_id::bigint AS id, e.canonical_name AS name,
                                  ust.display_order,
                                  (SELECT count(DISTINCT ae.article_id)::int FROM article_entities ae
                                   JOIN news_articles a ON a.article_id=ae.article_id
                                   WHERE ae.entity_id=e.entity_id AND a.is_excluded=false) AS article_count
                           FROM user_saved_timelines ust JOIN entities e ON e.entity_id=ust.entity_id
                           WHERE ust.user_id=%s AND ust.timeline_type='entity' AND e.status='active'
                         ) saved ORDER BY display_order, timeline_kind, name""", (user['user_id'], user['user_id']))
    return {'items': items}


@app.put('/api/user/saved-timelines')
def replace_saved_timelines(body: SaveTimelinesBody, user: dict = Depends(require_user)):
    # Deduplicate while preserving the user's chosen order.
    items = []
    seen = set()
    for item in body.items:
        key = (item.type, item.id)
        if key not in seen:
            seen.add(key)
            items.append(item)
    topic_ids = [item.id for item in items if item.type == 'topic']
    entity_ids = [item.id for item in items if item.type == 'entity']
    valid_topics = set()
    valid_entities = set()
    if topic_ids:
        valid_topics = {int(row['topic_id']) for row in fetch_all(
            "SELECT topic_id FROM topics WHERE topic_id = ANY(%s) AND status='active' AND topic_type='event'", (topic_ids,))}
    if entity_ids:
        valid_entities = {int(row['entity_id']) for row in fetch_all(
            "SELECT entity_id FROM entities WHERE entity_id = ANY(%s) AND status='active'", (entity_ids,))}
    invalid = [f"{x.type}:{x.id}" for x in items
               if (x.type == 'topic' and x.id not in valid_topics) or (x.type == 'entity' and x.id not in valid_entities)]
    if invalid:
        raise HTTPException(status_code=422, detail='Một số topic/entity không tồn tại hoặc đã lưu trữ: ' + ', '.join(invalid[:8]))
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM user_saved_timelines WHERE user_id=%s', (user['user_id'],))
            for order, item in enumerate(items):
                if item.type == 'topic':
                    cur.execute("""INSERT INTO user_saved_timelines(user_id, timeline_type, topic_id, display_order)
                                   VALUES (%s, 'topic', %s, %s)""", (user['user_id'], item.id, order))
                else:
                    cur.execute("""INSERT INTO user_saved_timelines(user_id, timeline_type, entity_id, display_order)
                                   VALUES (%s, 'entity', %s, %s)""", (user['user_id'], item.id, order))
    except Exception as exc:
        log.exception('Could not save personal timeline preferences')
        raise HTTPException(status_code=503, detail='Không thể lưu lựa chọn timeline. Vui lòng kiểm tra migration cơ sở dữ liệu.') from exc
    return {'ok': True, 'saved_count': len(items)}

# Telegram settings are per user. The bot token stays in server/workflow environment variables.
@app.get('/api/user/telegram/settings')
def get_user_telegram_settings(user: dict = Depends(require_user)):
    try:
        row = fetch_one(
            "SELECT telegram_enabled, telegram_chat_id, telegram_linked_at FROM app_users WHERE user_id=%s AND is_active=true",
            (user['user_id'],)
        )
    except Exception as exc:
        log.exception('Telegram settings could not be read')
        raise HTTPException(status_code=503, detail='Chưa sẵn sàng cấu hình Telegram. Vui lòng chạy migration SQL 008.') from exc
    if not row:
        raise HTTPException(status_code=404, detail='Không tìm thấy tài khoản người dùng.')
    configured = bool(settings.telegram_bot_token and re.fullmatch(r'[A-Za-z0-9_]{5,32}', settings.telegram_bot_username or '') and settings.telegram_webhook_secret)
    return {
        'enabled': bool(row.get('telegram_enabled')),
        'connected': bool(row.get('telegram_chat_id')),
        'linked_at': row.get('telegram_linked_at'),
        'bot_username': settings.telegram_bot_username or None,
        'bot_available': configured,
    }


@app.post('/api/user/telegram/link')
def create_user_telegram_link(user: dict = Depends(require_user)):
    if not (settings.telegram_bot_token and re.fullmatch(r'[A-Za-z0-9_]{5,32}', settings.telegram_bot_username or '') and settings.telegram_webhook_secret):
        raise HTTPException(status_code=503, detail='Máy chủ chưa cấu hình Telegram. Quản trị viên cần đặt TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME và TELEGRAM_WEBHOOK_SECRET.')
    code = secrets.token_urlsafe(24)
    try:
        row = fetch_one(
            "UPDATE app_users SET telegram_link_code=%s, telegram_link_expires_at=now()+interval '15 minutes' "
            "WHERE user_id=%s AND is_active=true RETURNING telegram_link_expires_at",
            (code, user['user_id'])
        )
    except Exception as exc:
        log.exception('Could not create Telegram link code')
        raise HTTPException(status_code=503, detail='Không thể tạo liên kết Telegram. Vui lòng kiểm tra migration SQL 008.') from exc
    if not row:
        raise HTTPException(status_code=404, detail='Không tìm thấy tài khoản người dùng.')
    return {
        'link_url': f"https://t.me/{settings.telegram_bot_username}?start={code}",
        'expires_at': row['telegram_link_expires_at'],
        'expires_in_seconds': 900,
    }


@app.put('/api/user/telegram/settings')
def update_user_telegram_settings(body: TelegramSettingsBody, user: dict = Depends(require_user)):
    try:
        current = fetch_one('SELECT telegram_chat_id, telegram_enabled FROM app_users WHERE user_id=%s AND is_active=true', (user['user_id'],))
        if not current:
            raise HTTPException(status_code=404, detail='Không tìm thấy tài khoản người dùng.')
        if body.enabled and not current.get('telegram_chat_id'):
            raise HTTPException(status_code=409, detail='Hãy kết nối Telegram trước khi bật thông báo.')
        if body.enabled and not settings.telegram_bot_token:
            raise HTTPException(status_code=503, detail='Máy chủ chưa cấu hình TELEGRAM_BOT_TOKEN để gửi thông báo.')
        row = fetch_one(
            "UPDATE app_users SET telegram_enabled=%s, "
            "telegram_notifications_since=CASE WHEN %s AND NOT telegram_enabled THEN now() ELSE telegram_notifications_since END "
            "WHERE user_id=%s RETURNING telegram_enabled, telegram_notifications_since",
            (body.enabled, body.enabled, user['user_id'])
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('Telegram settings update failed')
        raise HTTPException(status_code=503, detail='Không thể lưu cấu hình Telegram. Vui lòng kiểm tra migration SQL 008.') from exc
    return {'ok': True, 'enabled': bool(row['telegram_enabled']), 'notifications_since': row['telegram_notifications_since']}


@app.delete('/api/user/telegram/link')
def unlink_user_telegram(user: dict = Depends(require_user)):
    try:
        row = fetch_one(
            "UPDATE app_users SET telegram_chat_id=NULL, telegram_enabled=false, telegram_notifications_since=NULL, "
            "telegram_link_code=NULL, telegram_link_expires_at=NULL, telegram_linked_at=NULL "
            "WHERE user_id=%s RETURNING user_id",
            (user['user_id'],)
        )
    except Exception as exc:
        log.exception('Telegram unlink failed')
        raise HTTPException(status_code=503, detail='Không thể ngắt kết nối Telegram. Vui lòng kiểm tra migration SQL 008.') from exc
    if not row:
        raise HTTPException(status_code=404, detail='Không tìm thấy tài khoản người dùng.')
    return {'ok': True}


@app.post('/api/telegram/webhook')
def telegram_webhook(
    update: dict = Body(...),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = settings.telegram_webhook_secret
    if not expected:
        raise HTTPException(status_code=404, detail='Telegram webhook is not configured.')
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(status_code=403, detail='Invalid webhook secret.')
    message = update.get('message') or update.get('edited_message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    text = str(message.get('text') or '').strip()
    # Only link private chats; never link group chats to an account.
    if not chat_id or chat.get('type') != 'private' or not text.startswith('/start'):
        return {'ok': True}
    match = re.match(r'^/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})\s*$', text)
    from .telegram_notifications import send_telegram_text
    try:
        if not match:
            send_telegram_text(str(chat_id), 'Để liên kết, hãy tạo nút kết nối Telegram trong phần cài đặt News Radar rồi mở liên kết đó.')
            return {'ok': True}
        code = match.group(1)
        try:
            linked = fetch_one(
                "UPDATE app_users SET telegram_chat_id=%s, telegram_linked_at=now(), telegram_link_code=NULL, telegram_link_expires_at=NULL "
                "WHERE telegram_link_code=%s AND telegram_link_expires_at > now() AND is_active=true "
                "RETURNING username",
                (str(chat_id), code)
            )
        except Exception:
            log.warning('Telegram link could not be saved; chat identifier is not logged.')
            send_telegram_text(str(chat_id), 'Không thể liên kết tài khoản lúc này. Vui lòng quay lại News Radar và thử tạo liên kết mới.')
            return {'ok': True}
        if not linked:
            send_telegram_text(str(chat_id), 'Mã liên kết không hợp lệ hoặc đã hết hạn. Hãy tạo liên kết mới trong News Radar.')
            return {'ok': True}
        send_telegram_text(str(chat_id), 'Đã liên kết Telegram với News Radar thành công. Quay lại trang News Radar để bật thông báo bài mới.')
    except Exception:
        # Keep webhook responses successful so Telegram does not keep retrying a user-facing reply.
        log.exception('Telegram webhook reply failed')
    return {'ok': True}


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

@app.get('/api/public/news/overview')
def public_news_overview():
    """Public, read-only summary for the user-facing news portal."""
    row = fetch_one("""WITH clock AS (
          SELECT (now() at time zone %s)::date AS today
        )
        SELECT
          (SELECT count(*)::int FROM news_articles a, clock c
           WHERE a.is_excluded=false
             AND (coalesce(a.published_at,a.collected_at) at time zone %s)::date=c.today) AS articles_today,
          (SELECT count(*)::int FROM data_sources WHERE enabled=true) AS enabled_sources,
          (SELECT count(*)::int FROM topics WHERE status='active' AND topic_type='event') AS active_event_topics,
          (SELECT count(*)::int FROM fact_topic_trending f
           JOIN topics t ON t.topic_id=f.topic_id
           CROSS JOIN clock c
           WHERE f.period_type='day' AND f.period_start=c.today
             AND t.status='active' AND t.topic_type='event') AS ranked_events_today
        """, (settings.timezone, settings.timezone))
    return row or {'articles_today': 0, 'enabled_sources': 0,
                   'active_event_topics': 0, 'ranked_events_today': 0}


@app.get('/api/public/news/hot-topics')
def public_hot_topics(
    period: str = Query(default='day', pattern='^(day|week|month)$'),
    limit: int = Query(default=10, ge=1, le=30),
):
    """Return current event-topic ranking for today, this week or this month."""
    local = fetch_one("select (now() at time zone %s)::date as today", (settings.timezone,))
    today = (local or {}).get('today')
    if today is None:
        raise HTTPException(status_code=503, detail='Could not resolve local date')
    if period == 'day':
        period_start = today
        label = 'Hôm nay'
    elif period == 'week':
        period_start = today - timedelta(days=today.weekday())
        label = 'Tuần này'
    else:
        period_start = date(today.year, today.month, 1)
        label = 'Tháng này'
    items = fetch_all("""SELECT f.topic_id, t.slug, t.name AS topic_name,
                              t.description, f.period_type, f.period_start,
                              f.article_count, f.source_count,
                              f.previous_article_count, f.growth_pct,
                              f.hotness_score, f.rank_position
                       FROM fact_topic_trending f
                       JOIN topics t ON t.topic_id=f.topic_id
                       WHERE f.period_type=%s AND f.period_start=%s
                         AND t.status='active' AND t.topic_type='event'
                       ORDER BY f.rank_position ASC, f.hotness_score DESC
                       LIMIT %s""", (period, period_start, limit))
    return {'period': period, 'period_start': period_start, 'period_label': label,
            'items': items, 'count': len(items)}


@app.get('/api/public/news/articles')
def public_daily_articles(
    q: str | None = Query(default=None, max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """Public daily article list, or a global search across all non-excluded stored articles when q is set."""
    local = fetch_one("select (now() at time zone %s)::date as today", (settings.timezone,))
    today = (local or {}).get('today')
    if today is None:
        raise HTTPException(status_code=503, detail='Could not resolve local date')

    query = (q or '').strip()[:150]
    where = ["a.is_excluded=false"]
    vals: list = []
    if query:
        term = f"%{query}%"
        # A search query intentionally has no publication-date predicate: search the
        # entire stored article archive, not just today's list or followed items.
        # Excluded/low-quality records remain hidden from public results.
        where.append("""(
            a.title ILIKE %s
            OR a.summary ILIKE %s
            OR a.content_text ILIKE %s
            OR coalesce(a.author, '') ILIKE %s
            OR s.name ILIKE %s
            OR EXISTS (
                SELECT 1 FROM article_topics search_at
                JOIN topics search_t ON search_t.topic_id=search_at.topic_id
                WHERE search_at.article_id=a.article_id
                  AND search_t.status='active'
                  AND (
                      search_t.name ILIKE %s
                      OR search_t.description ILIKE %s
                      OR array_to_string(search_t.keywords, ' ') ILIKE %s
                  )
            )
            OR EXISTS (
                SELECT 1 FROM article_entities search_ae
                JOIN entities search_e ON search_e.entity_id=search_ae.entity_id
                WHERE search_ae.article_id=a.article_id
                  AND search_e.status='active'
                  AND (
                      search_e.canonical_name ILIKE %s
                      OR search_ae.mention_text ILIKE %s
                      OR EXISTS (
                          SELECT 1 FROM entity_aliases search_alias
                          WHERE search_alias.entity_id=search_e.entity_id
                            AND search_alias.alias ILIKE %s
                      )
                  )
            )
        )""")
        vals.extend([term] * 11)
    else:
        where.append("(coalesce(a.published_at,a.collected_at) at time zone %s)::date=%s")
        vals.extend([settings.timezone, today])

    from_sql = " FROM news_articles a JOIN data_sources s ON s.source_id=a.source_id"
    where_sql = " WHERE " + " AND ".join(where)
    total_row = fetch_one("SELECT count(*)::int AS total" + from_sql + where_sql, vals)
    total = int((total_row or {}).get('total', 0))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    items = fetch_all("""SELECT a.article_id, a.title, a.url, a.author, a.language,
                              left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
                              a.published_at, a.collected_at,
                              coalesce(a.published_at,a.collected_at) AS timeline_at,
                              s.name AS source_name,
                              coalesce((SELECT json_agg(t.name ORDER BY t.name)
                                        FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                                        WHERE at.article_id=a.article_id AND t.status='active'), '[]'::json) AS topics,
                              coalesce((SELECT json_agg(json_build_object('id', t.topic_id, 'name', t.name) ORDER BY t.name)
                                        FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                                        WHERE at.article_id=a.article_id AND t.status='active' AND t.topic_type='event'), '[]'::json) AS topic_refs,
                              coalesce((SELECT json_agg(json_build_object('id', e.entity_id, 'name', e.canonical_name) ORDER BY e.canonical_name)
                                        FROM article_entities ae JOIN entities e ON e.entity_id=ae.entity_id
                                        WHERE ae.article_id=a.article_id AND e.status='active'), '[]'::json) AS entity_refs
                       """ + from_sql + where_sql +
                       " ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC LIMIT %s OFFSET %s",
                       vals + [page_size, (page - 1) * page_size])
    return {'date': today, 'query': query or None, 'items': items,
            'pagination': pagination_meta(page, page_size, total)}


@app.get('/api/user/feed')
def user_news_feed(
    limit: int = Query(default=30, ge=1, le=50),
    user: dict = Depends(require_user),
):
    """Latest distinct articles matching a user's followed event topics or entities."""
    items = fetch_all("""WITH matched AS (
          SELECT DISTINCT a.article_id, 'topic'::text AS follow_type,
                 t.topic_id::bigint AS follow_id, t.name AS follow_name
          FROM user_saved_timelines ust
          JOIN topics t ON t.topic_id=ust.topic_id AND t.status='active' AND t.topic_type='event'
          JOIN article_topics at ON at.topic_id=t.topic_id
          JOIN news_articles a ON a.article_id=at.article_id AND a.is_excluded=false
          WHERE ust.user_id=%s AND ust.timeline_type='topic'
          UNION ALL
          SELECT DISTINCT a.article_id, 'entity'::text AS follow_type,
                 e.entity_id::bigint AS follow_id, e.canonical_name AS follow_name
          FROM user_saved_timelines ust
          JOIN entities e ON e.entity_id=ust.entity_id AND e.status='active'
          JOIN article_entities ae ON ae.entity_id=e.entity_id
          JOIN news_articles a ON a.article_id=ae.article_id AND a.is_excluded=false
          WHERE ust.user_id=%s AND ust.timeline_type='entity'
        ), rolled_up AS (
          SELECT article_id,
                 jsonb_agg(DISTINCT jsonb_build_object('id', follow_id, 'name', follow_name))
                   FILTER (WHERE follow_type='topic') AS matched_topics,
                 jsonb_agg(DISTINCT jsonb_build_object('id', follow_id, 'name', follow_name))
                   FILTER (WHERE follow_type='entity') AS matched_entities
          FROM matched GROUP BY article_id
        )
        SELECT a.article_id, a.title, a.url, a.author, a.language,
               left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
               a.published_at, a.collected_at,
               coalesce(a.published_at,a.collected_at) AS timeline_at,
               s.name AS source_name,
               coalesce(r.matched_topics, '[]'::jsonb) AS matched_topics,
               coalesce(r.matched_entities, '[]'::jsonb) AS matched_entities,
               count(*) OVER()::int AS total_count
        FROM rolled_up r
        JOIN news_articles a ON a.article_id=r.article_id
        JOIN data_sources s ON s.source_id=a.source_id
        ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC
        LIMIT %s""", (user['user_id'], user['user_id'], limit))
    total = int(items[0].get('total_count', 0)) if items else 0
    for item in items:
        item.pop('total_count', None)
    return {'items': items, 'count': total, 'limit': limit}


@app.get('/api/public/news/topics/{topic_id}/timeline')
def public_topic_timeline(
    topic_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """Public chronological view of articles assigned to one active event topic."""
    topic = fetch_one("""SELECT topic_id, slug, name, description, topic_type
                         FROM topics WHERE topic_id=%s AND status='active'""", (topic_id,))
    if not topic or topic.get('topic_type') != 'event':
        raise HTTPException(status_code=404, detail='Event topic not found')
    from_sql = " FROM article_topics at JOIN news_articles a ON a.article_id=at.article_id JOIN data_sources s ON s.source_id=a.source_id"
    where_sql = " WHERE at.topic_id=%s AND a.is_excluded=false"
    total_row = fetch_one("SELECT count(DISTINCT a.article_id)::int AS total" + from_sql + where_sql, (topic_id,))
    total = int((total_row or {}).get('total', 0))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    items = fetch_all("""SELECT a.article_id, a.title, a.url, a.author, a.language,
                              left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
                              a.published_at, a.collected_at,
                              coalesce(a.published_at,a.collected_at) AS timeline_at,
                              s.name AS source_name,
                              coalesce((SELECT string_agg(e.canonical_name, ', ' ORDER BY e.canonical_name)
                                        FROM article_entities ae JOIN entities e ON e.entity_id=ae.entity_id
                                        WHERE ae.article_id=a.article_id AND e.status='active'), '') AS entities
                       """ + from_sql + where_sql +
                       " ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC LIMIT %s OFFSET %s",
                       (topic_id, page_size, (page - 1) * page_size))
    return {'topic': topic, 'items': items,
            'pagination': pagination_meta(page, page_size, total)}




@app.get('/api/public/news/entities/{entity_id}/timeline')
def public_entity_timeline(
    entity_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """Public chronological view of articles associated with one active entity."""
    entity = fetch_one("""SELECT entity_id, canonical_name, entity_type, description
                          FROM entities WHERE entity_id=%s AND status='active'""", (entity_id,))
    if not entity:
        raise HTTPException(status_code=404, detail='Entity not found')
    from_sql = " FROM article_entities ae JOIN news_articles a ON a.article_id=ae.article_id JOIN data_sources s ON s.source_id=a.source_id"
    where_sql = " WHERE ae.entity_id=%s AND a.is_excluded=false"
    total_row = fetch_one("SELECT count(DISTINCT a.article_id)::int AS total" + from_sql + where_sql, (entity_id,))
    total = int((total_row or {}).get('total', 0))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    items = fetch_all("""SELECT a.article_id, a.title, a.url, a.author, a.language,
                              left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
                              a.published_at, a.collected_at,
                              coalesce(a.published_at,a.collected_at) AS timeline_at,
                              s.name AS source_name,
                              coalesce((SELECT string_agg(t.name, ', ' ORDER BY t.name)
                                        FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                                        WHERE at.article_id=a.article_id AND t.status='active'), '') AS topics
                       """ + from_sql + where_sql +
                       " ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC LIMIT %s OFFSET %s",
                       (entity_id, page_size, (page - 1) * page_size))
    return {'entity': entity, 'items': items,
            'pagination': pagination_meta(page, page_size, total)}


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
def list_articles(
    q: str | None = None,
    source_id: int | None = None,
    excluded: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    """Return one page of articles plus total-count metadata."""
    where=[]; vals=[]
    if q:
        where.append('(a.title ilike %s or a.summary ilike %s or a.url ilike %s)')
        term=f'%{q[:150]}%'; vals.extend([term,term,term])
    if source_id is not None:
        where.append('a.source_id=%s'); vals.append(source_id)
    if excluded is not None:
        where.append('a.is_excluded=%s'); vals.append(excluded)
    from_sql = ' from news_articles a join data_sources s on s.source_id=a.source_id'
    where_sql = (' where ' + ' and '.join(where)) if where else ''
    total_row = fetch_one('select count(*)::int as total' + from_sql + where_sql, vals)
    total = int((total_row or {}).get('total', 0))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    sql="""select a.article_id,a.title,a.url,a.canonical_url,a.summary,left(a.content_text,500) as content_preview,a.author,a.language,a.published_at,a.collected_at,a.quality_score,a.is_excluded,a.exclude_reason,s.name as source_name,
           coalesce((select string_agg(t.name,', ' order by t.name) from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id),'') as topics,
           coalesce((select string_agg(e.canonical_name,', ' order by e.canonical_name) from article_entities ae join entities e on e.entity_id=ae.entity_id where ae.article_id=a.article_id),'') as entities,
           coalesce((select json_agg(t.name order by t.name) from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id),'[]'::json) as topic_names,
           coalesce((select json_agg(e.canonical_name order by e.canonical_name) from article_entities ae join entities e on e.entity_id=ae.entity_id where ae.article_id=a.article_id),'[]'::json) as entity_names""" + from_sql + where_sql + ' order by a.collected_at desc limit %s offset %s'
    items = fetch_all(sql, vals + [page_size, (page - 1) * page_size])
    return {'items': items, 'pagination': pagination_meta(page, page_size, total)}

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
def list_topics(
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    all_items: bool = Query(default=False, alias='all'),
):
    """Paginated topic catalog. `all=true` is reserved for the timeline picker."""
    where=[]; vals=[]
    if q:
        where.append('(t.name ilike %s or t.slug ilike %s or t.description ilike %s)')
        term=f'%{q[:150]}%'; vals.extend([term,term,term])
    where_sql = (' where ' + ' and '.join(where)) if where else ''
    total_row = fetch_one('select count(*)::int as total from topics t' + where_sql, vals)
    total = int((total_row or {}).get('total', 0))
    if all_items:
        page_size = max(1, min(total or 1, 5000))
        page = 1
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    topics=fetch_all("""select t.*, (select count(*) from article_topics at where at.topic_id=t.topic_id) as article_count
                       from topics t""" + where_sql + " order by t.status, t.name limit %s offset %s",
                      vals + [page_size, 0 if all_items else (page - 1) * page_size])
    # Topic discovery is automatic; no manual approval queue is exposed in the admin UI.
    return {'topics':topics,'candidates':[],'pagination':pagination_meta(page, page_size, total)}
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
def list_entities(
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    """Paginated entity catalog, with aliases only loaded for the requested page."""
    where=[]; vals=[]
    if q:
        where.append("(e.canonical_name ilike %s or e.normalized_name ilike %s or e.entity_type ilike %s or exists (select 1 from entity_aliases aq where aq.entity_id=e.entity_id and aq.alias ilike %s))")
        term=f'%{q[:150]}%'; vals.extend([term,term,term,term])
    where_sql = (' where ' + ' and '.join(where)) if where else ''
    total_row=fetch_one('select count(*)::int as total from entities e' + where_sql, vals)
    total=int((total_row or {}).get('total',0))
    total_pages=max(1,(total+page_size-1)//page_size)
    page=min(page,total_pages)
    ents=fetch_all("""select e.*, (select count(*) from article_entities ae where ae.entity_id=e.entity_id) as article_count,
                     coalesce((select array_agg(a.alias order by a.alias) from entity_aliases a where a.entity_id=e.entity_id),'{}') as aliases
                     from entities e""" + where_sql + " order by e.status,e.canonical_name limit %s offset %s",
                   vals + [page_size, (page-1)*page_size])
    # Entities are automatically inserted by GenAI; the admin edits article links as needed.
    return {'entities':ents,'candidates':[],'pagination':pagination_meta(page,page_size,total)}
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
