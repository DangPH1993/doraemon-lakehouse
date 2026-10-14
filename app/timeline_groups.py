"""Canonical entity-based timeline grouping for News Radar v0.7.0.

An EVENT entity takes precedence: articles sharing that event entity belong to the same
Timeline regardless of publication date. Otherwise, articles sharing a canonical entity
within 48 hours are linked transitively. Display topic labels do not determine membership.
"""
from __future__ import annotations

from typing import Any


def refresh_article_timeline_group(conn, article_id: int) -> dict[str, Any]:
    """Recompute and persist the transitive timeline group containing ``article_id``.

    The caller supplies an open psycopg connection. Existing rows outside a component that
    has split are reset to their own ID; every row in the current component receives the same
    canonical timeline_id (the smallest article_id in that component).
    """
    article_id = int(article_id)
    with conn.cursor() as cur:
        cur.execute("SELECT article_id,timeline_id FROM news_articles WHERE article_id=%s AND is_excluded=false", (article_id,))
        anchor = cur.fetchone()
        if not anchor:
            return {'timeline_id': article_id, 'article_ids': [article_id], 'has_event': False, 'timeline_name': None}
        old_timeline_id = anchor.get('timeline_id')
        cur.execute("""WITH RECURSIVE
            entity_resolution_map AS (
                SELECT source_entity.entity_id AS source_entity_id,
                       CASE
                         WHEN source_entity.entity_type='PERSON'
                          AND source_entity.normalized_name='trump'
                          AND EXISTS (SELECT 1 FROM entities donald WHERE donald.status='active'
                                      AND donald.entity_type='PERSON' AND donald.normalized_name='donald trump')
                           THEN (SELECT donald.entity_id FROM entities donald WHERE donald.status='active'
                                 AND donald.entity_type='PERSON' AND donald.normalized_name='donald trump' LIMIT 1)
                         WHEN alias_match.target_count=1 THEN alias_match.target_id
                         ELSE source_entity.entity_id
                       END AS canonical_entity_id
                FROM entities source_entity
                LEFT JOIN LATERAL (
                    SELECT min(target.entity_id) AS target_id,
                           count(DISTINCT target.entity_id)::int AS target_count
                    FROM entity_aliases alias_row
                    JOIN entities target ON target.entity_id=alias_row.entity_id
                    WHERE alias_row.normalized_alias=source_entity.normalized_name
                      AND target.status='active'
                      AND target.entity_type=source_entity.entity_type
                      AND target.entity_id<>source_entity.entity_id
                      AND target.normalized_name LIKE '% %'
                ) alias_match ON true
                WHERE source_entity.status='active'
            ),
            canonical_article_entities AS (
                SELECT DISTINCT ae.article_id,coalesce(erm.canonical_entity_id,ae.entity_id) AS entity_id
                FROM article_entities ae
                LEFT JOIN entity_resolution_map erm ON erm.source_entity_id=ae.entity_id
            ),
            seed AS (
                SELECT a.article_id FROM news_articles a
                WHERE a.article_id=%s AND a.is_excluded=false
            ),
            seed_event_entities AS (
                SELECT DISTINCT ae.entity_id
                FROM seed s
                JOIN canonical_article_entities ae ON ae.article_id=s.article_id
                JOIN entities e ON e.entity_id=ae.entity_id AND e.status='active'
                WHERE upper(e.entity_type)='EVENT'
            ),
            mode AS (SELECT EXISTS(SELECT 1 FROM seed_event_entities) AS has_event),
            related(article_id) AS (
                SELECT article_id FROM seed
                UNION
                SELECT next_article.article_id
                FROM related r
                JOIN mode m ON NOT m.has_event
                JOIN canonical_article_entities e1 ON e1.article_id=r.article_id
                JOIN canonical_article_entities e2 ON e2.entity_id=e1.entity_id
                JOIN news_articles current_article ON current_article.article_id=r.article_id AND current_article.is_excluded=false
                JOIN news_articles next_article ON next_article.article_id=e2.article_id AND next_article.is_excluded=false
                WHERE abs(extract(epoch FROM (
                    coalesce(current_article.published_at,current_article.collected_at) -
                    coalesce(next_article.published_at,next_article.collected_at)
                ))) <= 172800
            ),
            timeline_ids AS (
                SELECT DISTINCT ae.article_id
                FROM seed_event_entities se
                JOIN canonical_article_entities ae ON ae.entity_id=se.entity_id
                JOIN news_articles a ON a.article_id=ae.article_id AND a.is_excluded=false
                JOIN mode m ON m.has_event
                UNION
                SELECT r.article_id
                FROM related r
                JOIN news_articles a ON a.article_id=r.article_id AND a.is_excluded=false
                JOIN mode m ON NOT m.has_event
            )
            SELECT ARRAY(SELECT DISTINCT article_id FROM timeline_ids ORDER BY article_id) AS article_ids,
                   coalesce((SELECT has_event FROM mode),false) AS has_event,
                   coalesce((SELECT min(article_id) FROM timeline_ids),%s)::bigint AS timeline_id,
                   (SELECT e.canonical_name FROM seed_event_entities se
                    JOIN entities e ON e.entity_id=se.entity_id
                    ORDER BY e.canonical_name LIMIT 1) AS timeline_name
            """, (article_id, article_id))
        group = cur.fetchone() or {}
        article_ids = [int(x) for x in (group.get('article_ids') or [article_id])]
        timeline_id = int(group.get('timeline_id') or min(article_ids))
        if old_timeline_id is not None:
            # If entity corrections split an old group, detach only old members outside the
            # current connected component. They will be recomputed lazily when opened.
            cur.execute("""UPDATE news_articles SET timeline_id=article_id
                           WHERE timeline_id=%s AND article_id <> ALL(%s::bigint[])""",
                        (old_timeline_id, article_ids))
        cur.execute("UPDATE news_articles SET timeline_id=%s WHERE article_id=ANY(%s::bigint[])",
                    (timeline_id, article_ids))
        return {
            'timeline_id': timeline_id,
            'article_ids': article_ids,
            'has_event': bool(group.get('has_event')),
            'timeline_name': group.get('timeline_name'),
        }
