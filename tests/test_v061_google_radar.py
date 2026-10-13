import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class GoogleRadarV061Tests(unittest.TestCase):
    def test_search_first_design_has_no_stats_and_uses_plain_lists(self):
        html = (ROOT / 'static' / 'news.html').read_text(encoding='utf-8')
        js = (ROOT / 'static' / 'news.js').read_text(encoding='utf-8')
        css = (ROOT / 'static' / 'news.css').read_text(encoding='utf-8')
        self.assertIn('id="radarSearchForm"', html)
        self.assertLess(html.index('id="searchHome"'), html.index('id="trending"'))
        self.assertLess(html.index('id="trending"'), html.index('id="articles"'))
        self.assertIn('id="userFeedContainer"', html)
        self.assertNotIn('id="stats-strip"', html)
        self.assertIn('class="radar-list"', html)
        self.assertIn('data-open-timeline="topic"', js)
        self.assertIn('data-open-timeline="entity"', js)
        self.assertIn('.radar-row{display:grid', css)
        self.assertIn('/api/user/feed?limit=30', js)

    def test_search_api_includes_topic_and_entity_matches(self):
        import sys, types
        from fastapi.testclient import TestClient
        fake_psycopg = types.ModuleType('psycopg'); fake_psycopg.__path__ = []; fake_psycopg.connect = lambda *a, **k: None
        fake_rows = types.ModuleType('psycopg.rows'); fake_rows.dict_row = object()
        with patch.dict(sys.modules, {'psycopg': fake_psycopg, 'psycopg.rows': fake_rows}):
            from app.main import app
        with patch('app.main.fetch_one', side_effect=[{'today': date(2026, 10, 13)}, {'total': 0}]), patch('app.main.fetch_all', return_value=[]) as articles_query:
            response = TestClient(app).get('/api/public/news/articles?q=Nvidia&page=1&page_size=20')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['query'], 'Nvidia')
        sql = articles_query.call_args.args[0]
        self.assertIn('search_t.name ILIKE %s', sql)
        self.assertIn('search_e.canonical_name ILIKE %s', sql)
        self.assertNotIn('at time zone %s)::date=%s', sql)

    def test_personal_feed_is_authenticated_and_matches_topics_entities(self):
        import sys, types
        from fastapi.testclient import TestClient
        fake_psycopg = types.ModuleType('psycopg'); fake_psycopg.__path__ = []; fake_psycopg.connect = lambda *a, **k: None
        fake_rows = types.ModuleType('psycopg.rows'); fake_rows.dict_row = object()
        with patch.dict(sys.modules, {'psycopg': fake_psycopg, 'psycopg.rows': fake_rows}):
            from app.main import app
            from app.security import require_user
        from datetime import datetime, timezone
        app.dependency_overrides[require_user] = lambda: {'user_id': 8, 'username': 'test-user'}
        fake_item = {
            'article_id': 7, 'title': 'Related story', 'url': 'https://example.com/news',
            'source_name': 'Example', 'snippet': 'Snippet', 'matched_topics': [{'id': 2, 'name': 'Topic A'}],
            'matched_entities': [{'id': 9, 'name': 'Entity B'}], 'total_count': 1,
            'published_at': datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc),
        }
        try:
            with patch('app.main.fetch_all', return_value=[dict(fake_item)]) as query:
                response = TestClient(app).get('/api/user/feed?limit=30')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['count'], 1)
            self.assertEqual(response.json()['items'][0]['matched_topics'][0]['id'], 2)
            self.assertEqual(response.json()['items'][0]['matched_entities'][0]['id'], 9)
            self.assertIn('jsonb_agg', query.call_args.args[0])
        finally:
            app.dependency_overrides.pop(require_user, None)

if __name__ == '__main__':
    unittest.main()
