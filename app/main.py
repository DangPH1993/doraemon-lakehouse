from __future__ import annotations
import json, logging, threading, secrets, hmac, re, hashlib, smtplib, ssl, html as html_lib
from io import BytesIO
from datetime import date, timedelta, datetime, timezone
from typing import Literal
import re
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, Depends, HTTPException, Query, Body, Request, Response, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from email.message import EmailMessage
from .config import settings
from .db import get_conn, fetch_all, fetch_one
from .security import (issue_token, require_admin, hash_password, verify_password, issue_user_token, require_user)
from .processing import canonicalize_url, hash_text, norm_name, slugify, normalize_space
from .entity_rules import should_track_entity, is_media_source_entity, is_grounded_entity, filter_grounded_entity_references
from .entity_names import entity_normalized_key
from .entity_resolution import (build_entity_lookup, entity_name_variants,
                                resolve_entity_in_database, resolve_entity_in_lookup, strip_person_title)
from .field_categories import FIELD_CATEGORIES, LEGACY_BROAD_TOPIC_LABELS, is_broad_field_topic
from .r2_storage import (R2NotConfigured, delete_object as delete_r2_object, get_bytes as get_r2_bytes,
                         is_configured as r2_is_configured, make_object_key as make_r2_key,
                         put_bytes as put_r2_bytes)
from .subscriptions import expire_pro_plans

logging.basicConfig(level=logging.INFO)
log=logging.getLogger('mini-lakehouse.api')
app=FastAPI(title='Tìm Tin Tức',version='0.7.60',description='SEO article pages with per-article URLs; public article cards open original publisher links while 3T permalinks retain server-rendered detail pages.')
app.add_middleware(CORSMiddleware,allow_origins=list(settings.cors_origins) or ['*'],allow_credentials=False,allow_methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'],allow_headers=['Authorization','Content-Type'])
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

class ForgotPasswordBody(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    @field_validator('email')
    @classmethod
    def valid_reset_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value):
            raise ValueError('Email chưa đúng định dạng.')
        return value

class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=8, max_length=128)

class UserPlanBody(BaseModel):
    plan: Literal['Free', 'Pro']

class PaymentReviewBody(BaseModel):
    decision: Literal['approve', 'reject']
    note: str = Field(default='', max_length=1000)

class SavedTimelineRef(BaseModel):
    type: Literal['topic', 'entity']
    id: int = Field(ge=1)

class SaveTimelinesBody(BaseModel):
    items: list[SavedTimelineRef] = Field(default_factory=list, max_length=10)

class TelegramSettingsBody(BaseModel):
    enabled: bool

class GoogleAnalyticsConfigBody(BaseModel):
    measurement_id: str = Field(default='', max_length=32)
    @field_validator('measurement_id')
    @classmethod
    def valid_measurement_id(cls, value):
        value = value.strip().upper()
        if not value:
            return ''
        if not re.fullmatch(r'G-[A-Z0-9]{4,20}', value):
            raise ValueError('Measurement ID phải có dạng G-XXXXXXXXXX. Để tắt theo dõi, hãy để trống.')
        return value
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
    topic_type: str = 'event'
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


@app.get('/', include_in_schema=False)
def index():
    # The domain root is the public news portal, not the admin console.
    return RedirectResponse(url='/news', status_code=302, headers={'Cache-Control': 'no-store'})

@app.get('/admin', include_in_schema=False)
def admin_page():
    # Keep the admin console available at a dedicated path.
    return FileResponse('static/index.html', headers={'Cache-Control': 'no-cache, no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0'})

@app.get('/news')
def public_news_page():
    # Always deliver the newest HTML so outdated filter markup is not retained by browsers/CDNs.
    return FileResponse('static/news.html', headers={'Cache-Control': 'no-cache, no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0'})

@app.get('/timeline')
def public_standalone_timeline_page():
    # Dedicated share destination: fetches only the requested timeline, not the home news feed.
    return FileResponse('static/timeline.html', headers={'Cache-Control': 'no-cache, no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0'})

# Public SEO surface: each article gets a stable, server-rendered URL.
PUBLIC_SITE_URL = (settings.public_app_url or 'https://timtintuc.com').rstrip('/')
if 'YOUR-NEWS-RADAR-DOMAIN' in PUBLIC_SITE_URL:
    PUBLIC_SITE_URL = 'https://timtintuc.com'
SITEMAP_PAGE_SIZE = 5000

# Keep duplicate RSS records for provenance in Neon, but expose the best representative
# of an exact headline to search crawlers. This mirrors the public feed's 72-hour policy.
_SEO_CANONICAL_ARTICLE_FILTER = """a.is_excluded=false AND NOT EXISTS (
    SELECT 1 FROM news_articles other
    WHERE other.is_excluded=false
      AND other.article_id<>a.article_id
      AND other.headline_hash=a.headline_hash
      AND coalesce(other.published_at,other.collected_at) >= coalesce(a.published_at,a.collected_at)-INTERVAL '72 hours'
      AND coalesce(other.published_at,other.collected_at) <= coalesce(a.published_at,a.collected_at)+INTERVAL '72 hours'
      AND (
        coalesce(other.quality_score,0)>coalesce(a.quality_score,0)
        OR (coalesce(other.quality_score,0)=coalesce(a.quality_score,0) AND other.image_url IS NOT NULL AND a.image_url IS NULL)
        OR (coalesce(other.quality_score,0)=coalesce(a.quality_score,0) AND (other.image_url IS NOT NULL)=(a.image_url IS NOT NULL) AND length(coalesce(other.summary,''))>length(coalesce(a.summary,'')))
        OR (coalesce(other.quality_score,0)=coalesce(a.quality_score,0) AND (other.image_url IS NOT NULL)=(a.image_url IS NOT NULL) AND length(coalesce(other.summary,''))=length(coalesce(a.summary,'')) AND coalesce(other.published_at,other.collected_at)>coalesce(a.published_at,a.collected_at))
        OR (coalesce(other.quality_score,0)=coalesce(a.quality_score,0) AND (other.image_url IS NOT NULL)=(a.image_url IS NOT NULL) AND length(coalesce(other.summary,''))=length(coalesce(a.summary,'')) AND coalesce(other.published_at,other.collected_at)=coalesce(a.published_at,a.collected_at) AND other.article_id>a.article_id)
      )
)"""


def _seo_article_path(article_id: int, title: str) -> str:
    return f"/tin/{int(article_id)}/{slugify(title)}"


def _seo_absolute_url(path: str) -> str:
    return f"{PUBLIC_SITE_URL}{path if path.startswith('/') else '/' + path}"


def _seo_html(value) -> str:
    return html_lib.escape(str(value or ''), quote=True)


def _seo_iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _seo_json(value) -> str:
    # Prevent untrusted RSS text from closing the JSON-LD script element.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')


