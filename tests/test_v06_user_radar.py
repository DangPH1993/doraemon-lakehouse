import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load_test_app():
    import sys
    import types
    # The lightweight test environment may not install psycopg; routes mock DB calls.
    if 'psycopg' not in sys.modules:
        fake_psycopg = types.ModuleType('psycopg')
        fake_psycopg.__path__ = []
        fake_psycopg.connect = lambda *args, **kwargs: None
        fake_rows = types.ModuleType('psycopg.rows')
        fake_rows.dict_row = object()
        sys.modules['psycopg'] = fake_psycopg
        sys.modules['psycopg.rows'] = fake_rows
    from app.main import app
    return app

class UserRadarV06Tests(unittest.TestCase):
    def test_migration_creates_user_and_saved_timeline_tables_without_touching_articles(self):
        sql = (ROOT / 'sql' / 'migrations' / '007_user_accounts_and_saved_radar.sql').read_text(encoding='utf-8').lower()
        self.assertIn('create table if not exists app_users', sql)
        self.assertIn('create table if not exists user_saved_timelines', sql)
        self.assertIn("check (timeline_type in ('topic', 'entity'))", sql)
        self.assertIn('references topics(topic_id) on delete cascade', sql)
        self.assertIn('references entities(entity_id) on delete cascade', sql)
        self.assertNotIn('delete from news_articles', sql)

    def test_canonical_schema_includes_v06_user_tables(self):
        schema = (ROOT / 'sql' / 'schema.sql').read_text(encoding='utf-8')
        self.assertIn('CREATE TABLE IF NOT EXISTS app_users', schema)
        self.assertIn('CREATE TABLE IF NOT EXISTS user_saved_timelines', schema)

    def test_auth_and_user_timeline_routes_exist(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        for route in [
            "@app.post('/api/user-auth/register')",
            "@app.post('/api/user-auth/login')",
            "@app.post('/api/user-auth/logout')",
            "@app.get('/api/user-auth/me')",
            "@app.get('/api/user/timeline-options')",
            "@app.get('/api/user/saved-timelines')",
            "@app.put('/api/user/saved-timelines')",
            "@app.get('/api/public/news/entities/{entity_id}/timeline')",
        ]:
            self.assertIn(route, main)
        self.assertIn('Depends(require_user)', main)

    def test_password_hashing_and_user_token_are_separate_from_admin_token(self):
        from app import security
        secret_settings = SimpleNamespace(session_secret='unit-test-secret', admin_username='admin')
        with patch.object(security, 'settings', secret_settings):
            hashed = security.hash_password('A-strong-password-123')
            self.assertNotEqual(hashed, 'A-strong-password-123')
            self.assertTrue(security.verify_password('A-strong-password-123', hashed))
            self.assertFalse(security.verify_password('wrong-password', hashed))
            token = security.issue_user_token(42, 'radaruser')
            claims = security.read_user_token(token)
            self.assertEqual(claims['sub'], 42)
            self.assertEqual(claims['typ'], 'news-user')
            self.assertFalse(security.valid_token(token))

    def test_login_api_rejects_unauthenticated_me_requests(self):
        from fastapi.testclient import TestClient
        app = load_test_app()
        response = TestClient(app).get('/api/user-auth/me')
        self.assertEqual(response.status_code, 401)

    def test_register_sets_http_only_cookie_and_password_is_hashed(self):
        from fastapi.testclient import TestClient
        from unittest.mock import Mock
        from app import security
        app = load_test_app()
        settings_for_test = SimpleNamespace(session_secret='auth-test-secret', admin_username='admin')
        inserted_user = {'user_id': 13, 'username': 'radar_test', 'email': 'test@example.org'}
        insert_mock = Mock(return_value=inserted_user)
        with patch('app.main.settings', settings_for_test), patch.object(security, 'settings', settings_for_test), \
             patch('app.main.fetch_one', insert_mock), patch('app.db.fetch_one', return_value=inserted_user):
            client = TestClient(app)
            response = client.post('/api/user-auth/register', json={
                'username': 'radar_test', 'email': 'test@example.org', 'password': 'Strong-pass-123'
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('news_radar_session=', response.headers.get('set-cookie', ''))
            self.assertIn('httponly', response.headers.get('set-cookie', '').lower())
            stored_hash = insert_mock.call_args.args[1][4]
            self.assertNotEqual(stored_hash, 'Strong-pass-123')
            self.assertTrue(security.verify_password('Strong-pass-123', stored_hash))
            me = client.get('/api/user-auth/me')
            self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(me.json()['user']['username'], 'radar_test')

    def test_entity_timeline_returns_chronological_articles(self):
        from datetime import datetime, timezone
        from fastapi.testclient import TestClient
        app = load_test_app()
        with patch('app.main.fetch_one', side_effect=[
            {'entity_id': 9, 'canonical_name': 'Example Person', 'entity_type': 'PERSON', 'description': ''},
            {'total': 1},
        ]), patch('app.main.fetch_all', return_value=[{
            'article_id': 71, 'title': 'Entity news', 'url': 'https://example.org/story',
            'source_name': 'Example Source', 'snippet': 'Short excerpt',
            'timeline_at': datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc), 'topics': 'World'
        }]):
            response = TestClient(app).get('/api/public/news/entities/9/timeline?page=1&page_size=10')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['entity']['canonical_name'], 'Example Person')
        self.assertEqual(payload['items'][0]['title'], 'Entity news')
        self.assertEqual(payload['pagination']['total'], 1)

    def test_news_page_contains_account_controls_and_personal_radar(self):
        html = (ROOT / 'static' / 'news.html').read_text(encoding='utf-8')
        js = (ROOT / 'static' / 'news.js').read_text(encoding='utf-8')
        self.assertIn('data-auth-open="register"', html)
        self.assertIn('id="myRadar"', html)
        self.assertIn('id="savedTimelinesContainer"', html)
        self.assertIn('/api/user/saved-timelines', js)
        self.assertIn('/api/public/news/entities/${encodeURIComponent(id)}/timeline', js)
        self.assertIn('MAX_SAVED_TIMELINES = 10', js)

    def test_python_source_parses(self):
        ast.parse((ROOT / 'app' / 'main.py').read_text(encoding='utf-8'))
        ast.parse((ROOT / 'app' / 'security.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
