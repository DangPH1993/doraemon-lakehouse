import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class PublicNewsPortalTests(unittest.TestCase):
    def test_public_page_and_assets_exist(self):
        self.assertTrue((ROOT / 'static' / 'news.html').exists())
        self.assertTrue((ROOT / 'static' / 'news.css').exists())
        self.assertTrue((ROOT / 'static' / 'news.js').exists())
        html = (ROOT / 'static' / 'news.html').read_text(encoding='utf-8')
        self.assertIn('id="radarSearchForm"', html)
        self.assertIn('id="trending"', html)
        self.assertIn('id="timeline"', html)
        self.assertIn('id="articles"', html)
        self.assertNotIn('id="stats-strip"', html)
        self.assertIn('/static/news.js', html)

    def test_public_api_routes_are_read_only_and_exist(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("@app.get('/news')", main)
        self.assertIn("@app.get('/api/public/news/overview')", main)
        self.assertIn("@app.get('/api/public/news/hot-topics')", main)
        self.assertIn("@app.get('/api/public/news/articles')", main)
        self.assertIn("@app.get('/api/public/news/topics/{topic_id}/timeline')", main)
        self.assertNotIn("Depends(require_admin)])\ndef public_", main)

    def test_public_queries_exclude_article_full_text(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        start = main.index("@app.get('/api/public/news/articles')")
        end = main.index("@app.get('/api/public/news/topics/{topic_id}/timeline')", start)
        block = main[start:end]
        self.assertIn("left(coalesce(nullif(a.summary,''), nullif(a.content_text,''), ''), 320) AS snippet", block)
        self.assertNotIn('a.content_text, a.summary', block)
        self.assertIn('pagination_meta', block)

    def test_public_timeline_only_returns_active_event_topic(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        start = main.index("@app.get('/api/public/news/topics/{topic_id}/timeline')")
        end = main.index("@app.get('/api/sources'", start)
        block = main[start:end]
        self.assertIn("status='active'", block)
        self.assertIn("topic_type", block)
        self.assertIn("a.is_excluded=false", block)
        self.assertIn('pagination_meta', block)

    def test_assets_escape_dynamic_content_and_link_to_original(self):
        js = (ROOT / 'static' / 'news.js').read_text(encoding='utf-8')
        self.assertIn("replace(/[&<>\"']/g", js)
        self.assertIn('target="_blank" rel="noopener noreferrer"', js)
        self.assertIn('/api/public/news/hot-topics', js)
        self.assertIn('/api/public/news/articles', js)
        self.assertIn('/api/public/news/topics/${encodeURIComponent(id)}/timeline', js)
        self.assertIn('/api/public/news/entities/${encodeURIComponent(id)}/timeline', js)
        self.assertIn('/api/user/feed?limit=30', js)


    def test_public_routes_work_with_mocked_database(self):
        from datetime import date, datetime, timezone
        from unittest.mock import patch
        import sys, types
        from fastapi.testclient import TestClient
        fake_psycopg = types.ModuleType('psycopg')
        fake_psycopg.__path__ = []
        fake_psycopg.connect = lambda *args, **kwargs: None
        fake_rows = types.ModuleType('psycopg.rows')
        fake_rows.dict_row = object()
        with patch.dict(sys.modules, {'psycopg': fake_psycopg, 'psycopg.rows': fake_rows}):
            from app.main import app
        client = TestClient(app)
        with patch('app.main.fetch_one', return_value={
            'articles_today': 12, 'enabled_sources': 7,
            'active_event_topics': 33, 'ranked_events_today': 5,
        }):
            response = client.get('/api/public/news/overview')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['articles_today'], 12)
        with patch('app.main.fetch_one', return_value={'today': date(2026, 10, 12)}), patch(
            'app.main.fetch_all', return_value=[{
                'topic_id': 7, 'topic_name': 'Mỹ đàm phán với Iran',
                'article_count': 8, 'source_count': 3, 'growth_pct': 42.5,
                'hotness_score': 13.3, 'rank_position': 1,
                'period_start': date(2026, 10, 12), 'period_type': 'day',
            }]
        ):
            response = client.get('/api/public/news/hot-topics?period=day')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['items'][0]['topic_name'], 'Mỹ đàm phán với Iran')
        with patch('app.main.fetch_one', side_effect=[{'today': date(2026, 10, 12)}, {'total': 1}]), patch(
            'app.main.fetch_all', return_value=[{
                'article_id': 22, 'title': 'Bài tin thử', 'url': 'https://example.com/news',
                'snippet': 'Đoạn trích', 'source_name': 'Nguồn thử', 'topics': ['Thế giới'],
                'published_at': datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc),
                'collected_at': datetime(2026, 10, 12, 8, 2, tzinfo=timezone.utc),
                'timeline_at': datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc),
            }]
        ):
            response = client.get('/api/public/news/articles?page=1&page_size=20')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['pagination']['total'], 1)
            self.assertEqual(response.json()['items'][0]['title'], 'Bài tin thử')
        with patch('app.main.fetch_one', side_effect=[
            {'topic_id': 7, 'slug': 'iran-talks', 'name': 'Mỹ đàm phán với Iran', 'description': '', 'topic_type': 'event'},
            {'total': 1},
        ]), patch('app.main.fetch_all', return_value=[{
            'article_id': 22, 'title': 'Bài tin thử', 'url': 'https://example.com/news',
            'snippet': 'Đoạn trích', 'source_name': 'Nguồn thử',
            'timeline_at': datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc),
        }]):
            response = client.get('/api/public/news/topics/7/timeline?page=1&page_size=20')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['topic']['name'], 'Mỹ đàm phán với Iran')
            self.assertEqual(response.json()['pagination']['total'], 1)
        response = client.get('/news')
        self.assertEqual(response.status_code, 200)
        self.assertIn('NEWS RADAR', response.text)

    def test_main_python_parses(self):
        ast.parse((ROOT / 'app' / 'main.py').read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main()
