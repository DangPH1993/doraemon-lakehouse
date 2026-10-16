"""Publisher-hosted article image URL discovery. Image files are never mirrored locally."""
from __future__ import annotations

import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger('mini-lakehouse.article-images')
IMAGE_USER_AGENT = 'DoraemonMiniLakehouse/1.0 (+RSS image metadata; contact admin of the configured source)'

_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif', '.bmp', '.tif', '.tiff')
_IMAGE_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_IMAGE_OG_FALLBACK_LIMIT_PER_SOURCE = 40
_IMAGE_BACKFILL_LIMIT_PER_RUN = 20


def _safe_external_http_url(value, base_url: str | None = None) -> str | None:
    """Normalize externally hosted HTTP(S) URLs and reject obvious local targets."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().replace('\\/', '/')
    if not candidate or len(candidate) > 2048 or any(ord(ch) < 32 for ch in candidate):
        return None
    if base_url:
        candidate = urljoin(base_url, candidate)
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        hostname = parsed.hostname.rstrip('.').lower()
        if hostname in {'localhost', 'metadata.google.internal'} or hostname.endswith(('.localhost', '.local', '.internal', '.test', '.lan')):
            return None
        try:
            ip = ipaddress.ip_address(hostname)
            if not ip.is_global:
                return None
        except ValueError:
            # RSS publishers are configured by the operator; reject obvious local hostnames
            # while avoiding a DNS lookup for each URL during a scheduled ingestion run.
            if '.' not in hostname:
                return None
        return parsed.geturl()
    except (TypeError, ValueError):
        return None


def _rss_image_candidate(item, field_name: str, base_url: str | None) -> str | None:
    if isinstance(item, str):
        candidate = item
        media_type = ''
        medium = ''
        rel = ''
    elif isinstance(item, dict):
        candidate = item.get('url') or item.get('href') or item.get('src') or item.get('value')
        media_type = str(item.get('type') or item.get('mime_type') or item.get('media_type') or '').lower()
        medium = str(item.get('medium') or '').lower()
        rel = str(item.get('rel') or '').lower()
    else:
        return None
    if not candidate:
        return None
    try:
        parsed_candidate = urlsplit(urljoin(base_url or '', str(candidate)))
    except (TypeError, ValueError):
        return None
    path = parsed_candidate.path.lower()
    is_image_mime = media_type.startswith('image/') or medium == 'image'
    is_image_extension = path.endswith(_IMAGE_EXTENSIONS)
    has_non_image_type = bool(media_type) and not media_type.startswith('image/')
    is_known_media_field = field_name in {'media_content', 'media_thumbnail', 'image'} and not has_non_image_type and medium not in {'video', 'audio'}
    is_image_enclosure = field_name in {'enclosures', 'links', 'enclosure'} and (is_image_mime or is_image_extension)
    if not (is_known_media_field or is_image_mime or is_image_extension or is_image_enclosure):
        return None
    return _safe_external_http_url(str(candidate), base_url)


def extract_image_url(entry: dict, base_url: str | None = None) -> str | None:
    """Extract the publisher's original image URL from common RSS/Atom image fields."""
    for field_name in ('media_content', 'media_thumbnail', 'image', 'enclosures', 'enclosure', 'links'):
        values = entry.get(field_name)
        if not values:
            continue
        if isinstance(values, dict) or isinstance(values, str):
            values = [values]
        try:
            iterator = list(values)
        except TypeError:
            iterator = [values]
        for value in iterator:
            candidate = _rss_image_candidate(value, field_name, base_url)
            if candidate:
                return candidate

    # Some publishers only include an inline image in the RSS description/content HTML.
    html_parts = []
    for content in (entry.get('content') or []):
        if isinstance(content, dict) and content.get('value'):
            html_parts.append(str(content['value']))
    for field_name in ('summary', 'description', 'encoded'):
        if entry.get(field_name):
            html_parts.append(str(entry[field_name]))
    for html in html_parts:
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup.find_all('img'):
                src = tag.get('src') or tag.get('data-src') or tag.get('data-original')
                candidate = _safe_external_http_url(str(src or ''), base_url)
                if candidate:
                    return candidate
        except Exception:
            continue
    return None


def fetch_open_graph_image(article_url: str) -> str | None:
    """Read og:image/twitter:image metadata only; never downloads the image file itself."""
    current_url = _safe_external_http_url(article_url)
    if not current_url:
        return None
    headers = {'User-Agent': IMAGE_USER_AGENT, 'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.1'}
    for _ in range(4):
        response = None
        try:
            response = requests.get(current_url, timeout=(2, 4), headers=headers,
                                   stream=True, allow_redirects=False)
            if response.status_code in _IMAGE_REDIRECT_STATUSES:
                location = response.headers.get('Location')
                response.close()
                if not location:
                    return None
                current_url = _safe_external_http_url(location, current_url)
                if not current_url:
                    return None
                continue
            if response.status_code != 200:
                return None
            content_type = str(response.headers.get('Content-Type') or '').lower()
            if content_type and 'html' not in content_type and 'xhtml' not in content_type:
                return None
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                remaining = 1_000_000 - total
                chunks.append(chunk[:remaining])
                total += min(len(chunk), remaining)
                if total >= 1_000_000 or len(chunk) > remaining:
                    break
            soup = BeautifulSoup(b''.join(chunks), 'html.parser')
            for selector in (
                {'property': 'og:image'}, {'property': 'og:image:secure_url'},
                {'name': 'twitter:image'}, {'name': 'twitter:image:src'},
            ):
                tag = soup.find('meta', attrs=selector)
                candidate_value = tag.get('content') if tag else None
                candidate = _safe_external_http_url(str(candidate_value or ''), current_url)
                if candidate:
                    return candidate
            return None
        except Exception as exc:
            log.debug('Open Graph image lookup failed for %s: %s', current_url, type(exc).__name__)
            return None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
    return None


def _lookup_open_graph_images(items: list[dict], limit: int) -> tuple[int, int]:
    """Populate image_url for a bounded set of items lacking an RSS image."""
    candidates = [item for item in items if not item.get('image_url')][:max(0, limit)]
    if not candidates:
        return 0, 0
    found = 0
    with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as executor:
        futures = {executor.submit(fetch_open_graph_image, item['raw_url']): item for item in candidates}
        for future in as_completed(futures):
            item = futures[future]
            item['image_lookup_attempted'] = True
            try:
                url = future.result()
            except Exception:
                url = None
            if url:
                item['image_url'] = url
                item['image_source'] = 'og'
                found += 1
    return len(candidates), found

