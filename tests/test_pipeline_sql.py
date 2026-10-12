import ast
import unittest
from pathlib import Path


class PipelineSQLTests(unittest.TestCase):
    def _assert_function_placeholders(self, function_name, min_calls=1):
        source = Path(__file__).resolve().parents[1].joinpath('app', 'pipeline.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        function_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
        execute_calls = []
        for node in ast.walk(function_node):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'execute':
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            query = node.args[0].value
            params_node = node.args[1] if len(node.args) > 1 else None
            if params_node is None:
                param_count = 0
            elif isinstance(params_node, (ast.Tuple, ast.List)):
                param_count = len(params_node.elts)
            else:
                self.fail(f'Unexpected SQL params representation for query: {query[:80]}')
            execute_calls.append((query, param_count))
        self.assertGreaterEqual(len(execute_calls), min_calls, f'{function_name} did not contain expected SQL calls')
        mismatches = [(query.splitlines()[0][:80], query.count('%s'), count)
                      for query, count in execute_calls if query.count('%s') != count]
        self.assertEqual(mismatches, [], f'SQL placeholders do not match parameter counts in {function_name}: {mismatches}')

    def test_refresh_facts_sql_placeholder_counts_match(self):
        self._assert_function_placeholders('refresh_facts', min_calls=7)

    def test_refresh_topic_trending_sql_placeholder_counts_match(self):
        self._assert_function_placeholders('refresh_topic_trending', min_calls=3)

    def test_run_pipeline_sql_placeholder_counts_match(self):
        self._assert_function_placeholders('run_pipeline', min_calls=3)


if __name__ == '__main__':
    unittest.main()
