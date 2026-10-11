import unittest
from app.processing import canonicalize_url, clean_html, hash_text, keyword_matches, norm_name, slugify, apply_cleaning_rules

class ProcessingTests(unittest.TestCase):
    def test_canonicalize_removes_tracking(self):
        self.assertEqual(canonicalize_url('https://EXAMPLE.com/story/?utm_source=x&id=4#top'),'https://example.com/story/?id=4')
    def test_rejects_non_http_url(self):
        self.assertEqual(canonicalize_url('javascript:alert(1)'),'')
    def test_clean_html(self):
        self.assertEqual(clean_html('<p>Hello <b>world</b></p><script>bad()</script>'),'Hello world')
    def test_hash_normalizes_case_whitespace(self):
        self.assertEqual(hash_text('Hello   World'),hash_text('hello world'))
    def test_keyword_short_word_boundary(self):
        self.assertEqual(keyword_matches('AI is here, paid offer', ['AI']), ['AI'])
        self.assertEqual(keyword_matches('paid offer', ['AI']), [])
    def test_slugify(self):
        self.assertEqual(slugify('AI Agent Security'), 'ai-agent-security')
    def test_normalized_entity_name(self):
        self.assertEqual(norm_name('U.S. — Government'), 'u s government')
    def test_cleaning_rule_title(self):
        excluded, reason = apply_cleaning_rules('Sponsored: test', 'https://x.org/a', 1, [{'enabled':True,'rule_type':'exclude_title_contains','name':'no sponsored','config':{'keywords':['sponsored']}}])
        self.assertTrue(excluded)
        self.assertIn('no sponsored', reason)
    def test_cleaning_rule_min_title(self):
        excluded, _ = apply_cleaning_rules('short', 'https://x.org/a', 1, [{'enabled':True,'rule_type':'min_title_length','config':{'min_length':15}}])
        self.assertTrue(excluded)
    def test_disabled_rule_ignored(self):
        excluded, reason = apply_cleaning_rules('Sponsored: test', 'https://x.org/a', 1, [{'enabled':False,'rule_type':'exclude_title_contains','config':{'keywords':['sponsored']}}])
        self.assertFalse(excluded)
        self.assertIsNone(reason)

if __name__ == '__main__': unittest.main()
