"""Union topic-created event links and structured-entity links for News Radar v0.7.4.

A Timeline is the transitive component reached through either of these rules:
1. two articles share an active, specific EVENT entity (no time limit);
2. two articles share the same AI-created event topic (no time limit);
3. otherwise any shared canonical entity links articles when their publication times are
   within 48 hours. Links are transitive, so a chain can span longer than 48 hours overall.

Display topic names are labels only; they do not replace entity-based linking.
"""
from __future__ import annotations
from typing import Any


def refresh_article_timeline_group(conn, article_id: int) -> dict[str, Any]:
    """Recompute and persist the transitive timeline component containing ``article_id``."""
    article_id = int(article_id)
    with conn.cursor() as cur:
        cur.execute("SELECT article_id,timeline_id FROM news_articles WHERE article_id=%s AND is_excluded=false", (article_id,))
        anchor = cur.fetchone()
        if not anchor:
            return {'timeline_id': article_id, 'article_ids': [article_id], 'has_event': False, 'timeline_name': None}
        old_timeline_id = anchor.get('timeline_id')
        cur.execute("""WITH RECURSIVE
            entity_names AS (
                SELECT e.entity_id,e.canonical_name,e.normalized_name,e.entity_type,e.status,
                       lower(btrim(regexp_replace(
                         regexp_replace(e.canonical_name,'[^[:alnum:]_[:space:]-]',' ','g'),
                         '[[:space:]]+',' ','g'))) AS canonical_surface_norm
                FROM entities e
            ),
            entity_resolution_map AS (
                SELECT source_entity.entity_id AS source_entity_id,
                       CASE
                         WHEN upper(source_entity.entity_type)='PERSON'
                          AND source_entity.canonical_surface_norm='trump'
                          AND EXISTS (SELECT 1 FROM entity_names donald WHERE donald.status='active'
                                      AND upper(donald.entity_type)='PERSON' AND donald.canonical_surface_norm='donald trump')
                           THEN (SELECT donald.entity_id FROM entity_names donald WHERE donald.status='active'
                                 AND upper(donald.entity_type)='PERSON' AND donald.canonical_surface_norm='donald trump' LIMIT 1)
                         WHEN alias_match.target_count=1 THEN alias_match.target_id
                         ELSE source_entity.entity_id
                       END AS canonical_entity_id
                FROM entity_names source_entity
                LEFT JOIN LATERAL (
                    SELECT min(target.entity_id) AS target_id,
                           count(DISTINCT target.entity_id)::int AS target_count
                    FROM entity_aliases alias_row
                    JOIN entity_names target ON target.entity_id=alias_row.entity_id
                    WHERE alias_row.normalized_alias=source_entity.canonical_surface_norm
                      AND target.status='active'
                      AND target.entity_type=source_entity.entity_type
                      AND target.entity_id<>source_entity.entity_id
                      AND target.canonical_name ~ '[[:space:]]'
                ) alias_match ON true
                WHERE source_entity.status='active'
            ),
            canonical_article_entities AS (
                SELECT DISTINCT ae.article_id,
                       coalesce(erm.canonical_entity_id,ae.entity_id) AS entity_id
                FROM article_entities ae
                JOIN entity_names source_entity ON source_entity.entity_id=ae.entity_id AND source_entity.status='active'
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
                JOIN entity_names e ON e.entity_id=ae.entity_id AND e.status='active'
                WHERE upper(e.entity_type)='EVENT'
            ),
            seed_event_topics AS (
                SELECT DISTINCT t.topic_id,t.name
                FROM seed s
                JOIN article_topics at ON at.article_id=s.article_id
                JOIN topics t ON t.topic_id=at.topic_id AND t.status='active' AND t.topic_type='event'
                AND (t.origin='discovered' OR at.assignment_method='genai')
            ),
            related(article_id) AS (
                SELECT article_id FROM seed
                UNION
                SELECT neighbour.article_id
                FROM related r
                CROSS JOIN LATERAL (
                    -- Shared EVENT entity links at any publication distance.
                    SELECT next_article.article_id
                    FROM canonical_article_entities current_entity
                    JOIN entity_names e ON e.entity_id=current_entity.entity_id AND e.status='active'
                    JOIN canonical_article_entities next_entity ON next_entity.entity_id=current_entity.entity_id
                    JOIN news_articles current_article ON current_article.article_id=current_entity.article_id AND current_article.is_excluded=false
                    JOIN news_articles next_article ON next_article.article_id=next_entity.article_id AND next_article.is_excluded=false
                    WHERE current_entity.article_id=r.article_id
                      AND next_entity.article_id<>r.article_id
                      AND upper(e.entity_type)='EVENT'
                    UNION
                    -- Shared AI/event topic links at any publication distance.
                    SELECT next_article.article_id
                    FROM article_topics current_topic
                    JOIN topics t ON t.topic_id=current_topic.topic_id AND t.status='active' AND t.topic_type='event'
                    JOIN article_topics next_topic ON next_topic.topic_id=current_topic.topic_id
                    JOIN news_articles next_article ON next_article.article_id=next_topic.article_id AND next_article.is_excluded=false
                    WHERE current_topic.article_id=r.article_id AND next_topic.article_id<>r.article_id
                      AND (t.origin='discovered' OR current_topic.assignment_method='genai')
                      AND (t.origin='discovered' OR next_topic.assignment_method='genai')
                    UNION
                    -- Shared non-event entity, using the strict adjacent 48-hour rule.
                    SELECT next_article.article_id
                    FROM canonical_article_entities current_entity
                    JOIN entity_names e ON e.entity_id=current_entity.entity_id AND e.status='active'
                    JOIN canonical_article_entities next_entity ON next_entity.entity_id=current_entity.entity_id
                    JOIN news_articles current_article ON current_article.article_id=current_entity.article_id AND current_article.is_excluded=false
                    JOIN news_articles next_article ON next_article.article_id=next_entity.article_id AND next_article.is_excluded=false
                    WHERE current_entity.article_id=r.article_id
                      AND next_entity.article_id<>r.article_id
                      AND upper(e.entity_type)<>'EVENT'
                      AND abs(extract(epoch FROM (
                          coalesce(current_article.published_at,current_article.collected_at) -
                          coalesce(next_article.published_at,next_article.collected_at)
                      ))) <= 172800
                ) neighbour
            ),
            result_ids AS (
                SELECT DISTINCT r.article_id FROM related r
                JOIN news_articles a ON a.article_id=r.article_id AND a.is_excluded=false
            ),
            metadata AS (
                SELECT EXISTS(SELECT 1 FROM seed_event_entities)
                       OR EXISTS(SELECT 1 FROM seed_event_topics) AS has_event
            )
            SELECT ARRAY(SELECT article_id FROM result_ids ORDER BY article_id) AS article_ids,
                   coalesce((SELECT has_event FROM metadata),false) AS has_event,
                   coalesce((SELECT min(article_id) FROM result_ids),%s)::bigint AS timeline_id,
                   coalesce(
                     (SELECT e.canonical_name FROM seed_event_entities se
                      JOIN entity_names e ON e.entity_id=se.entity_id ORDER BY e.canonical_name LIMIT 1),
                     (SELECT setn.name FROM seed_event_topics setn ORDER BY setn.name LIMIT 1)
                   ) AS timeline_name
            """, (article_id, article_id))
        group = cur.fetchone() or {}
        article_ids = [int(x) for x in (group.get('article_ids') or [article_id])]
        timeline_id = int(group.get('timeline_id') or min(article_ids))
        if old_timeline_id is not None:
            # If an old component has split due to corrected entity IDs, detach members not
            # reached from this anchor. Other components are recomputed when opened.
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
