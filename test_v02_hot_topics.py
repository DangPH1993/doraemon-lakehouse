import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class V02HotTopicsTests(unittest.TestCase):
    def test_migration_removes_vocemundi_from_active_sources(self):
        sql = (ROOT / "sql/migrations/002_vietnam_sources_and_topic_trending.sql").read_text(encoding="utf-8")
        self.assertIn("enabled = false", sql)
        self.assertIn("https://vocemundi.com/wire/feeds/en/", sql)
        self.assertNotIn("('Vocemundi Wire'", sql)

    def test_migration_preloads_vietnamese_sources(self):
        sql = (ROOT / "sql/migrations/002_vietnam_sources_and_topic_trending.sql").read_text(encoding="utf-8")
        for domain in ["vnexpress.net", "tuoitre.vn", "thanhnien.vn", "dantri.com.vn", "vtv.vn", "vietnamplus.vn", "baotintuc.vn", "qdnd.vn", "baovanhoa.vn", "infonet.vietnamnet.vn"]:
            self.assertIn(domain, sql)

    def test_new_fact_table_is_in_schema_and_api(self):
        schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
        migration = (ROOT / "sql/migrations/002_vietnam_sources_and_topic_trending.sql").read_text(encoding="utf-8")
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        index = (ROOT / "static/index.html").read_text(encoding="utf-8")
        pipeline = (ROOT / "app/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("create table if not exists fact_topic_trending", schema.lower())
        self.assertIn("create table if not exists fact_topic_trending", migration.lower())
        self.assertIn("'fact_topic_trending'", main)
        self.assertIn('value="fact_topic_trending"', index)
        self.assertIn("def refresh_topic_trending()", pipeline)

    def test_dashboard_has_day_week_month_containers(self):
        index = (ROOT / "static/index.html").read_text(encoding="utf-8")
        js = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for id_ in ["hotTopicsDay", "hotTopicsWeek", "hotTopicsMonth"]:
            self.assertIn(id_, index)
            self.assertIn(id_, js)

if __name__ == "__main__":
    unittest.main()
