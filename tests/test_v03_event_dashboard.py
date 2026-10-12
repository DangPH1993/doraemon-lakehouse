import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class V03EventDashboardTests(unittest.TestCase):
    def test_schema_has_event_topic_columns(self):
        schema = (ROOT / 'sql/schema.sql').read_text(encoding='utf-8')
        self.assertIn('topic_type text not null default', schema)
        self.assertIn('event_key text unique', schema)
        self.assertIn('event_topic_scanned_at timestamptz', schema)
        self.assertIn("candidate_type text not null default 'event'", schema)

    def test_dashboard_only_ranks_event_topics(self):
        main = (ROOT / 'app/main.py').read_text(encoding='utf-8')
        index = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        self.assertGreaterEqual(main.count("t.topic_type='event'"), 2)
        self.assertIn('Sự kiện nổi bật', index)
        self.assertIn('Mỹ tấn công Iran', index)

    def test_event_topic_logic_is_deterministic(self):
        pipeline = (ROOT / 'app/pipeline.py').read_text(encoding='utf-8')
        self.assertIn('def assign_event_topic(', pipeline)
        self.assertIn('def backfill_unscanned_event_topics(', pipeline)
        self.assertIn("assignment_method)\n                       values(%s,%s,%s,'event_rule')", pipeline)

if __name__ == '__main__':
    unittest.main()
