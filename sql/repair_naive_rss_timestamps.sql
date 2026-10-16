-- v0.6.6 helper: inspect legacy RSS timestamps that may have been stored 7 hours late.
-- READ-ONLY by default. Review source timestamp and article_id before any UPDATE.
-- This candidate query only recognizes the date format seen in the Tuổi Trẻ RSS:
-- "10/13/2026 12:00:00 PM" (no timezone suffix).

SELECT
    a.article_id,
    s.name AS source_name,
    a.title,
    r.raw_payload ->> 'published' AS raw_rss_published,
    a.published_at AS stored_published_at_utc,
    a.published_at AT TIME ZONE 'Asia/Ho_Chi_Minh' AS currently_shown_vietnam,
    (a.published_at - INTERVAL '7 hours') AS proposed_published_at_utc,
    (a.published_at - INTERVAL '7 hours') AT TIME ZONE 'Asia/Ho_Chi_Minh'
        AS proposed_shown_vietnam,
    a.collected_at AT TIME ZONE 'Asia/Ho_Chi_Minh' AS collected_at_vietnam,
    ROUND(EXTRACT(EPOCH FROM (a.published_at - a.collected_at)) / 3600.0, 2)
        AS published_minus_collected_hours
FROM news_articles a
JOIN data_sources s ON s.source_id = a.source_id
JOIN raw_feed_items r ON r.raw_id = a.raw_id
WHERE a.published_at IS NOT NULL
  AND COALESCE(r.raw_payload ->> 'published', '') ~* '^\s*[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}\s+[0-9]{1,2}:[0-9]{2}:[0-9]{2}\s+(AM|PM)\s*$'
ORDER BY a.collected_at DESC;

-- Secondary diagnostic: articles that appear to be published after ingestion.
-- This does NOT prove a timezone error; inspect RSS raw values / publisher pages.
SELECT
    a.article_id,
    s.name AS source_name,
    a.title,
    a.published_at AT TIME ZONE 'Asia/Ho_Chi_Minh' AS published_at_vietnam,
    a.collected_at AT TIME ZONE 'Asia/Ho_Chi_Minh' AS collected_at_vietnam,
    ROUND(EXTRACT(EPOCH FROM (a.published_at - a.collected_at)) / 3600.0, 2)
        AS hours_published_after_collection
FROM news_articles a
JOIN data_sources s ON s.source_id = a.source_id
WHERE a.is_excluded = FALSE
  AND a.published_at > a.collected_at + INTERVAL '15 minutes'
ORDER BY a.published_at - a.collected_at DESC
LIMIT 200;

-- DO NOT blindly run an automatic -7 hour UPDATE.
-- First inspect the SELECT results and identify only confirmed legacy rows.
-- After confirmation, correct explicit article IDs in a separate statement, for example:
-- UPDATE news_articles
-- SET published_at = published_at - INTERVAL '7 hours'
-- WHERE article_id IN (123, 456)
--   AND published_at > collected_at;
