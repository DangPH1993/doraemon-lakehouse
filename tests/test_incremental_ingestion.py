import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = (ROOT / 'app' / 'pipeline.py').read_text(encoding='utf-8')
TREE = ast.parse(PIPELINE)

class IncrementalIngestionTests(unittest.TestCase):
    def test_conditional_header_helper_exists(self):
        names = {node.name for node in TREE.body if isinstance(node, ast.FunctionDef)}
        self.assertIn('conditional_feed_headers', names)
        self.assertIn('If-None-Match', PIPELINE)
        self.assertIn('If-Modified-Since', PIPELINE)

    def test_existing_articles_are_prefetched_in_one_query_per_source(self):
        fn = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == 'process_source')
        source = ast.get_source_segment(PIPELINE, fn)
        self.assertIn('select canonical_url from news_articles where source_id=%s and canonical_url = any(%s)', source)
        self.assertNotIn('select article_id from news_articles where source_id=%s and canonical_url=%s limit 1', source)
        self.assertIn("result['skipped_existing'] += 1", source)

    def test_rolling_24_hour_window_is_used_before_processing(self):
        self.assertIn('PIPELINE_LOOKBACK_HOURS', (ROOT / 'app' / 'config.py').read_text(encoding='utf-8'))
        self.assertIn('timedelta(hours=max(1, settings.pipeline_lookback_hours))', PIPELINE)
        self.assertIn("result['skipped_outside_window'] += 1", PIPELINE)

    def test_pipeline_metrics_include_skips_and_unchanged_feeds(self):
        self.assertIn('entries_skipped_existing', PIPELINE)
        self.assertIn('entries_skipped_outside_window', PIPELINE)
        self.assertIn('feeds_not_modified', PIPELINE)


    def test_entity_and_topic_catalogs_are_cached_per_run(self):
        self.assertIn('def load_entity_lookup(conn)', PIPELINE)
        self.assertIn('def load_active_topics(conn)', PIPELINE)
        self.assertIn('process_source(source,rules,entity_lookup=entity_lookup,active_topics=active_topics)', PIPELINE)
        self.assertIn('entity_lookup = load_entity_lookup(conn)', PIPELINE)

    def test_event_backfill_is_batched(self):
        config = (ROOT / 'app' / 'config.py').read_text(encoding='utf-8')
        workflow = (ROOT / '.github/workflows/daily-pipeline.yml').read_text(encoding='utf-8')
        self.assertIn('EVENT_TOPIC_BACKFILL_BATCH_SIZE', config)
        self.assertIn('EVENT_TOPIC_BACKFILL_BATCH_SIZE: "60"', workflow)
        self.assertIn('limit = max(1, settings.event_topic_backfill_batch_size)', PIPELINE)

    def test_workflow_has_progress_timeout_and_node24_actions(self):
        workflow = (ROOT / '.github/workflows/daily-pipeline.yml').read_text(encoding='utf-8')
        self.assertIn('timeout-minutes: 35', workflow)
        self.assertIn('actions/checkout@v7', workflow)
        self.assertIn('actions/setup-python@v7', workflow)
        self.assertIn('processing prepared entry', PIPELINE)

    def test_workflow_runs_twice_daily(self):
        workflow = (ROOT / '.github/workflows/daily-pipeline.yml').read_text(encoding='utf-8')
        self.assertIn('cron: "10 1,13 * * *"', workflow)
        self.assertIn('PIPELINE_LOOKBACK_HOURS: "24"', workflow)

if __name__ == '__main__':
    unittest.main()
