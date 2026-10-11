# Update v0.1.1 -> v0.2.0

## Run order for an existing Supabase database

1. Back up/export important data first.
2. In Supabase SQL Editor, run `sql/migrations/002_vietnam_sources_and_topic_trending.sql` once. Do **not** rerun the full `sql/schema.sql` against an already deployed database.
3. Upload/commit the v0.2.0 project files to the existing GitHub repository. Render will deploy the web code from that repository (if Auto-Deploy is enabled).
4. In GitHub Actions, run `Mini Lakehouse Daily Pipeline` manually. This retags RSS articles using the Vietnamese keyword taxonomy and refreshes `fact_topic_trending`.
5. Open the admin portal, refresh Overview. The three Topic Hotness lists are based on facts created by the latest pipeline run.

## Files changed

- `sql/schema.sql`: initial-install schema now seeds Vietnamese RSS sources and Vietnamese + English topic keywords, and adds `fact_topic_trending`.
- `sql/migrations/002_vietnam_sources_and_topic_trending.sql`: idempotent migration for existing databases; deactivates Vocemundi Wire without deleting its historical articles, upserts source definitions, merges Vietnamese keywords into existing topics, creates the topic-hotness fact and catalog entry.
- `app/pipeline.py`: recomputes topic rankings by calendar day/week/month in `TIMEZONE`, with article count, source coverage, previous-period count, growth and rank. The new topic discovery tokenizer supports Vietnamese diacritics.
- `app/main.py`: includes hot-topic rankings in the dashboard response and makes the new FACT table queryable from the FACT page.
- `static/index.html`, `static/app.js`, `static/style.css`: three dashboard panels (today/current week/current month) and FACT selector/grain description.
- tests: SQL placeholder count and migration/dashboard coverage checks.

## Sources

Seven direct RSS endpoints are enabled by default after seeding: VnExpress, Tuổi Trẻ, Thanh Niên, Dân trí, VTV, VietnamPlus, and Infonet. Báo Tin tức & Dân tộc, Báo Quân đội nhân dân, and Báo Văn hóa are preconfigured but disabled because the direct feed URLs need validation. Use **Data Sources -> Test RSS** and only enable a source when it returns entries. Old Vocemundi rows are marked disabled/legacy; article history is retained.

## Ranking definition

`hotness_score = article_count + 2 * ln(1 + source_count) + positive_growth_bonus`, with the growth bonus capped at 2 points. The dashboard ranks by score, then article count, then source count. This is a trend signal—not an assessment of truth, public importance, or quality.
