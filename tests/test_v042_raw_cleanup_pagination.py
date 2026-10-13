import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class V042RawCleanupPaginationTests(unittest.TestCase):
    def test_raw_cleanup_runs_only_after_fact_refresh(self):
        src = (ROOT / 'app' / 'pipeline.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('purge_raw_feed_items_after_fact_refresh', funcs)
        run_src = ast.get_source_segment(src, funcs['run_pipeline'])
        self.assertLess(run_src.index('refresh_facts()'), run_src.index('purge_raw_feed_items_after_fact_refresh()'))
        purge_src = ast.get_source_segment(src, funcs['purge_raw_feed_items_after_fact_refresh'])
        self.assertIn('left join fact_news_article', purge_src.lower())
        self.assertIn('where a.raw_id=r.raw_id', purge_src)
        self.assertIn('Raw payload cleanup skipped', purge_src)

    def test_paginated_articles_topics_entities_have_total_count(self):
        src = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        for marker in ["@app.get('/api/articles'", "@app.get('/api/topics'", "@app.get('/api/entities'"]:
            self.assertIn(marker, src)
        self.assertIn('def pagination_meta(page: int, page_size: int, total: int)', src)
        self.assertIn("'pagination': pagination_meta(page, page_size, total)", src)
        self.assertIn('page_size: int = Query(default=25, ge=1, le=100)', src)

    def test_admin_has_pagination_controls_and_search_for_each_catalog(self):
        html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
        js = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
        for control in ['articlesPagination', 'topicsPagination', 'entitiesPagination', 'topicSearch', 'entitySearch']:
            self.assertIn(f'id="{control}"', html)
        self.assertIn('data-page-control', js)
        self.assertIn('page_size:String(listPageSize)', js)
        self.assertIn("/api/topics?all=true", js)

    def test_raw_layer_ui_explains_transient_cleanup(self):
        html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('Raw RSS chỉ lưu tạm', html)
        self.assertIn('Doraemon Mini Lakehouse <span>·</span> MVP 0.6.1', html)

if __name__ == '__main__':
    unittest.main()