@app.get('/robots.txt', response_class=PlainTextResponse, include_in_schema=False)
def public_robots_txt():
    body = '\n'.join([
        'User-agent: *',
        'Allow: /',
        'Disallow: /admin',
        'Disallow: /api/',
        f'Sitemap: {_seo_absolute_url("/sitemap.xml")}',
        '',
    ])
    return PlainTextResponse(body, headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/sitemap.xml', response_class=Response, include_in_schema=False)
def public_sitemap_index():
    count_row = fetch_one(f"SELECT count(*)::int AS total FROM news_articles a WHERE {_SEO_CANONICAL_ARTICLE_FILTER}")
    total = int((count_row or {}).get('total', 0))
    page_count = max(1, (total + SITEMAP_PAGE_SIZE - 1) // SITEMAP_PAGE_SIZE)
    sitemap_urls = ''.join(
        '<sitemap><loc>' + _seo_html(_seo_absolute_url(f'/sitemap-articles-{page}.xml')) + '</loc></sitemap>'
        for page in range(1, page_count + 1)
    )
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + sitemap_urls + '</sitemapindex>'
    return Response(content=xml, media_type='application/xml; charset=utf-8', headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/sitemap-articles-{page}.xml', response_class=Response, include_in_schema=False)
def public_article_sitemap(page: int):
    if page < 1:
        raise HTTPException(status_code=404, detail='Sitemap page not found')
    count_row = fetch_one(f"SELECT count(*)::int AS total FROM news_articles a WHERE {_SEO_CANONICAL_ARTICLE_FILTER}")
    total = int((count_row or {}).get('total', 0))
    page_count = max(1, (total + SITEMAP_PAGE_SIZE - 1) // SITEMAP_PAGE_SIZE)
    if page > page_count:
        raise HTTPException(status_code=404, detail='Sitemap page not found')
    rows = fetch_all(f"""SELECT a.article_id,a.title,a.updated_at,a.published_at,a.collected_at
                         FROM news_articles a
                         WHERE {_SEO_CANONICAL_ARTICLE_FILTER}
                         ORDER BY coalesce(a.published_at,a.collected_at) DESC,a.article_id DESC
                         LIMIT %s OFFSET %s""", (SITEMAP_PAGE_SIZE, (page - 1) * SITEMAP_PAGE_SIZE))
    entries = []
    for row in rows:
        path = _seo_article_path(int(row['article_id']), row.get('title') or 'tin-tuc')
        lastmod = _seo_iso(row.get('updated_at') or row.get('published_at') or row.get('collected_at'))
        lastmod_xml = '<lastmod>' + _seo_html(lastmod) + '</lastmod>' if lastmod else ''
        entries.append('<url><loc>' + _seo_html(_seo_absolute_url(path)) + '</loc>' + lastmod_xml + '</url>')
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(entries) + '</urlset>'
    return Response(content=xml, media_type='application/xml; charset=utf-8', headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/tin/{article_id}/{slug}', response_class=HTMLResponse, include_in_schema=False)
def public_article_page(article_id: int, slug: str):
    article = fetch_one("""SELECT a.article_id,a.title,a.url,a.canonical_url,coalesce(nullif(a.summary,''),left(nullif(a.content_text,''),2400),'') AS summary,a.image_url,a.author,
                                 a.published_at,a.collected_at,a.updated_at,
                                 coalesce(a.field_category,'Tin tức') AS field_category,
                                 s.name AS source_name
                          FROM news_articles a
                          JOIN data_sources s ON s.source_id=a.source_id
                          WHERE a.article_id=%s AND a.is_excluded=false""", (article_id,))
    if not article:
        raise HTTPException(status_code=404, detail='Article not found')

    title = str(article.get('title') or 'Tin tức')
    canonical_path = _seo_article_path(article_id, title)
    canonical_url = _seo_absolute_url(canonical_path)
    if slug != slugify(title):
        return RedirectResponse(url=canonical_path, status_code=301)

    source_url = str(article.get('url') or '')
    if not source_url.lower().startswith(('https://', 'http://')):
        source_url = ''
    image_url = str(article.get('image_url') or '')
    if not image_url.lower().startswith(('https://', 'http://')):
        image_url = ''

    summary = ' '.join(str(article.get('summary') or '').split())[:4000]
    source_name = str(article.get('source_name') or 'Nguồn báo chí')
    if not summary:
        summary = f'Tin được Tìm Tin Tức tổng hợp từ nguồn {source_name}. Mở bài gốc để xem thông tin chi tiết.'
    description = summary[:300] if len(summary) > 300 else summary
    published = article.get('published_at') or article.get('collected_at')
    published_iso = _seo_iso(published)
    modified_iso = _seo_iso(article.get('updated_at') or published)

    topics = fetch_all("""SELECT t.topic_id,t.name
                         FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                         WHERE at.article_id=%s AND t.status='active'
                         ORDER BY t.name LIMIT 8""", (article_id,))
    related = fetch_all("""SELECT * FROM (
                             SELECT DISTINCT ON (a.article_id) a.article_id,a.title,a.summary,
                                    a.published_at,a.collected_at,s.name AS source_name
                             FROM article_topics own
                             JOIN topics topic ON topic.topic_id=own.topic_id AND topic.status='active' AND topic.topic_type='event'
                             JOIN article_topics other ON other.topic_id=topic.topic_id AND other.article_id<>own.article_id
                             JOIN news_articles a ON a.article_id=other.article_id AND a.is_excluded=false
                             JOIN data_sources s ON s.source_id=a.source_id
                             WHERE own.article_id=%s
                             ORDER BY a.article_id,coalesce(a.published_at,a.collected_at) DESC
                         ) candidates
                         ORDER BY coalesce(published_at,collected_at) DESC,article_id DESC LIMIT 5""", (article_id,))

    topic_html_parts = []
    for topic in topics:
        topic_path = f'/timeline?type=topic&id={int(topic["topic_id"])}&page=1'
        topic_html_parts.append('<a class="topic" href="' + _seo_html(_seo_absolute_url(topic_path)) + '">' + _seo_html(topic.get('name')) + '</a>')
    topic_html = ''.join(topic_html_parts)
    topic_block = '<nav class="topics" aria-label="Chủ đề liên quan">' + topic_html + '</nav>' if topic_html else ''

    related_parts = []
    for item in related:
        related_path = _seo_article_path(int(item['article_id']), item.get('title') or 'tin-tuc')
        related_date = _seo_iso(item.get('published_at') or item.get('collected_at')) or ''
        related_parts.append('<li><a href="' + _seo_html(related_path) + '">' + _seo_html(item.get('title')) + '</a>'
                             + '<small>' + _seo_html(item.get('source_name')) + ' · ' + _seo_html(related_date) + '</small></li>')
    related_html = ''.join(related_parts) or '<li class="muted">Chưa có bài liên quan được gắn cùng sự kiện.</li>'

    schema = {
        '@context': 'https://schema.org',
        '@type': 'NewsArticle',
        'mainEntityOfPage': {'@type': 'WebPage', '@id': canonical_url},
        'headline': title[:110],
        'description': description,
        'datePublished': published_iso,
        'dateModified': modified_iso,
        'articleSection': article.get('field_category') or 'Tin tức',
        'inLanguage': 'vi',
        'publisher': {'@type': 'Organization', 'name': 'Tìm Tin Tức', 'url': PUBLIC_SITE_URL},
        'author': {'@type': 'Person', 'name': str(article.get('author'))} if article.get('author') else {'@type': 'Organization', 'name': source_name, 'url': source_url or PUBLIC_SITE_URL},
    }
    if image_url:
        schema['image'] = [image_url]
    schema = {key: value for key, value in schema.items() if value is not None}

    values = {
        '{{SEO_TITLE}}': _seo_html(title + ' | Tìm Tin Tức 3T'),
        '{{TITLE}}': _seo_html(title),
        '{{DESCRIPTION}}': _seo_html(description),
        '{{CANONICAL_URL}}': _seo_html(canonical_url),
        '{{OG_IMAGE_META}}': '<meta property="og:image" content="' + _seo_html(image_url) + '">' if image_url else '',
        '{{TWITTER_CARD}}': 'summary_large_image' if image_url else 'summary',
        '{{JSON_LD}}': _seo_json(schema),
        '{{BRAND_URL}}': _seo_html(_seo_absolute_url('/news')),
        '{{CATEGORY}}': _seo_html(article.get('field_category') or 'Tin tức'),
        '{{SOURCE_NAME}}': _seo_html(source_name),
        '{{PUBLISHED_ISO}}': _seo_html(published_iso or ''),
        '{{PUBLISHED_TEXT}}': _seo_html(published_iso or 'Chưa rõ thời gian'),
        '{{IMAGE_BLOCK}}': '<img class="hero" src="' + _seo_html(image_url) + '" alt="" loading="eager">' if image_url else '',
        '{{SUMMARY}}': _seo_html(summary),
        '{{AUTHOR_BLOCK}}': ' · Tác giả: ' + _seo_html(article.get('author')) if article.get('author') else '',
        '{{SOURCE_LINK}}': '<a class="source-button" href="' + _seo_html(source_url) + '" target="_blank" rel="noopener noreferrer">Đọc bài gốc tại ' + _seo_html(source_name) + ' ↗</a>' if source_url else '<span class="muted">Nguồn gốc chưa có liên kết hợp lệ.</span>',
        '{{TOPICS_BLOCK}}': topic_block,
        '{{RELATED_ITEMS}}': related_html,
    }
    template_path = 'static/article.html'
    try:
        page_html = open(template_path, 'r', encoding='utf-8').read()
    except OSError:
        raise HTTPException(status_code=500, detail='Article page template is missing')
    for token, value in values.items():
        page_html = page_html.replace(token, value)
    return HTMLResponse(content=page_html, headers={'Cache-Control': 'public, max-age=300'})



@app.get('/news/following')
def public_following_page(): return FileResponse('static/following.html')

@app.api_route('/health', methods=['GET', 'HEAD'])
def health(): return {'status':'ok','app':'doraemon-mini-lakehouse','version':app.version}
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


@app.get('/api/admin/analytics-config', dependencies=[Depends(require_admin)])
def get_admin_analytics_config():
    try:
        row = fetch_one("SELECT setting_value, updated_at FROM app_site_settings WHERE setting_key='google_analytics_measurement_id'")
    except Exception as exc:
        log.exception('Could not read Google Analytics configuration')
        raise HTTPException(status_code=503, detail='Chưa có bảng cấu hình Google Analytics. Hãy chạy migration 022 trong Neon SQL Editor.') from exc
    return {
        'measurement_id': (row or {}).get('setting_value') or '',
        'enabled': bool((row or {}).get('setting_value')),
        'updated_at': (row or {}).get('updated_at'),
    }


@app.put('/api/admin/analytics-config', dependencies=[Depends(require_admin)])
def update_admin_analytics_config(body: GoogleAnalyticsConfigBody):
    try:
        row = fetch_one("INSERT INTO app_site_settings (setting_key, setting_value) VALUES ('google_analytics_measurement_id', %s) ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value, updated_at=now() RETURNING setting_value, updated_at", (body.measurement_id,))
    except Exception as exc:
        log.exception('Could not save Google Analytics configuration')
        raise HTTPException(status_code=503, detail='Không lưu được cấu hình Google Analytics. Hãy chạy migration 022 trong Neon SQL Editor.') from exc
    return {
        'ok': True,
        'measurement_id': (row or {}).get('setting_value') or '',
        'enabled': bool((row or {}).get('setting_value')),
        'updated_at': (row or {}).get('updated_at'),
        'message': 'Đã lưu cấu hình Google Analytics.' if body.measurement_id else 'Đã tắt Google Analytics.',
    }


@app.get('/api/public/analytics-config')
def get_public_analytics_config(response: Response):
    # Measurement ID is public by design. No Google API secret is stored or exposed here.
    response.headers['Cache-Control'] = 'public, max-age=60'
    try:
        row = fetch_one("SELECT setting_value FROM app_site_settings WHERE setting_key='google_analytics_measurement_id'")
    except Exception:
        # Keep the public news page available if the optional analytics migration has not run.
        log.warning('Google Analytics config unavailable; tracking remains disabled until migration 022 is applied.')
        return {'measurement_id': ''}
    measurement_id = (row or {}).get('setting_value') or ''
    if not re.fullmatch(r'G-[A-Z0-9]{4,20}', measurement_id):
        measurement_id = ''
    return {'measurement_id': measurement_id}


# Site branding is persisted as private R2 objects, while only public image endpoints are exposed.
_SITE_BRANDING_SETTING_KEYS = (
    'site_logo_r2_key', 'site_favicon_r2_key', 'site_favicon_ico_r2_key', 'site_logo_mode', 'site_branding_version',
)
_SITE_LOGO_MAX_BYTES = 3 * 1024 * 1024
_SITE_LOGO_TYPES = {'PNG', 'JPEG', 'WEBP'}


def _get_site_branding_state() -> dict:
    rows = fetch_all(
        "SELECT setting_key, setting_value, updated_at FROM app_site_settings WHERE setting_key = ANY(%s)",
        (list(_SITE_BRANDING_SETTING_KEYS),),
    )
    values = {row['setting_key']: row for row in rows}
    logo_key = (values.get('site_logo_r2_key') or {}).get('setting_value') or ''
    favicon_key = (values.get('site_favicon_r2_key') or {}).get('setting_value') or ''
    favicon_ico_key = (values.get('site_favicon_ico_r2_key') or {}).get('setting_value') or ''
    mode = (values.get('site_logo_mode') or {}).get('setting_value') or 'icon'
    version = (values.get('site_branding_version') or {}).get('setting_value') or 'default-0.7.58'
    logo_url = f'/api/public/site-logo?v={version}' if logo_key else '/static/site-logo.png?v=0.7.58'
    favicon_url = f'/api/public/site-favicon?v={version}' if favicon_key else '/static/favicon.png?v=0.7.58'
    return {
        'logo_url': logo_url,
        'favicon_url': favicon_url,
        'logo_mode': mode if mode in {'icon', 'wordmark'} else 'icon',
        'is_custom': bool(logo_key and favicon_key),
        'updated_at': (values.get('site_branding_version') or {}).get('updated_at'),
        '_logo_key': logo_key,
        '_favicon_key': favicon_key,
        '_favicon_ico_key': favicon_ico_key,
    }


def _public_site_branding_payload(state: dict) -> dict:
    return {key: state[key] for key in ('logo_url', 'favicon_url', 'logo_mode', 'is_custom', 'updated_at')}


def _prepare_site_logo(raw: bytes) -> tuple[bytes, bytes, bytes, str]:
    """Validate a logo and return normalized PNG logo, square PNG favicon, and display mode."""
    from PIL import Image
    try:
        with Image.open(BytesIO(raw)) as probe:
            image_format = (probe.format or '').upper()
            width, height = probe.size
            if image_format not in _SITE_LOGO_TYPES:
                raise ValueError('Chỉ chấp nhận ảnh PNG, JPG hoặc WebP.')
            if width < 32 or height < 32:
                raise ValueError('Ảnh logo tối thiểu phải có kích thước 32 × 32 px.')
            if width > 6000 or height > 6000 or width * height > 20_000_000:
                raise ValueError('Kích thước ảnh quá lớn; hãy dùng ảnh tối đa 6000 × 6000 px.')
            probe.verify()
        with Image.open(BytesIO(raw)) as source:
            image = source.convert('RGBA')
            image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            logo_buffer = BytesIO()
            image.save(logo_buffer, format='PNG', optimize=True)
            # Keep the entire uploaded mark visible in the favicon; square padding
            # avoids cropping the middle of wide wordmark logos.
            favicon_art = image.copy()
            favicon_art.thumbnail((160, 160), Image.Resampling.LANCZOS)
            favicon = Image.new('RGBA', (192, 192), (255, 255, 255, 0))
            favicon.alpha_composite(favicon_art, ((192 - favicon_art.width) // 2, (192 - favicon_art.height) // 2))
            favicon_buffer = BytesIO()
            favicon.save(favicon_buffer, format='PNG', optimize=True)
            favicon_ico_buffer = BytesIO()
            favicon.save(favicon_ico_buffer, format='ICO', sizes=[(16, 16), (32, 32), (48, 48)])
        mode = 'wordmark' if width >= height * 1.65 else 'icon'
        return logo_buffer.getvalue(), favicon_buffer.getvalue(), favicon_ico_buffer.getvalue(), mode
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Không đọc được ảnh logo; hãy chọn lại ảnh PNG, JPG hoặc WebP hợp lệ.') from exc


def _save_site_branding_settings(logo_key: str, favicon_key: str, favicon_ico_key: str, mode: str, version: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        values = {
            'site_logo_r2_key': logo_key,
            'site_favicon_r2_key': favicon_key,
            'site_favicon_ico_r2_key': favicon_ico_key,
            'site_logo_mode': mode,
            'site_branding_version': version,
        }
        for key, value in values.items():
            cur.execute(
                "INSERT INTO app_site_settings (setting_key, setting_value) VALUES (%s, %s) "
                "ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value, updated_at=now()",
                (key, value),
            )


def _serve_site_branding_image(kind: str):
    try:
        state = _get_site_branding_state()
    except Exception:
        # Keep public pages available while optional settings storage is unavailable.
        log.warning('Site branding settings unavailable; using bundled fallback %s.', kind, exc_info=True)
        path = 'static/site-logo.png' if kind == 'logo' else ('static/favicon.png' if kind == 'favicon' else 'static/favicon.ico')
        media_type = 'image/png' if kind in {'logo', 'favicon'} else 'image/x-icon'
        return FileResponse(path, media_type=media_type, headers={'Cache-Control': 'public, max-age=3600'})
    key = state['_logo_key'] if kind == 'logo' else (state['_favicon_key'] if kind == 'favicon' else state['_favicon_ico_key'])
    fallback_path = 'static/site-logo.png' if kind == 'logo' else ('static/favicon.png' if kind == 'favicon' else 'static/favicon.ico')
    media_type = 'image/png' if kind in {'logo', 'favicon'} else 'image/x-icon'
    if not key:
        return FileResponse(fallback_path, media_type=media_type, headers={'Cache-Control': 'public, max-age=86400'})
    try:
        content = get_r2_bytes(key, max_bytes=8 * 1024 * 1024)
        return Response(content=content, media_type=media_type, headers={
            # Keep the canonical root favicon refreshable after a logo change;
            # versioned API image URLs can safely use immutable caching.
            'Cache-Control': 'public, max-age=3600' if kind == 'ico' else 'public, max-age=31536000, immutable',
            'X-Content-Type-Options': 'nosniff',
        })
    except Exception as exc:
        log.exception('Unable to read custom site %s from R2', kind)
        raise HTTPException(status_code=503, detail='Không tải được logo đã lưu; hãy kiểm tra cấu hình Cloudflare R2.') from exc


@app.get('/api/admin/site-branding', dependencies=[Depends(require_admin)])
def get_admin_site_branding():
    try:
        state = _get_site_branding_state()
    except Exception as exc:
        log.exception('Could not read site branding settings')
        raise HTTPException(status_code=503, detail='Không đọc được cấu hình logo. Hãy kiểm tra migration 022 và bảng app_site_settings trong Neon.') from exc
    return _public_site_branding_payload(state)


@app.put('/api/admin/site-branding', dependencies=[Depends(require_admin)])
async def update_admin_site_branding(
    logo: UploadFile | None = File(default=None),
    clear_logo: bool = Form(default=False),
):
    try:
        current = _get_site_branding_state()
    except Exception as exc:
        log.exception('Could not load current site branding before update')
        raise HTTPException(status_code=503, detail='Không đọc được cấu hình logo. Hãy kiểm tra bảng app_site_settings trong Neon.') from exc

    if not clear_logo and logo is None:
        return {**_public_site_branding_payload(current), 'message': 'Chưa có thay đổi logo.'}

    if clear_logo:
        version = secrets.token_hex(8)
        try:
            _save_site_branding_settings('', '', '', 'icon', version)
        except Exception as exc:
            log.exception('Unable to reset site branding settings')
            raise HTTPException(status_code=503, detail='Không lưu được cấu hình logo mới.') from exc
        for old_key in (current['_logo_key'], current['_favicon_key'], current['_favicon_ico_key']):
            if old_key:
                delete_r2_object(old_key)
        state = _get_site_branding_state()
        return {**_public_site_branding_payload(state), 'message': 'Đã chuyển về logo 3T mặc định và cập nhật favicon.'}

    if logo is None:
        raise HTTPException(status_code=400, detail='Vui lòng chọn một ảnh logo.')
    if not r2_is_configured():
        raise HTTPException(status_code=503, detail='Chưa cấu hình Cloudflare R2. Hãy thiết lập thông tin R2 trong Environment của Render để lưu logo bền vững.')
    raw = await logo.read(_SITE_LOGO_MAX_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail='File logo rỗng.')
    if len(raw) > _SITE_LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail='Logo vượt quá 3 MB.')
    try:
        logo_png, favicon_png, favicon_ico, mode = _prepare_site_logo(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await logo.close()

    version = secrets.token_hex(8)
    logo_key = make_r2_key('branding', f'site-logo-{version}.png')
    favicon_key = make_r2_key('branding', f'site-favicon-{version}.png')
    favicon_ico_key = make_r2_key('branding', f'site-favicon-{version}.ico')
    stored_keys = []
    try:
        put_r2_bytes(logo_key, logo_png, 'image/png')
        stored_keys.append(logo_key)
        put_r2_bytes(favicon_key, favicon_png, 'image/png')
        stored_keys.append(favicon_key)
        put_r2_bytes(favicon_ico_key, favicon_ico, 'image/x-icon')
        stored_keys.append(favicon_ico_key)
        _save_site_branding_settings(logo_key, favicon_key, favicon_ico_key, mode, version)
    except Exception as exc:
        for key in stored_keys:
            delete_r2_object(key)
        log.exception('Unable to save uploaded site branding assets')
        raise HTTPException(status_code=503, detail='Không lưu được logo/favicon. Hãy kiểm tra kết nối Cloudflare R2 và PostgreSQL.') from exc

    for old_key in (current['_logo_key'], current['_favicon_key'], current['_favicon_ico_key']):
        if old_key and old_key not in stored_keys:
            delete_r2_object(old_key)
    state = _get_site_branding_state()
    return {**_public_site_branding_payload(state), 'message': 'Đã lưu logo website và tạo favicon 192 × 192 px.'}


@app.get('/api/public/site-branding')
def get_public_site_branding(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        state = _get_site_branding_state()
    except Exception:
        log.warning('Public site branding settings unavailable; using bundled fallback.', exc_info=True)
        state = {
            'logo_url': '/static/site-logo.png?v=0.7.58',
            'favicon_url': '/static/favicon.png?v=0.7.58',
            'logo_mode': 'icon', 'is_custom': False, 'updated_at': None,
        }
    return _public_site_branding_payload(state)


@app.get('/api/public/site-logo')
def get_public_site_logo():
    return _serve_site_branding_image('logo')


@app.get('/api/public/site-favicon')
def get_public_site_favicon():
    return _serve_site_branding_image('favicon')


@app.get('/favicon.ico', include_in_schema=False)
def root_favicon():
    # Browsers/crawlers that probe the conventional root favicon path get the same current brand icon.
    return _serve_site_branding_image('ico')


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
                             RETURNING user_id, username, email, plan, plan_expires_at""",
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
        # Expire this account before returning its plan at login.
        fetch_one("""UPDATE app_users
                      SET plan='Free', plan_expires_at=NULL, plan_updated_at=now(),
                          plan_updated_by='subscription-expiry', telegram_enabled=false,
                          telegram_notifications_since=NULL
                      WHERE (username_normalized=%s OR email_normalized=%s)
                        AND plan='Pro' AND plan_expires_at IS NOT NULL AND plan_expires_at <= now()
                      RETURNING user_id""", (identifier, identifier))
        user = fetch_one("""SELECT user_id, username, email, plan, plan_expires_at, password_hash
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
    return {'user': {'user_id': user['user_id'], 'username': user['username'], 'email': user['email'], 'plan': user.get('plan', 'Free'), 'plan_expires_at': user.get('plan_expires_at')}, 'expires_in': 604800}


@app.post('/api/user-auth/logout')
def logout_user(response: Response):
    response.delete_cookie('news_radar_session', path='/', httponly=True, samesite='lax')
    return {'ok': True}


@app.get('/api/user-auth/me')
def current_user(user: dict = Depends(require_user)):
    return {'user': user}


# Subscription, payment evidence, and password recovery.
MAX_PAYMENT_PROOF_BYTES = 3 * 1024 * 1024
ALLOWED_PAYMENT_MIME = {'image/jpeg', 'image/png', 'image/webp'}


def _ensure_pro_user(user: dict) -> None:
    expires_at = user.get('plan_expires_at')
    if str(user.get('plan') or 'Free') != 'Pro' or expires_at is None or expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail='Vui lòng nâng cấp lên gói Pro để follow tin tức và nhận thông báo tin mới qua telegram!')


def _valid_image_signature(content: bytes, content_type: str) -> bool:
    if content_type == 'image/jpeg': return content.startswith(b'\xff\xd8\xff')
    if content_type == 'image/png': return content.startswith(b'\x89PNG\r\n\x1a\n')
    if content_type == 'image/webp': return len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP'
    return False


def _get_plan_config_row():
    try:
        return fetch_one("""SELECT payment_qr_data, payment_qr_object_key, payment_qr_mime_type,
                                 payment_qr_filename, payment_qr_override, pro_plan_description,
                                 pro_plan_price_label, updated_at
                          FROM app_runtime_settings WHERE setting_key='pro_plan'""")
    except Exception:
        # Preserve the legacy environment-based configuration if migration 019/020
        # has not been applied yet. Admin save routes still clearly require them.
        log.warning('Plan runtime settings unavailable; using environment defaults', exc_info=True)
        return None


def _effective_plan_config(request: Request | None = None) -> dict:
    row = _get_plan_config_row() or {}
    has_db_qr = bool(row.get('payment_qr_object_key') or row.get('payment_qr_data'))
    env_qr = (settings.payment_qr_image_url or '').strip() or None
    fallback_qr = None if bool(row.get('payment_qr_override')) else env_qr
    qr_url = None
    if has_db_qr and request is not None:
        version = row.get('updated_at')
        version_key = str(int(version.timestamp())) if hasattr(version, 'timestamp') else 'current'
        configured_base = (settings.public_app_url or '').strip().rstrip('/')
        if not configured_base or 'YOUR-NEWS-RADAR-DOMAIN' in configured_base or not configured_base.startswith('https://'):
            configured_base = str(request.base_url).rstrip('/')
        qr_url = f"{configured_base}/api/public/plan-config/qr?v={version_key}"
    elif fallback_qr:
        qr_url = fallback_qr
    return {
        'pro_price_label': (row.get('pro_plan_price_label') or '').strip() or settings.pro_plan_price_label or None,
        'qr_image_url': qr_url,
        'qr_configured': has_db_qr or bool(fallback_qr),
        'payment_instructions': row.get('pro_plan_description') if row.get('pro_plan_description') is not None else settings.payment_instructions,
        'qr_data': row.get('payment_qr_data'),
        'qr_object_key': row.get('payment_qr_object_key'),
        'qr_mime_type': row.get('payment_qr_mime_type'),
        'qr_filename': row.get('payment_qr_filename'),
        'updated_at': row.get('updated_at'),
    }


@app.get('/api/public/plan-config/qr')
def public_plan_qr():
    config = _get_plan_config_row() or {}
    object_key = config.get('payment_qr_object_key')
    image_data = config.get('payment_qr_data')
    image_type = config.get('payment_qr_mime_type')
    if object_key:
        try:
            image_data = get_r2_bytes(object_key)
        except R2NotConfigured as exc:
            log.error('R2 is not configured while serving Pro payment QR')
            raise HTTPException(status_code=503, detail='Chưa cấu hình Cloudflare R2 để hiển thị mã QR.') from exc
        except Exception as exc:
            log.exception('Could not retrieve Pro payment QR from R2')
            raise HTTPException(status_code=503, detail='Không tải được mã QR từ Cloudflare R2. Vui lòng thử lại sau.') from exc
    if image_data and image_type in ALLOWED_PAYMENT_MIME:
        return Response(content=bytes(image_data), media_type=image_type,
                        headers={'Cache-Control': 'public, max-age=300', 'X-Content-Type-Options': 'nosniff'})
    if not config.get('payment_qr_override') and settings.payment_qr_image_url:
        return RedirectResponse(settings.payment_qr_image_url, status_code=307)
    raise HTTPException(status_code=404, detail='QR thanh toán chưa được cấu hình.')


@app.get('/api/user/plan')
def get_user_plan(request: Request, user: dict = Depends(require_user)):
    latest = fetch_one("""SELECT payment_request_id, requested_plan, status, note, created_at, reviewed_at, admin_note
                          FROM payment_requests WHERE user_id=%s ORDER BY created_at DESC, payment_request_id DESC LIMIT 1""", (user['user_id'],))
    config = _effective_plan_config(request)
    return {
        'plan': user.get('plan') or 'Free',
        'plan_expires_at': user.get('plan_expires_at'),
        'pro_price_label': config['pro_price_label'],
        'qr_image_url': config['qr_image_url'],
        'payment_instructions': config['payment_instructions'],
        'payment_request': latest,
        'can_follow': user.get('plan') == 'Pro',
    }


@app.get('/api/admin/plan-config', dependencies=[Depends(require_admin)])
def get_admin_plan_config(request: Request):
    row = fetch_one("""SELECT payment_qr_data, payment_qr_object_key, payment_qr_mime_type,
                             payment_qr_filename, payment_qr_override, pro_plan_description,
                             pro_plan_price_label, updated_at
                      FROM app_runtime_settings WHERE setting_key='pro_plan'""")
    config = _effective_plan_config(request)
    has_saved_qr = bool((row or {}).get('payment_qr_object_key') or (row or {}).get('payment_qr_data'))
    return {
        'description': (row.get('pro_plan_description') if row and row.get('pro_plan_description') is not None else settings.payment_instructions),
        'price_label': ((row.get('pro_plan_price_label') or settings.pro_plan_price_label or '') if row else settings.pro_plan_price_label),
        'qr_configured': has_saved_qr or (not bool((row or {}).get('payment_qr_override')) and bool(settings.payment_qr_image_url)),
        'qr_mime_type': (row or {}).get('payment_qr_mime_type'),
        'qr_filename': (row or {}).get('payment_qr_filename'),
        'qr_image_url': config['qr_image_url'],
        'updated_at': (row or {}).get('updated_at'),
        'qr_source': 'cloudflare-r2' if (row or {}).get('payment_qr_object_key') else ('legacy-database' if (row or {}).get('payment_qr_data') else ('environment' if not bool((row or {}).get('payment_qr_override')) and settings.payment_qr_image_url else 'none')),
    }


@app.put('/api/admin/plan-config', dependencies=[Depends(require_admin)])
async def update_admin_plan_config(
    description: str = Form(default='', max_length=5000),
    price_label: str = Form(default='', max_length=120),
    qr: UploadFile | None = File(default=None),
    clear_qr: bool = Form(default=False),
):
    content = None
    content_type = None
    filename = None
    uploaded_key = None
    old_key = None
    if qr is not None and qr.filename:
        content_type = (qr.content_type or '').lower().split(';', 1)[0].strip()
        if content_type not in ALLOWED_PAYMENT_MIME:
            raise HTTPException(status_code=415, detail='QR chỉ chấp nhận ảnh JPG, PNG hoặc WebP.')
        content = await qr.read(MAX_PAYMENT_PROOF_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail='File QR đang trống.')
        if len(content) > MAX_PAYMENT_PROOF_BYTES:
            raise HTTPException(status_code=413, detail='Ảnh QR vượt quá 3 MB.')
        if not _valid_image_signature(content, content_type):
            raise HTTPException(status_code=415, detail='Nội dung file không khớp với định dạng ảnh.')
        filename = (qr.filename or 'pro-plan-qr').replace('\\', '/').split('/')[-1][:180]
    try:
        # Upload first, before opening a DB transaction. The bucket stays private;
        # users receive the QR through /api/public/plan-config/qr.
        if content is not None and not clear_qr:
            if not r2_is_configured():
                raise HTTPException(status_code=503, detail='Cloudflare R2 chưa được cấu hình. Hãy thiết lập các biến CLOUDFLARE_R2_* trong Render Environment.')
            extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}[content_type]
            uploaded_key = make_r2_key('payment-qr', f"pro-plan-{secrets.token_hex(16)}{extension}")
            put_r2_bytes(uploaded_key, content, content_type)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT payment_qr_data, payment_qr_object_key, payment_qr_mime_type,
                                  payment_qr_filename, payment_qr_override
                              FROM app_runtime_settings WHERE setting_key='pro_plan' FOR UPDATE""")
            current = cur.fetchone()
            if not current:
                raise HTTPException(status_code=503, detail='Chưa có cấu hình gói Pro. Hãy chạy migration 019 và 020 trong Neon SQL Editor.')
            current_data = current.get('payment_qr_data')
            current_key = current.get('payment_qr_object_key')
            current_type = current.get('payment_qr_mime_type')
            current_name = current.get('payment_qr_filename')
            current_override = bool(current.get('payment_qr_override'))
            old_key = current_key
            if clear_qr:
                current_data = current_key = current_type = current_name = None
                current_override = True
            elif content is not None:
                current_data = None
                current_key = uploaded_key
                current_type, current_name = content_type, filename
                current_override = True
            cur.execute("""UPDATE app_runtime_settings
                              SET payment_qr_data=%s, payment_qr_object_key=%s,
                                  payment_qr_mime_type=%s, payment_qr_filename=%s,
                                  payment_qr_override=%s, pro_plan_description=%s,
                                  pro_plan_price_label=%s, updated_at=now()
                              WHERE setting_key='pro_plan'""",
                        (current_data, current_key, current_type, current_name, current_override, description.strip(), price_label.strip()))
    except HTTPException:
        if uploaded_key:
            delete_r2_object(uploaded_key)
        raise
    except R2NotConfigured as exc:
        if uploaded_key:
            delete_r2_object(uploaded_key)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        if uploaded_key:
            delete_r2_object(uploaded_key)
        log.exception('Could not save Pro plan configuration')
        raise HTTPException(status_code=503, detail='Không lưu được cấu hình. Hãy kiểm tra migration 019/020 và cấu hình Cloudflare R2.') from exc
    if old_key and (clear_qr or (uploaded_key and old_key != uploaded_key)):
        delete_r2_object(old_key)
    return {'ok': True, 'message': 'Đã lưu cấu hình gói Pro. Ảnh QR được lưu trong Cloudflare R2.'}


@app.post('/api/user/payment-requests')
async def submit_payment_request(
    proof: UploadFile = File(...),
    note: str = Form(default='', max_length=1000),
    user: dict = Depends(require_user),
):
    if user.get('plan') == 'Pro':
        raise HTTPException(status_code=409, detail='Tài khoản của bạn đang ở gói Pro.')
    if not _effective_plan_config().get('qr_configured'):
        raise HTTPException(status_code=503, detail='Admin chưa cấu hình mã QR thanh toán. Vui lòng quay lại sau.')
    if not r2_is_configured():
        raise HTTPException(status_code=503, detail='Cloudflare R2 chưa được cấu hình. Vui lòng liên hệ admin.')
    content_type = (proof.content_type or '').lower().split(';', 1)[0].strip()
    if content_type not in ALLOWED_PAYMENT_MIME:
        raise HTTPException(status_code=415, detail='Chỉ chấp nhận ảnh JPG, PNG hoặc WebP làm bằng chứng thanh toán.')
    content = await proof.read(MAX_PAYMENT_PROOF_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail='Ảnh bằng chứng đang trống.')
    if len(content) > MAX_PAYMENT_PROOF_BYTES:
        raise HTTPException(status_code=413, detail='Ảnh quá lớn. Vui lòng chọn ảnh nhỏ hơn 3 MB.')
    if not _valid_image_signature(content, content_type):
        raise HTTPException(status_code=415, detail='Nội dung file không khớp với định dạng ảnh được hỗ trợ.')
    filename = (proof.filename or 'payment-proof').replace('\\', '/').split('/')[-1][:180]
    extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}[content_type]
    object_key = make_r2_key('payment-proofs', f"{secrets.token_hex(16)}{extension}", scope=str(user['user_id']))
    try:
        put_r2_bytes(object_key, content, content_type)
        with get_conn() as conn, conn.cursor() as cur:
            # Do not block repeated notifications while a prior request is pending.
            # The review queue keeps each request separately with its own evidence.
            cur.execute("""INSERT INTO payment_requests
                (user_id, requested_plan, status, note, proof_filename, proof_mime_type, proof_data, proof_object_key)
                VALUES (%s, 'Pro', 'pending', %s, %s, %s, NULL, %s)
                RETURNING payment_request_id, status, created_at""",
                (user['user_id'], note.strip(), filename, content_type, object_key))
            row = cur.fetchone()
    except HTTPException:
        delete_r2_object(object_key)
        raise
    except Exception as exc:
        delete_r2_object(object_key)
        log.exception('Could not save Pro payment request')
        raise HTTPException(status_code=503, detail='Không thể lưu yêu cầu thanh toán. Hãy kiểm tra migration SQL 020 và cấu hình Cloudflare R2.') from exc
    return {'ok': True, 'payment_request': row, 'message': 'Đã thông báo admin và gửi ảnh bằng chứng. Bạn vẫn có thể gửi thông báo/bằng chứng bổ sung nếu cần; Pro chỉ được kích hoạt sau khi admin xác nhận.'}


@app.post('/api/user-auth/forgot-password')
def forgot_password(body: ForgotPasswordBody, request: Request):
    # Prefer the Brevo HTTPS API. Render Free blocks outbound SMTP ports 25/465/587,
    # so SMTP fallback is only useful on hosts/instances that permit outbound SMTP.
    if not (settings.smtp_from_email and (settings.brevo_api_key or settings.smtp_host)):
        raise HTTPException(status_code=503, detail='Chức năng gửi email chưa được cấu hình. Hãy cấu hình BREVO_API_KEY và SMTP_FROM_EMAIL trên Render.')
    email = body.email.strip().lower()
    user = fetch_one("SELECT user_id, username, email FROM app_users WHERE email_normalized=%s AND is_active=true", (email,))
    # Return the same result whether an account exists or not, avoiding account enumeration.
    if user:
        recent = fetch_one("SELECT count(*)::int AS n FROM user_password_reset_tokens WHERE user_id=%s AND created_at>now()-interval '2 minutes'", (user['user_id'],))
        if int((recent or {}).get('n') or 0) >= 1:
            return {'ok': True, 'message': 'Nếu email đã đăng ký trong hệ thống, hướng dẫn đặt lại mật khẩu sẽ được gửi đến hộp thư.'}
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
        try:
            fetch_one("DELETE FROM user_password_reset_tokens WHERE user_id=%s AND consumed_at IS NULL RETURNING token_hash", (user['user_id'],))
            fetch_one("""INSERT INTO user_password_reset_tokens(token_hash,user_id,expires_at)
                         VALUES (%s,%s,now()+interval '30 minutes') RETURNING token_hash""", (token_hash, user['user_id']))
            base = settings.public_app_url or str(request.base_url).rstrip('/')
            reset_url = f"{base}/news#reset_token={raw_token}"
            text_body = f"Xin chào {user['username']},\n\nBạn đã yêu cầu đặt lại mật khẩu News Radar. Mở liên kết sau trong vòng 30 phút:\n{reset_url}\n\nNếu bạn không yêu cầu, hãy bỏ qua email này."
            if settings.brevo_api_key:
                # HTTPS API avoids Render Free's outbound SMTP port restrictions.
                response = requests.post(
                    'https://api.brevo.com/v3/smtp/email',
                    headers={
                        'accept': 'application/json',
                        'api-key': settings.brevo_api_key,
                        'content-type': 'application/json',
                    },
                    json={
                        'sender': {'name': 'News Radar', 'email': settings.smtp_from_email},
                        'to': [{'email': user['email'], 'name': user['username']}],
                        'subject': 'Đặt lại mật khẩu News Radar',
                        'textContent': text_body,
                    },
                    timeout=20,
                )
                if not response.ok:
                    # Include Brevo's safe response detail in server logs, never expose secrets to the user.
                    raise RuntimeError(f'Brevo API returned HTTP {response.status_code}: {response.text[:500]}')
                try:
                    brevo_result = response.json()
                except ValueError:
                    brevo_result = {}
                log.info('Password reset email accepted by Brevo API; message_id=%s', brevo_result.get('messageId', 'n/a'))
            else:
                # SMTP fallback for paid hosts where outbound SMTP is available.
                message = EmailMessage()
                message['Subject'] = 'Đặt lại mật khẩu News Radar'
                message['From'] = settings.smtp_from_email
                message['To'] = user['email']
                message.set_content(text_body)
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                    if settings.smtp_starttls:
                        smtp.starttls(context=ssl.create_default_context())
                    if settings.smtp_username:
                        smtp.login(settings.smtp_username, settings.smtp_password)
                    smtp.send_message(message)
        except Exception as exc:
            log.exception('Password reset email could not be sent')
            try: fetch_one("DELETE FROM user_password_reset_tokens WHERE token_hash=%s RETURNING token_hash", (token_hash,))
            except Exception: log.exception('Could not remove unused password reset token')
            raise HTTPException(status_code=503, detail='Không gửi được email đặt lại mật khẩu. Kiểm tra BREVO_API_KEY, email Sender và cấu hình gửi email.') from exc
    return {'ok': True, 'message': 'Nếu email đã đăng ký trong hệ thống, hướng dẫn đặt lại mật khẩu sẽ được gửi đến hộp thư.'}


@app.post('/api/user-auth/reset-password')
def reset_password(body: ResetPasswordBody):
    token_hash = hashlib.sha256(body.token.encode('utf-8')).hexdigest()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT user_id FROM user_password_reset_tokens
                           WHERE token_hash=%s AND consumed_at IS NULL AND expires_at>now() FOR UPDATE""", (token_hash,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail='Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn. Hãy yêu cầu liên kết mới.')
            cur.execute("UPDATE app_users SET password_hash=%s WHERE user_id=%s", (hash_password(body.new_password), row['user_id']))
            cur.execute("UPDATE user_password_reset_tokens SET consumed_at=now() WHERE token_hash=%s", (token_hash,))
            cur.execute("UPDATE user_password_reset_tokens SET consumed_at=now() WHERE user_id=%s AND consumed_at IS NULL", (row['user_id'],))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('Password reset failed')
        raise HTTPException(status_code=503, detail='Không thể đổi mật khẩu lúc này. Vui lòng thử lại sau.') from exc
    return {'ok': True, 'message': 'Đã đổi mật khẩu. Bạn có thể đăng nhập bằng mật khẩu mới.'}


@app.get('/api/admin/payment-summary', dependencies=[Depends(require_admin)])
def admin_payment_summary():
    row = fetch_one("SELECT count(*)::int AS pending_count FROM payment_requests WHERE status='pending'")
    return {'pending_count': int((row or {}).get('pending_count') or 0)}


@app.get('/api/admin/payment-requests', dependencies=[Depends(require_admin)])
def admin_payment_requests(status: str = Query(default='pending', regex='^(pending|approved|rejected|all)$'), limit: int = Query(default=100, ge=1, le=500)):
    where = '' if status == 'all' else 'WHERE pr.status=%s'
    params = () if status == 'all' else (status,)
    return fetch_all(f"""SELECT pr.payment_request_id,pr.user_id,u.username,u.email,pr.requested_plan,pr.status,
                                pr.note,pr.created_at,pr.reviewed_at,pr.admin_note,
                                (pr.proof_data IS NOT NULL OR pr.proof_object_key IS NOT NULL) AS has_proof
                         FROM payment_requests pr JOIN app_users u ON u.user_id=pr.user_id
                         {where}
                         ORDER BY CASE WHEN pr.status='pending' THEN 0 ELSE 1 END, pr.created_at DESC
                         LIMIT %s""", (*params, limit))


@app.get('/api/admin/payment-requests/{payment_request_id}/proof', dependencies=[Depends(require_admin)])
def admin_payment_proof(payment_request_id: int):
    row = fetch_one("SELECT proof_data,proof_object_key,proof_mime_type,proof_filename FROM payment_requests WHERE payment_request_id=%s", (payment_request_id,))
    if not row or (not row.get('proof_object_key') and not row.get('proof_data')):
        raise HTTPException(status_code=404, detail='Không tìm thấy ảnh bằng chứng thanh toán.')
    image_data = row.get('proof_data')
    if row.get('proof_object_key'):
        try:
            image_data = get_r2_bytes(row['proof_object_key'])
        except R2NotConfigured as exc:
            raise HTTPException(status_code=503, detail='Cloudflare R2 chưa được cấu hình trên Render.') from exc
        except Exception as exc:
            log.exception('Could not retrieve payment evidence from private R2 bucket')
            raise HTTPException(status_code=503, detail='Không tải được ảnh bằng chứng từ Cloudflare R2.') from exc
    filename = re.sub(r'[^A-Za-z0-9_.-]', '_', row.get('proof_filename') or 'payment-proof')
    return Response(content=bytes(image_data), media_type=row.get('proof_mime_type') or 'application/octet-stream',
                    headers={'Content-Disposition': f'inline; filename="{filename}"', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.post('/api/admin/payment-requests/{payment_request_id}/review', dependencies=[Depends(require_admin)])
def review_payment_request(payment_request_id: int, body: PaymentReviewBody):
    plan_expires_at = None
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT payment_request_id,user_id,status FROM payment_requests WHERE payment_request_id=%s FOR UPDATE", (payment_request_id,))
            row = cur.fetchone()
            if not row: raise HTTPException(status_code=404, detail='Không tìm thấy yêu cầu thanh toán.')
            if row['status'] != 'pending': raise HTTPException(status_code=409, detail='Yêu cầu này đã được xử lý trước đó.')
            next_status = 'approved' if body.decision == 'approve' else 'rejected'
            cur.execute("UPDATE payment_requests SET status=%s,reviewed_at=now(),reviewed_by=%s,admin_note=%s WHERE payment_request_id=%s",
                        (next_status, settings.admin_username, body.note.strip(), payment_request_id))
            if body.decision == 'approve':
                cur.execute("UPDATE app_users SET plan='Pro',plan_expires_at=now()+interval '6 months',plan_updated_at=now(),plan_updated_by=%s WHERE user_id=%s RETURNING plan_expires_at", (settings.admin_username, row['user_id']))
                plan_expires_at = cur.fetchone()['plan_expires_at']
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('Payment request review failed')
        raise HTTPException(status_code=503, detail='Không thể duyệt thanh toán. Kiểm tra migration SQL 018.') from exc
    return {'ok': True, 'status': next_status, 'plan': 'Pro' if body.decision == 'approve' else None, 'plan_expires_at': plan_expires_at}


@app.get('/api/admin/users', dependencies=[Depends(require_admin)])
def admin_list_users(q: str | None = None, limit: int = Query(default=200, ge=1, le=500)):
    expire_pro_plans()
    search = (q or '').strip()
    where = 'WHERE (u.username ILIKE %s OR u.email ILIKE %s)' if search else ''
    params = (f'%{search}%', f'%{search}%') if search else ()
    return fetch_all(f"""WITH latest_payment AS (
                           SELECT DISTINCT ON (user_id) payment_request_id,user_id,status,requested_plan,created_at
                           FROM payment_requests ORDER BY user_id,created_at DESC,payment_request_id DESC
                         )
                         SELECT u.user_id,u.username,u.email,u.plan,u.plan_expires_at,u.is_active,u.created_at,u.last_login_at,
                                lp.payment_request_id,lp.status AS payment_status,lp.requested_plan,lp.created_at AS payment_created_at
                         FROM app_users u LEFT JOIN latest_payment lp ON lp.user_id=u.user_id
                         {where}
                         ORDER BY CASE WHEN lp.status='pending' THEN 0 ELSE 1 END, lp.created_at DESC NULLS LAST, u.created_at DESC
                         LIMIT %s""", (*params, limit))


@app.patch('/api/admin/users/{user_id}/plan', dependencies=[Depends(require_admin)])
def admin_set_user_plan(user_id: int, body: UserPlanBody):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE app_users
                           SET plan=%s,
                               plan_expires_at=CASE WHEN %s='Pro' THEN now()+interval '6 months' ELSE NULL END,
                               plan_updated_at=now(), plan_updated_by=%s,
                               telegram_enabled=CASE WHEN %s='Free' THEN false ELSE telegram_enabled END,
                               telegram_notifications_since=CASE WHEN %s='Free' THEN NULL ELSE telegram_notifications_since END
                           WHERE user_id=%s
                           RETURNING user_id,username,email,plan,plan_expires_at""",
                        (body.plan, body.plan, settings.admin_username, body.plan, body.plan, user_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Không tìm thấy tài khoản người dùng.')
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('Could not manually change user plan')
        raise HTTPException(status_code=503, detail='Không thể cập nhật gói. Kiểm tra migration SQL 018.') from exc
    return {'ok': True, 'user': row}


@app.get('/api/user/timeline-options')
def user_timeline_options(user: dict = Depends(require_user)):
    _ensure_pro_user(user)
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
    if user.get('plan') != 'Pro':
        return {'items': []}
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
    if user.get('plan') != 'Pro' and items:
        existing = fetch_all("SELECT timeline_type AS type,coalesce(topic_id,entity_id)::bigint AS id FROM user_saved_timelines WHERE user_id=%s", (user['user_id'],))
        existing_keys = {(str(x['type']), int(x['id'])) for x in existing}
        if any((x.type, x.id) not in existing_keys for x in items):
            raise HTTPException(status_code=403, detail='Vui lòng nâng cấp lên gói Pro để follow tin tức và nhận thông báo tin mới qua telegram!')
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
    _ensure_pro_user(user)
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
    _ensure_pro_user(user)
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
    _ensure_pro_user(user)
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
                          group by t.topic_id,t.name order by article_count desc limit 100""")
    top_topics = [item for item in top_topics if not is_broad_field_topic(item.get('name'))][:10]
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
    hot_topics = [item for item in hot_topics if not is_broad_field_topic(item.get('topic_name'))]
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


@app.get('/api/public/news/entity-cloud')
def public_entity_cloud(
    limit: int = Query(default=20, ge=5, le=20),
):
    """Return the top 20 story entities in the rolling 24-hour news window.

    Counts distinct included articles (not raw mentions); publisher/source labels are excluded
    because source names are metadata, not story entities users should follow.
    """
    candidates = fetch_all("""SELECT e.entity_id,
                              e.canonical_name AS name,
                              e.entity_type,
                              coalesce(e.description, '') AS description,
                              count(DISTINCT a.article_id)::int AS article_count,
                              count(DISTINCT a.source_id)::int AS source_count,
                              max(coalesce(a.published_at, a.collected_at)) AS latest_at
                       FROM entities e
                       JOIN article_entities ae ON ae.entity_id=e.entity_id
                       JOIN news_articles a ON a.article_id=ae.article_id
                       WHERE e.status='active'
                         AND nullif(btrim(e.canonical_name), '') IS NOT NULL
                         AND a.is_excluded=false
                         AND (a.published_at >= now() - interval '24 hours'
                              OR (a.published_at IS NULL AND a.collected_at >= now() - interval '24 hours'))
                       GROUP BY e.entity_id, e.canonical_name, e.entity_type, e.description
                       ORDER BY count(DISTINCT a.article_id) DESC,
                                max(coalesce(a.published_at, a.collected_at)) DESC,
                                e.canonical_name ASC
                       LIMIT 300""")
    items = [item for item in candidates
             if should_track_entity(str(item.get('name') or ''), str(item.get('entity_type') or 'UNKNOWN'))
             and not is_media_source_entity(str(item.get('name') or ''), str(item.get('entity_type') or 'UNKNOWN'))][:limit]
    return {'window_hours': 24, 'items': items, 'count': len(items), 'limit': limit}


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
                              f.hotness_score, f.rank_position,
                              (SELECT at.article_id
                               FROM article_topics at
                               JOIN news_articles anchor ON anchor.article_id=at.article_id
                               WHERE at.topic_id=t.topic_id AND anchor.is_excluded=false
                               ORDER BY coalesce(anchor.published_at,anchor.collected_at) DESC,
                                        anchor.article_id DESC
                               LIMIT 1) AS anchor_article_id
                       FROM fact_topic_trending f
                       JOIN topics t ON t.topic_id=f.topic_id
                       WHERE f.period_type=%s AND f.period_start=%s
                         AND t.status='active' AND t.topic_type='event'
                       ORDER BY f.rank_position ASC, f.hotness_score DESC
                       LIMIT %s""", (period, period_start, max(100, min(600, limit * 20))))
    items = [item for item in items if not is_broad_field_topic(item.get('topic_name'))][:limit]
    return {'period': period, 'period_start': period_start, 'period_label': label,
            'items': items, 'count': len(items)}


@app.get('/api/public/news/articles')
def public_daily_articles(
    q: str | None = Query(default=None, max_length=150),
    field_category: str | None = Query(default=None, max_length=60),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    progressive: bool = Query(default=False),
):
    """List daily articles/search results; progressive mode avoids the expensive total-count query."""
    local = fetch_one("select (now() at time zone %s)::date as today", (settings.timezone,))
    today = (local or {}).get('today')
    if today is None:
        raise HTTPException(status_code=503, detail='Could not resolve local date')

    query = (q or '').strip()[:150]
    chosen_field = (field_category or '').strip()
    allowed_fields = set(FIELD_CATEGORIES) | {'Chưa phân loại'}
    if chosen_field and chosen_field not in allowed_fields:
        raise HTTPException(status_code=400, detail='Lĩnh vực không hợp lệ')

    # The public headlines list defaults to today in the configured Vietnam timezone.
    # Explicit dates also work with search requests, and bounds are inclusive. If only
    # one bound is supplied, interpret it as a single-day filter.
    effective_date_from = date_from
    effective_date_to = date_to
    if effective_date_from is None and effective_date_to is None and not query:
        effective_date_from = effective_date_to = today
    elif effective_date_from is None and effective_date_to is not None:
        effective_date_from = effective_date_to
    elif effective_date_to is None and effective_date_from is not None:
        effective_date_to = effective_date_from
    if effective_date_from is not None and effective_date_to is not None and effective_date_from > effective_date_to:
        raise HTTPException(status_code=400, detail='Ngày bắt đầu không được sau ngày kết thúc')

    where = ["a.is_excluded=false"]
    vals: list = []
    if chosen_field == 'Chưa phân loại':
        where.append('a.field_category IS NULL')
    elif chosen_field:
        where.append('a.field_category=%s')
        vals.append(chosen_field)
    if query:
        term = f"%{query}%"
        # A search query spans the stored archive unless the caller explicitly supplies
        # date bounds. Excluded/low-quality records remain hidden from public results.
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

        )""")
        vals.extend([term] * 8)
    if effective_date_from is not None and effective_date_to is not None:
        where.append("(coalesce(a.published_at,a.collected_at) at time zone %s)::date BETWEEN %s AND %s")
        vals.extend([settings.timezone, effective_date_from, effective_date_to])

    from_sql = " FROM news_articles a JOIN data_sources s ON s.source_id=a.source_id"
    where_sql = " WHERE " + " AND ".join(where)

    # Cross-source duplicates have different canonical URLs, so the ingestion unique
    # constraint intentionally retains each source's original record. Public list/search
    # results collapse exact normalized headline hashes within a rolling 72-hour window.
    # The representative is chosen deterministically: higher quality, image available,
    # richer summary, then newest publication/collection time. Only rows matching the
    # current filters compete, so source/content-specific searches still work as expected.
    dedupe_cte = """WITH filtered_candidates AS (
          SELECT a.article_id, a.headline_hash,
                 coalesce(a.quality_score, 0) AS quality_score,
                 (a.image_url IS NOT NULL) AS has_image,
                 length(coalesce(a.summary, '')) AS summary_length,
                 coalesce(a.published_at, a.collected_at) AS timeline_at
          """ + from_sql + where_sql + """
        ), deduped_candidates AS (
          SELECT candidate.article_id
          FROM filtered_candidates candidate
          WHERE NOT EXISTS (
            SELECT 1
            FROM filtered_candidates other
            WHERE other.headline_hash = candidate.headline_hash
              AND other.article_id <> candidate.article_id
              AND other.timeline_at >= candidate.timeline_at - INTERVAL '72 hours'
              AND other.timeline_at <= candidate.timeline_at + INTERVAL '72 hours'
              AND (
                other.quality_score > candidate.quality_score
                OR (other.quality_score = candidate.quality_score
                    AND other.has_image AND NOT candidate.has_image)
                OR (other.quality_score = candidate.quality_score
                    AND other.has_image = candidate.has_image
                    AND other.summary_length > candidate.summary_length)
                OR (other.quality_score = candidate.quality_score
                    AND other.has_image = candidate.has_image
                    AND other.summary_length = candidate.summary_length
                    AND other.timeline_at > candidate.timeline_at)
                OR (other.quality_score = candidate.quality_score
                    AND other.has_image = candidate.has_image
                    AND other.summary_length = candidate.summary_length
                    AND other.timeline_at = candidate.timeline_at
                    AND other.article_id > candidate.article_id)
              )
          )
        ) """

    # Progressive public-list requests skip an exact COUNT over the deduplicated
    # candidate set. They fetch one extra row to determine whether another batch exists.
    # The legacy paginated API keeps exact totals unless progressive=true is requested.
    total = None
    total_pages = None
    if not progressive:
        total_row = fetch_one(dedupe_cte + " SELECT count(*)::int AS total FROM deduped_candidates", vals)
        total = int((total_row or {}).get('total', 0))
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
    query_limit = page_size + 1 if progressive else page_size
    items = fetch_all(dedupe_cte + """SELECT a.article_id, a.title, a.url, a.image_url, a.author, a.language,
                              left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
                              a.published_at, a.collected_at,
                              coalesce(a.published_at,a.collected_at) AS timeline_at,
                              s.name AS source_name,
                              coalesce(a.field_category,'Chưa phân loại') AS field_category,
                              coalesce((SELECT json_agg(t.name ORDER BY t.name)
                                        FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                                        WHERE at.article_id=a.article_id AND t.status='active' AND t.topic_type='event'), '[]'::json) AS topics,
                              coalesce((SELECT json_agg(json_build_object('id', t.topic_id, 'name', t.name) ORDER BY t.name)
                                        FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                                        WHERE at.article_id=a.article_id AND t.status='active' AND t.topic_type='event'), '[]'::json) AS topic_refs,
                              coalesce((SELECT json_agg(json_build_object(
                                                'id', e.entity_id, 'name', e.canonical_name, 'type', e.entity_type,
                                                '_mention_text', ae.mention_text,
                                                '_aliases', coalesce((SELECT json_agg(ea.alias ORDER BY ea.alias)
                                                                     FROM entity_aliases ea WHERE ea.entity_id=e.entity_id), '[]'::json)
                                              ) ORDER BY e.canonical_name)
                                        FROM article_entities ae
                                        JOIN entities e ON e.entity_id=ae.entity_id AND e.status='active'
                                        WHERE ae.article_id=a.article_id), '[]'::json) AS entity_refs,
                              coalesce(a.summary,'') AS _entity_grounding_summary
                       FROM news_articles a
                       JOIN data_sources s ON s.source_id=a.source_id
                       JOIN deduped_candidates dc ON dc.article_id=a.article_id
                       ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC
                       LIMIT %s OFFSET %s""",
                       vals + [query_limit, (page - 1) * page_size])
    has_more = len(items) > page_size if progressive else page < (total_pages or 1)
    if progressive and has_more:
        items = items[:page_size]
    # Stored article_entities rows can be stale after earlier reconciliation runs. Never
    # expose an entity action/tag unless the entity is grounded in this article's title/summary.
    for item in items:
        item['entity_refs'] = filter_grounded_entity_references(
            item.get('title') or '', item.pop('_entity_grounding_summary', ''), item.get('entity_refs') or []
        )
    pagination = ({'page': page, 'page_size': page_size, 'total': None,
                   'total_pages': None,
                   'start': (page - 1) * page_size + 1 if items else 0,
                   'end': (page - 1) * page_size + len(items)}
                  if progressive else pagination_meta(page, page_size, int(total or 0)))
    return {'date': today,
            'date_from': effective_date_from.isoformat() if effective_date_from else None,
            'date_to': effective_date_to.isoformat() if effective_date_to else None,
            'query': query or None, 'field_category': chosen_field or None,
            'field_categories': [*FIELD_CATEGORIES, 'Chưa phân loại'], 'items': items,
            'has_more': has_more if progressive else None,
            'pagination': pagination}


@app.get('/api/user/feed')
def user_news_feed(user: dict = Depends(require_user)):
    """Return followed topic/entity groups with at most five latest articles per group."""
    _ensure_pro_user(user)
    rows = fetch_all("""WITH followed AS (
          SELECT 'topic'::text AS follow_type, t.topic_id::bigint AS follow_id,
                 t.name AS follow_name, ust.display_order
          FROM user_saved_timelines ust
          JOIN topics t ON t.topic_id=ust.topic_id
          WHERE ust.user_id=%s AND ust.timeline_type='topic'
            AND t.status='active' AND t.topic_type='event'
          UNION ALL
          SELECT 'entity'::text AS follow_type, e.entity_id::bigint AS follow_id,
                 e.canonical_name AS follow_name, ust.display_order
          FROM user_saved_timelines ust
          JOIN entities e ON e.entity_id=ust.entity_id
          WHERE ust.user_id=%s AND ust.timeline_type='entity' AND e.status='active'
        ), matched AS (
          SELECT f.follow_type, f.follow_id, f.follow_name, f.display_order,
                 a.article_id, a.title, a.url, a.image_url, a.author, a.language,
                 left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
                 a.published_at, a.collected_at,
                 coalesce(a.published_at,a.collected_at) AS timeline_at,
                 s.name AS source_name
          FROM followed f
          JOIN article_topics at ON f.follow_type='topic' AND at.topic_id=f.follow_id
          JOIN news_articles a ON a.article_id=at.article_id AND a.is_excluded=false
          JOIN data_sources s ON s.source_id=a.source_id
          UNION ALL
          SELECT f.follow_type, f.follow_id, f.follow_name, f.display_order,
                 a.article_id, a.title, a.url, a.image_url, a.author, a.language,
                 left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet,
                 a.published_at, a.collected_at,
                 coalesce(a.published_at,a.collected_at) AS timeline_at,
                 s.name AS source_name
          FROM followed f
          JOIN article_entities ae ON f.follow_type='entity' AND ae.entity_id=f.follow_id
          JOIN news_articles a ON a.article_id=ae.article_id AND a.is_excluded=false
          JOIN data_sources s ON s.source_id=a.source_id
        ), ranked AS (
          SELECT m.*,
                 count(*) OVER (PARTITION BY follow_type, follow_id)::int AS total_count,
                 row_number() OVER (PARTITION BY follow_type, follow_id
                                    ORDER BY timeline_at DESC, article_id DESC) AS row_num
          FROM matched m
        ), grouped AS (
          SELECT follow_type, follow_id, max(total_count)::int AS total_count,
                 coalesce(jsonb_agg(jsonb_build_object(
                   'article_id', article_id, 'title', title, 'url', url, 'image_url', image_url, 'author', author,
                   'language', language, 'snippet', snippet, 'published_at', published_at,
                   'collected_at', collected_at, 'timeline_at', timeline_at,
                   'source_name', source_name,
                   'matched_topics', CASE WHEN follow_type='topic'
                     THEN jsonb_build_array(jsonb_build_object('id', follow_id, 'name', follow_name))
                     ELSE '[]'::jsonb END,
                   'matched_entities', CASE WHEN follow_type='entity'
                     THEN jsonb_build_array(jsonb_build_object('id', follow_id, 'name', follow_name))
                     ELSE '[]'::jsonb END
                 ) ORDER BY timeline_at DESC, article_id DESC)
                   FILTER (WHERE row_num <= 5), '[]'::jsonb) AS items
          FROM ranked
          GROUP BY follow_type, follow_id
        )
        SELECT f.follow_type AS type, f.follow_id AS id, f.follow_name AS name,
               f.display_order, coalesce(g.total_count, 0)::int AS count,
               coalesce(g.items, '[]'::jsonb) AS items
        FROM followed f
        LEFT JOIN grouped g ON g.follow_type=f.follow_type AND g.follow_id=f.follow_id
        ORDER BY f.display_order, f.follow_type, f.follow_name""",
        (user['user_id'], user['user_id']))

    groups = []
    flat_by_id = {}
    grouped_count = 0
    for row in rows:
        group_type = row.get('type')
        group_id = int(row.get('id'))
        group_name = row.get('name') or ''
        group_count = int(row.get('count') or 0)
        group_items = row.get('items') or []
        if isinstance(group_items, str):
            try:
                group_items = json.loads(group_items)
            except (TypeError, ValueError):
                group_items = []
        if not isinstance(group_items, list):
            group_items = []
        group = {'type': group_type, 'id': group_id, 'name': group_name,
                 'count': group_count, 'items': group_items[:5]}
        groups.append(group)
        grouped_count += group_count
        match = {'id': group_id, 'name': group_name}
        for item in group['items']:
            article_id = item.get('article_id')
            if article_id is None:
                continue
            merged = flat_by_id.setdefault(article_id, {**item, 'matched_topics': [], 'matched_entities': []})
            target = 'matched_topics' if group_type == 'topic' else 'matched_entities'
            if not any(int(x.get('id', 0)) == group_id for x in merged[target]):
                merged[target].append(match)
    flat_items = sorted(flat_by_id.values(),
                        key=lambda item: (str(item.get('timeline_at') or ''), int(item.get('article_id') or 0)),
                        reverse=True)
    return {'groups': groups, 'items': flat_items, 'count': grouped_count,
            'limit_per_group': 5}


def _public_direct_timeline_items(relation_table: str, relation_key: str, record_id: int,
                                  page: int, page_size: int):
    """Fetch articles linked to one exact topic_id or entity_id; no transitive grouping."""
    # relation_table/relation_key are internal constants supplied only by our route handlers.
    total_row = fetch_one(
        f"""SELECT count(DISTINCT a.article_id)::int AS total
            FROM {relation_table} rel
            JOIN news_articles a ON a.article_id=rel.article_id
            WHERE rel.{relation_key}=%s AND a.is_excluded=false""",
        (record_id,),
    )
    total = int((total_row or {}).get('total', 0))
    total_pages = max(1, (total + page_size - 1) // page_size)
    actual_page = min(page, total_pages)
    items = fetch_all(
        f"""WITH matched AS (
                SELECT DISTINCT rel.article_id
                FROM {relation_table} rel
                WHERE rel.{relation_key}=%s
            )
            SELECT a.article_id, a.title, a.url, a.image_url, a.author, a.language,
                   left(coalesce(nullif(a.summary,''),nullif(a.content_text,''),''),320) AS snippet,
                   a.published_at, a.collected_at,
                   coalesce(a.published_at,a.collected_at) AS timeline_at,
                   a.field_category,
                   s.name AS source_name,
                   coalesce((SELECT json_agg(json_build_object(
                                      'id', e.entity_id, 'name', e.canonical_name, 'type', e.entity_type,
                                      '_mention_text', ae.mention_text,
                                      '_aliases', coalesce((SELECT json_agg(ea.alias ORDER BY ea.alias)
                                                           FROM entity_aliases ea WHERE ea.entity_id=e.entity_id), '[]'::json)
                                    ) ORDER BY e.canonical_name)
                             FROM article_entities ae JOIN entities e ON e.entity_id=ae.entity_id
                             WHERE ae.article_id=a.article_id AND e.status='active'), '[]'::json) AS _entity_refs_raw,
                   coalesce(a.summary,'') AS _entity_grounding_summary,
                   coalesce((SELECT string_agg(DISTINCT t.name, ', ' ORDER BY t.name)
                             FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                             WHERE at.article_id=a.article_id AND t.status='active'), '') AS topics
            FROM matched m
            JOIN news_articles a ON a.article_id=m.article_id
            JOIN data_sources s ON s.source_id=a.source_id
            WHERE a.is_excluded=false
            ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC
            LIMIT %s OFFSET %s""",
        (record_id, page_size, (actual_page - 1) * page_size),
    )
    for item in items:
        refs = filter_grounded_entity_references(
            item.get('title') or '', item.pop('_entity_grounding_summary', ''), item.pop('_entity_refs_raw', []) or []
        )
        item['entities'] = ', '.join(ref.get('name') or '' for ref in refs if ref.get('name'))
    return items, pagination_meta(actual_page, page_size, total), total


@app.get('/api/public/news/articles/{article_id}/timeline')
def public_article_timeline(
    article_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """Legacy article URL: return only the article, never infer a mixed/transitive Timeline.

    UI links now open Timeline by explicit topic_id or entity_id. This endpoint remains for
    older clients during cache/deploy transitions but deliberately does not join other groups.
    """
    anchor = fetch_one("""SELECT a.article_id, a.title, a.image_url, a.published_at, a.collected_at,
                                coalesce(a.published_at,a.collected_at) AS timeline_at
                         FROM news_articles a
                         WHERE a.article_id=%s AND a.is_excluded=false""", (article_id,))
    if not anchor:
        raise HTTPException(status_code=404, detail='Article not found')
    items, pagination, total = _public_direct_timeline_items('news_articles', 'article_id', article_id, page, page_size)
    return {'anchor': anchor, 'timeline_id': article_id, 'timeline_name': anchor['title'],
            'mode': 'article_only', 'items': items, 'pagination': pagination}


@app.get('/api/public/news/topics/{topic_id}/timeline')
def public_topic_timeline(
    topic_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """Topic Timeline means exact topic_id membership only; entity links do not expand it."""
    topic = fetch_one("""SELECT topic_id, slug, name, description, topic_type
                         FROM topics WHERE topic_id=%s AND status='active'""", (topic_id,))
    if not topic:
        raise HTTPException(status_code=404, detail='Active Topic not found')
    items, pagination, total = _public_direct_timeline_items('article_topics', 'topic_id', topic_id, page, page_size)
    return {'topic': topic, 'timeline_id': topic_id, 'timeline_name': topic['name'],
            'mode': 'topic_id', 'items': items, 'pagination': pagination}


def _public_grounded_entity_timeline_items(entity: dict, page: int, page_size: int):
    """Return only linked articles where the entity is grounded in title/summary.

    This is a presentation-layer safety net while the ingestion quality pass repairs legacy
    article_entities rows. It deliberately refuses weak one-word mentions for multi-word names.
    """
    entity_id = int(entity['entity_id'])
    entity_name = str(entity.get('canonical_name') or '')
    entity_type = str(entity.get('entity_type') or 'UNKNOWN')
    rows = fetch_all(
        """SELECT rel.mention_text AS _entity_mention,
                  a.article_id, a.title, a.url, a.image_url, a.author, a.language,
                  left(coalesce(nullif(a.summary,''),nullif(a.content_text,''),''),320) AS snippet,
                  coalesce(a.summary,'') AS _grounding_summary,
                  a.published_at, a.collected_at,
                  coalesce(a.published_at,a.collected_at) AS timeline_at,
                  a.field_category, s.name AS source_name,
                  coalesce((SELECT json_agg(json_build_object(
                                     'id', e.entity_id, 'name', e.canonical_name, 'type', e.entity_type,
                                     '_mention_text', ae.mention_text,
                                     '_aliases', coalesce((SELECT json_agg(ea.alias ORDER BY ea.alias)
                                                          FROM entity_aliases ea WHERE ea.entity_id=e.entity_id), '[]'::json)
                                   ) ORDER BY e.canonical_name)
                            FROM article_entities ae JOIN entities e ON e.entity_id=ae.entity_id
                            WHERE ae.article_id=a.article_id AND e.status='active'), '[]'::json) AS _entity_refs_raw,
                  coalesce((SELECT string_agg(DISTINCT t.name, ', ' ORDER BY t.name)
                            FROM article_topics at JOIN topics t ON t.topic_id=at.topic_id
                            WHERE at.article_id=a.article_id AND t.status='active'), '') AS topics
           FROM article_entities rel
           JOIN news_articles a ON a.article_id=rel.article_id
           JOIN data_sources s ON s.source_id=a.source_id
           WHERE rel.entity_id=%s AND a.is_excluded=false
           ORDER BY coalesce(a.published_at,a.collected_at) DESC, a.article_id DESC""",
        (entity_id,),
    )
    # article_entities can contain multiple mention rows for the same article/entity.
    # Keep one grounded card per article, while allowing a later safe mention to validate
    # the article even if an earlier weak alias did not.
    grounded_by_article: dict[int, dict] = {}
    for row in rows:
        source_text = normalize_space(f"{row.get('title') or ''}. {row.get('_grounding_summary') or ''}")
        mention = str(row.get('_entity_mention') or '')
        if not is_grounded_entity(entity_name, [mention], source_text, entity_type):
            continue
        article_id = int(row['article_id'])
        if article_id in grounded_by_article:
            continue
        row.pop('_entity_mention', None)
        row['entities'] = ', '.join(
            ref.get('name') or '' for ref in filter_grounded_entity_references(
                row.get('title') or '', row.get('_grounding_summary') or '', row.pop('_entity_refs_raw', []) or []
            ) if ref.get('name')
        )
        row.pop('_grounding_summary', None)
        grounded_by_article[article_id] = row
    grounded = list(grounded_by_article.values())
    total = len(grounded)
    total_pages = max(1, (total + page_size - 1) // page_size)
    actual_page = min(page, total_pages)
    start = (actual_page - 1) * page_size
    return grounded[start:start + page_size], pagination_meta(actual_page, page_size, total), total


@app.get('/api/public/news/entities/{entity_id}/timeline')
def public_entity_timeline(
    entity_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """Entity Timeline means exact canonical entity_id membership only."""
    entity = fetch_one("""SELECT entity_id, canonical_name, entity_type, description
                          FROM entities WHERE entity_id=%s AND status='active'""", (entity_id,))
    if not entity:
        raise HTTPException(status_code=404, detail='Entity not found')
    items, pagination, total = _public_grounded_entity_timeline_items(entity, page, page_size)
    return {'entity': entity, 'timeline_id': entity_id, 'timeline_name': entity['canonical_name'],
            'mode': 'entity_id', 'items': items, 'pagination': pagination}


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
    field_category: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    """Return one page of articles plus total-count metadata, including editorial field."""
    where=[]; vals=[]
    chosen_field = (field_category or '').strip()
    allowed_fields = set(FIELD_CATEGORIES) | {'Chưa phân loại'}
    if chosen_field and chosen_field not in allowed_fields:
        raise HTTPException(status_code=400, detail='Lĩnh vực không hợp lệ')
    if chosen_field == 'Chưa phân loại':
        where.append("(a.field_category IS NULL OR a.field_category='Chưa phân loại')")
    elif chosen_field:
        where.append('a.field_category=%s'); vals.append(chosen_field)
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
    sql="""select a.article_id,a.title,a.url,a.canonical_url,a.summary,left(a.content_text,500) as content_preview,a.author,a.language,a.published_at,a.collected_at,a.quality_score,a.is_excluded,a.exclude_reason,s.name as source_name,coalesce(a.field_category,'Chưa phân loại') as field_category,
           coalesce((select string_agg(t.name,', ' order by t.name) from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id and t.topic_type='event' and t.status='active'),'') as topics,
           coalesce((select string_agg(e.canonical_name,', ' order by e.canonical_name) from article_entities ae join entities e on e.entity_id=ae.entity_id where ae.article_id=a.article_id and e.status='active'),'') as entities,
           coalesce((select json_agg(t.name order by t.name) from article_topics at join topics t on t.topic_id=at.topic_id where at.article_id=a.article_id and t.topic_type='event' and t.status='active'),'[]'::json) as topic_names,
           coalesce((select json_agg(e.canonical_name order by e.canonical_name) from article_entities ae join entities e on e.entity_id=ae.entity_id where ae.article_id=a.article_id and e.status='active'),'[]'::json) as entity_names""" + from_sql + where_sql + ' order by a.collected_at desc limit %s offset %s'
    items = fetch_all(sql, vals + [page_size, (page - 1) * page_size])
    return {'items': items, 'pagination': pagination_meta(page, page_size, total)}

@app.get('/api/articles/{article_id}/classifications', dependencies=[Depends(require_admin)])
def get_article_classifications(article_id: int):
    article = fetch_one("select article_id,title from news_articles where article_id=%s", (article_id,))
    if not article:
        raise HTTPException(status_code=404, detail='Article not found')
    topics = fetch_all("""select t.topic_id,t.name,t.topic_type from article_topics at
                         join topics t on t.topic_id=at.topic_id where at.article_id=%s
                           and t.topic_type='event' and t.status='active' order by t.name""", (article_id,))
    entities = fetch_all("""select distinct e.entity_id,e.canonical_name,e.entity_type from article_entities ae
                           join entities e on e.entity_id=ae.entity_id where ae.article_id=%s and e.status='active' order by e.canonical_name""", (article_id,))
    return {'article': article, 'topics': topics, 'entities': entities}

@app.patch('/api/articles/{article_id}/classifications', dependencies=[Depends(require_admin)])
def patch_article_classifications(article_id: int, body: ArticleClassificationBody):
    article = fetch_one("select article_id,title from news_articles where article_id=%s", (article_id,))
    if not article:
        raise HTTPException(status_code=404, detail='Article not found')
    topic_names = list(dict.fromkeys(str(x).strip()[:180] for x in body.topic_names if str(x).strip()))
    if any(is_broad_field_topic(name) for name in topic_names):
        raise HTTPException(status_code=400, detail='Lĩnh vực không phải Topic. Hãy dùng thuộc tính Lĩnh vực của bài báo.')
    entity_names = list(dict.fromkeys(str(x).strip()[:180] for x in body.entity_names if str(x).strip() and should_track_entity(str(x).strip()[:180], 'UNKNOWN')))
    topic_ids: list[int] = []
    entity_records: list[tuple[int, str]] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select topic_id,name,slug,event_key,topic_type from topics where status='active' and topic_type='event'")
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
        cur.execute("select entity_id,canonical_name,normalized_name,entity_type,status from entities where status='active'")
        all_entities = cur.fetchall()
        cur.execute("""select a.entity_id,a.alias,a.normalized_alias from entity_aliases a
                       join entities e on e.entity_id=a.entity_id where e.status='active'""")
        alias_rows = cur.fetchall()
        entity_lookup = build_entity_lookup(all_entities, alias_rows)
        for name in entity_names:
            normalized = norm_name(name)
            if not normalized:
                continue
            # If the operator included a role/title, resolve as a person (e.g. "President Trump").
            resolution_type = 'PERSON' if strip_person_title(name) != normalize_space(name).strip(' ,.;:-') else 'UNKNOWN'
            entity_id, ambiguous = resolve_entity_in_lookup(name, resolution_type, entity_lookup)
            if ambiguous:
                raise HTTPException(status_code=422, detail=f"Entity '{name}' trùng với nhiều đối tượng. Hãy chọn tên chuẩn hoặc bổ sung ngữ cảnh.")
            entity = entity_lookup['entities_by_id'].get(int(entity_id)) if entity_id else None
            canonical_name = strip_person_title(name) if resolution_type == 'PERSON' else name
            canonical_norm = entity_normalized_key(canonical_name, resolution_type)
            if not entity:
                cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,description,origin)
                               values(%s,%s,%s,'Entity được tạo/chỉnh trực tiếp từ phân loại bài báo.','manual')
                               on conflict(normalized_name,entity_type) do update set updated_at=now()
                               returning entity_id,canonical_name,normalized_name,entity_type,status""",
                            (canonical_name,canonical_norm,resolution_type))
                entity = cur.fetchone()
                entity_id = int(entity['entity_id'])
            variants = entity_name_variants(canonical_name, resolution_type)
            for alias in variants:
                alias_norm = norm_name(alias)
                if alias_norm:
                    cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                                   values(%s,%s,%s) on conflict do nothing""", (entity_id,alias,alias_norm))
            add_entity_to_lookup(entity_lookup, entity, variants)
            if not any(eid == int(entity_id) for eid, _ in entity_records):
                entity_records.append((int(entity_id), name))
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
    topic = fetch_one("select topic_id,name,description,topic_type,parent_topic_id from topics where topic_id=%s and status='active'", (topic_id,))
    if not topic:
        raise HTTPException(status_code=404, detail='Active Topic not found')
    rows = fetch_all("""select recent.article_id,recent.title,recent.url,recent.summary,recent.author,
                             recent.source_name,recent.published_at,recent.collected_at,recent.timeline_at,
                             coalesce((select string_agg(e.canonical_name,', ' order by e.canonical_name)
                                       from article_entities ae join entities e on e.entity_id=ae.entity_id
                                       where ae.article_id=recent.article_id and e.status='active'),'') as entities
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
    where=["t.topic_type='event'", "t.status='active'", "lower(btrim(t.name)) <> ALL(%s::text[])"]; vals=[[label.casefold() for label in LEGACY_BROAD_TOPIC_LABELS]]
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
    if body.topic_type != 'event': raise HTTPException(status_code=400,detail='Topic chỉ dùng cho sự kiện cụ thể; lĩnh vực được lưu riêng trên bài báo.')
    if is_broad_field_topic(body.name): raise HTTPException(status_code=400,detail='Tên này là lĩnh vực, không phải Topic sự kiện.')
    slug=slugify(body.name)
    row=fetch_one("""insert into topics(slug,name,description,keywords,topic_type,status,origin) values(%s,%s,%s,%s,%s,%s,'manual') returning *""",(slug,body.name.strip(),body.description, [x.strip() for x in body.keywords if x.strip()],body.topic_type,body.status))
    return row
@app.patch('/api/topics/{topic_id}',dependencies=[Depends(require_admin)])
def patch_topic(topic_id:int,patch:dict=Body(...)):
    allowed={'name','description','keywords','status','topic_type'}; fields={k:v for k,v in patch.items() if k in allowed}
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields')
    if 'status' in fields and fields['status'] not in {'active','archived'}: raise HTTPException(status_code=400,detail='Invalid status')
    if 'topic_type' in fields and fields['topic_type'] != 'event': raise HTTPException(status_code=400,detail='Topic chỉ dùng cho sự kiện cụ thể; lĩnh vực được lưu riêng trên bài báo.')
    if 'name' in fields and is_broad_field_topic(fields['name']): raise HTTPException(status_code=400,detail='Tên này là lĩnh vực, không phải Topic sự kiện.')
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
    candidate_type = candidate.get('candidate_type') or 'event'
    if candidate_type != 'event' or is_broad_field_topic(candidate.get('suggested_name')):
        fetch_one("update topic_candidates set status='rejected',last_seen_at=now() where candidate_id=%s returning candidate_id", (candidate_id,))
        return {'candidate_id': candidate_id, 'status': 'rejected', 'reason': 'Lĩnh vực không được tạo thành Topic sự kiện.'}
    with get_conn() as conn, conn.cursor() as cur:
        slug=slugify(candidate['suggested_name'])
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
    entity_type = str(body.entity_type or 'UNKNOWN').upper().strip()
    input_name = body.canonical_name.strip()
    canonical_name = strip_person_title(input_name) if entity_type == 'PERSON' else input_name
    if not should_track_entity(canonical_name, entity_type):
        raise HTTPException(status_code=422, detail='Entity phải là một đối tượng cụ thể. Không lưu quốc gia/tỉnh/thành phố hoặc danh từ chung; địa điểm cụ thể có tên riêng vẫn được phép.')
    variants = entity_name_variants(input_name, entity_type, body.aliases)
    with get_conn() as conn, conn.cursor() as cur:
        resolved, ambiguous = resolve_entity_in_database(conn, input_name, entity_type, body.aliases)
        if ambiguous:
            raise HTTPException(status_code=409, detail='Tên entity này đang là bí danh của nhiều đối tượng. Hãy dùng tên chuẩn đầy đủ để tránh hợp nhất nhầm.')
        if resolved:
            entity_id = int(resolved['entity_id'])
            current_norm = norm_name(str(resolved.get('canonical_name') or ''))
            canonical_norm = entity_normalized_key(canonical_name, entity_type)
            if entity_type == 'PERSON' and ' ' not in current_norm and ' ' in canonical_norm:
                cur.execute("""select 1 from entities where status='active' and normalized_name=%s
                               and entity_id<>%s and entity_type in ('PERSON','UNKNOWN') limit 1""",(canonical_norm,entity_id))
                if not cur.fetchone():
                    cur.execute("update entities set canonical_name=%s,normalized_name=%s,updated_at=now() where entity_id=%s",(canonical_name,canonical_norm,entity_id))
            cur.execute("update entities set external_id=coalesce(%s,external_id),description=case when %s='' then description else %s end,updated_at=now() where entity_id=%s returning *",
                        (body.external_id, body.description, body.description, entity_id))
            row = cur.fetchone()
        else:
            cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,external_id,description,origin)
                           values(%s,%s,%s,%s,%s,'manual') returning *""",
                        (canonical_name,entity_normalized_key(canonical_name,entity_type),entity_type,body.external_id,body.description))
            row = cur.fetchone()
            entity_id = int(row['entity_id'])
        all_aliases = list(variants)
        for alias in all_aliases:
            alias_norm = norm_name(alias)
            if alias_norm:
                cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                               values(%s,%s,%s) on conflict do nothing""", (entity_id,alias,alias_norm))
        cur.execute("select coalesce(array_agg(alias order by alias),'{}') as aliases from entity_aliases where entity_id=%s",(entity_id,))
        row['aliases'] = cur.fetchone()['aliases']
    return row

@app.patch('/api/entities/{entity_id}',dependencies=[Depends(require_admin)])
def patch_entity(entity_id:int,patch:dict=Body(...)):
    allowed={'canonical_name','entity_type','external_id','description','status'}
    fields={k:v for k,v in patch.items() if k in allowed}
    if not fields: raise HTTPException(status_code=400,detail='No allowed fields')
    current = fetch_one('select canonical_name,entity_type,status from entities where entity_id=%s',(entity_id,))
    if not current: raise HTTPException(status_code=404,detail='Entity not found')
    candidate_type = str(fields.get('entity_type', current['entity_type']) or 'UNKNOWN').upper().strip()
    candidate_name = fields.get('canonical_name', current['canonical_name'])
    if candidate_type == 'PERSON':
        candidate_name = strip_person_title(candidate_name)
    if 'canonical_name' in fields or 'entity_type' in fields:
        fields['canonical_name'] = candidate_name
        fields['normalized_name'] = entity_normalized_key(candidate_name, candidate_type)
    if 'entity_type' in fields:
        fields['entity_type'] = candidate_type
    candidate_status = fields.get('status', current['status'])
    if candidate_status == 'active' and not should_track_entity(candidate_name, candidate_type):
        raise HTTPException(status_code=422,detail='Không thể kích hoạt entity là quốc gia/tỉnh/thành phố hoặc danh từ chung.')
    sets=[]; vals=[]
    for k,v in fields.items(): sets.append(f'{k}=%s'); vals.append(v)
    sets.append('updated_at=now()'); vals.append(entity_id)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"update entities set {', '.join(sets)} where entity_id=%s returning *",vals)
            row = cur.fetchone()
            if not row: raise HTTPException(status_code=404,detail='Entity not found')
            if row['status'] == 'active':
                for alias in entity_name_variants(row['canonical_name'], row['entity_type']):
                    alias_norm = norm_name(alias)
                    if alias_norm:
                        cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                                       values(%s,%s,%s) on conflict do nothing""",(entity_id,alias,alias_norm))
                cur.execute("select coalesce(array_agg(alias order by alias),'{}') as aliases from entity_aliases where entity_id=%s",(entity_id,))
                row['aliases'] = cur.fetchone()['aliases']
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409,detail=f'Could not update entity: {str(exc)[:200]}')
    return row
@app.post('/api/entity-candidates/{candidate_id}/approve',dependencies=[Depends(require_admin)])
def approve_entity(candidate_id:int):
    candidate=fetch_one('select * from entity_candidates where candidate_id=%s',(candidate_id,))
    if not candidate: raise HTTPException(status_code=404,detail='Candidate not found')
    if candidate['status']!='pending': raise HTTPException(status_code=409,detail='Candidate is not pending')
    candidate_type = str(candidate['entity_type'] or 'UNKNOWN').upper().strip()
    candidate_name = strip_person_title(candidate['canonical_name']) if candidate_type == 'PERSON' else candidate['canonical_name']
    if not should_track_entity(candidate_name, candidate_type):
        raise HTTPException(status_code=422, detail='Candidate là địa danh hành chính hoặc danh từ chung, không thể duyệt thành entity theo dõi.')
    with get_conn() as conn, conn.cursor() as cur:
        resolved, ambiguous = resolve_entity_in_database(conn, candidate_name, candidate_type)
        if ambiguous:
            raise HTTPException(status_code=409, detail='Candidate trùng bí danh của nhiều entity. Hãy chuẩn hóa thủ công trước khi duyệt.')
        if resolved:
            entity_id = int(resolved['entity_id'])
            current_norm = norm_name(str(resolved.get('canonical_name') or ''))
            canonical_norm = entity_normalized_key(candidate_name, candidate_type)
            if candidate_type == 'PERSON' and ' ' not in current_norm and ' ' in canonical_norm:
                cur.execute("""select 1 from entities where status='active' and normalized_name=%s
                               and entity_id<>%s and entity_type in ('PERSON','UNKNOWN') limit 1""",(canonical_norm,entity_id))
                if not cur.fetchone():
                    cur.execute("update entities set canonical_name=%s,normalized_name=%s,updated_at=now() where entity_id=%s",(candidate_name,canonical_norm,entity_id))
        else:
            cur.execute("""insert into entities(canonical_name,normalized_name,entity_type,origin) values(%s,%s,%s,'discovered')
                           on conflict(normalized_name,entity_type) do update set canonical_name=excluded.canonical_name,updated_at=now()
                           returning entity_id""",(candidate_name,entity_normalized_key(candidate_name,candidate_type),candidate_type))
            entity_id=int(cur.fetchone()['entity_id'])
        for alias in entity_name_variants(candidate_name, candidate_type):
            alias_norm = norm_name(alias)
            if alias_norm:
                cur.execute("""insert into entity_aliases(entity_id,alias,normalized_alias)
                               values(%s,%s,%s) on conflict do nothing""",(entity_id,alias,alias_norm))
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
