import ast
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class V04GenAITimelineTests(unittest.TestCase):
    def test_genai_prompt_requires_specific_event_not_broad_topic(self):
        source = (ROOT / 'app' / 'genai_events.py').read_text(encoding='utf-8')
        self.assertIn("Không dùng tên quá rộng kiểu", source)
        self.assertIn("cùng hành động/sự việc chính", source)
        self.assertIn("OPENAI_API_KEY is not configured", source)

    def test_genai_config_and_action_secrets_are_present(self):
        config = (ROOT / 'app' / 'config.py').read_text(encoding='utf-8')
        workflow = (ROOT / '.github' / 'workflows' / 'daily-pipeline.yml').read_text(encoding='utf-8')
        render = (ROOT / 'render.yaml').read_text(encoding='utf-8')
        self.assertIn("openai_model", config)
        self.assertIn("secrets.OPENAI_API_KEY", workflow)
        self.assertIn("key: OPENAI_API_KEY", render)

    def test_openai_classifier_uses_json_mode_and_gpt_model(self):
        source = (ROOT / 'app' / 'genai_events.py').read_text(encoding='utf-8')
        env_example = (ROOT / '.env.example').read_text(encoding='utf-8')
        self.assertIn("https://api.openai.com/v1/chat/completions", source)
        self.assertIn("'response_format': {'type': 'json_object'}", source)
        self.assertIn("'gpt-4.1-mini'", source)
        self.assertIn('OPENAI_API_KEY=', env_example)
        self.assertNotIn('GEMINI_API_KEY', env_example)

    def test_openai_call_sends_bearer_and_parses_results(self):
        fake_psycopg = types.ModuleType('psycopg')
        fake_psycopg.__path__ = []
        fake_psycopg.connect = lambda *args, **kwargs: None
        fake_rows = types.ModuleType('psycopg.rows')
        fake_rows.dict_row = object()

        fake_response = SimpleNamespace(
            ok=True,
            status_code=200,
            text='',
            json=lambda: {"choices": [{"message": {"content": '{"results":[{"article_id":1,"event_topic_name":"Mỹ không kích Iran"}]}'}}]},
        )
        fake_settings = SimpleNamespace(
            openai_api_key='test-api-key',
            openai_model='gpt-4.1-mini',
            ai_request_timeout_seconds=10,
        )
        with patch.dict(sys.modules, {'psycopg': fake_psycopg, 'psycopg.rows': fake_rows}):
            from app import genai_events
        with patch.object(genai_events, 'settings', fake_settings), patch.object(
            genai_events.requests, 'post', return_value=fake_response
        ) as post:
            result = genai_events._call_openai(
                [{'article_id': 1, 'title': 'Mỹ không kích Iran', 'summary': '', 'language': 'vi'}],
                [],
                [],
            )
        self.assertEqual(result[0]['event_topic_name'], 'Mỹ không kích Iran')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-api-key')
        self.assertEqual(kwargs['json']['model'], 'gpt-4.1-mini')
        self.assertEqual(kwargs['json']['response_format'], {'type': 'json_object'})

    def test_topic_timeline_api_and_admin_editor_exist(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
        js = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("@app.get('/api/topics/{topic_id}/timeline'", main)
        self.assertIn("@app.patch('/api/articles/{article_id}/classifications'", main)
        self.assertIn('id="page-timeline"', html)
        self.assertIn('id="classificationDialog"', html)
        self.assertIn('data-action="edit-classification"', js)
        self.assertNotIn("$('#topicCandidates')", js)
        self.assertNotIn("$('#entityCandidates')", js)

    def test_migration_adds_ai_state_and_eliminates_approval_dependency(self):
        migration = (ROOT / 'sql' / 'migrations' / '005_genai_topics_and_manual_classification.sql').read_text(encoding='utf-8')
        self.assertIn('ai_classified_at', migration)
        self.assertIn('ai_classification jsonb', migration)
        self.assertIn("UPDATE topic_candidates SET status = 'approved'", migration)
        self.assertIn("UPDATE entity_candidates SET status = 'approved'", migration)

    def test_main_and_pipeline_parse(self):
        for path in [ROOT/'app'/'main.py',ROOT/'app'/'pipeline.py',ROOT/'app'/'genai_events.py']:
            ast.parse(path.read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main()
