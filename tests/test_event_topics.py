import unittest
from app.processing import extract_event_signature
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class EventTopicTests(unittest.TestCase):
    def test_attack_events_get_specific_stable_name(self):
        a = extract_event_signature('Mỹ đánh Iran')
        b = extract_event_signature('Iran bị Mỹ không kích')
        self.assertIsNotNone(a)
        self.assertEqual(a['name'], 'Mỹ tấn công Iran')
        self.assertEqual(a['event_key'], b['event_key'])

    def test_modality_is_not_collapsed_into_actual_attack(self):
        a = extract_event_signature('Mỹ đe dọa tấn công Iran')
        b = extract_event_signature('Mỹ tấn công Iran')
        self.assertNotEqual(a['event_key'], b['event_key'])
        self.assertIn('đe dọa', a['name'])


    def test_negated_attack_is_not_merged_with_actual_attack(self):
        actual = extract_event_signature('Mỹ tấn công Iran')
        negated = extract_event_signature('Mỹ không tấn công Iran')
        self.assertIsNotNone(negated)
        self.assertNotEqual(actual['event_key'], negated['event_key'])
        self.assertIn('không tấn công', negated['name'])

    def test_exercise_topic_is_specific(self):
        event = extract_event_signature('NATO tập trận tại Italy')
        self.assertEqual(event['name'], 'NATO tập trận Italy')
        self.assertEqual(event['parent_slug'], 'defence-security')


    def test_single_entity_with_action_and_object_gets_specific_event(self):
        event = extract_event_signature('OpenAI ra mắt mô hình AI mới')
        self.assertIsNotNone(event)
        self.assertTrue(event['name'].startswith('OpenAI ra mắt '))
        self.assertIn('mô hình AI mới', event['name'])

    def test_vietnamese_agency_event_gets_specific_label(self):
        event = extract_event_signature('Bộ Công an khởi tố vụ án lừa đảo')
        self.assertIsNotNone(event)
        self.assertTrue(event['name'].startswith('Bộ Công an khởi tố '))

    def test_broad_category_without_action_is_not_event(self):
        self.assertIsNone(extract_event_signature('Mỹ và Iran căng thẳng'))

    def test_migration_supports_event_topics_and_scan_marker(self):
        sql = (ROOT / 'sql/migrations/004_event_specific_topics.sql').read_text(encoding='utf-8')
        self.assertIn('topic_type', sql)
        self.assertIn('event_key', sql)
        self.assertIn('event_topic_scanned_at', sql)

if __name__ == '__main__':
    unittest.main()
