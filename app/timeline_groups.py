"""Compatibility shim for old callers. Timeline membership is now ID-exact (v0.7.7)."""
from __future__ import annotations
from typing import Any

def refresh_article_timeline_group(conn, article_id: int) -> dict[str, Any]:
    """Return a singleton for legacy callers; never perform transitive entity/time unions."""
    article_id = int(article_id)
    with conn.cursor() as cur:
        cur.execute("SELECT article_id FROM news_articles WHERE article_id=%s AND is_excluded=false", (article_id,))
        anchor = cur.fetchone()
    if not anchor:
        return {'timeline_id': article_id, 'article_ids': [article_id], 'has_event': False, 'timeline_name': None}
    return {'timeline_id': article_id, 'article_ids': [article_id], 'has_event': False, 'timeline_name': None}
