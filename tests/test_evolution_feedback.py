import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution, paths
from tools import evolution_feedback


class EvolutionFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = patch.object(paths, 'DATA_ROOT', str(self.root))
        self.program = patch.object(paths, 'PROGRAM_ROOT', str(self.root))
        self.data.start()
        self.program.start()

    def tearDown(self):
        self.program.stop()
        self.data.stop()
        self.temp.cleanup()

    def record(self, aid, metrics, when, publish='2024-01-01'):
        evolution.ingest_snapshot([{'aid': aid, 'publish_date': publish,
                                   'metrics': metrics}], when)

    def assign(self, aid, version='5.3.0'):
        with patch.object(evolution, 'VERSION', version):
            evolution.record_generation(aid + '.md', aid)
        node = evolution.contract()['nodes'][-1]['id']
        evolution.assign_article(aid, node)
        return node

    def test_daily_observations_preserve_zero_missing_and_retry_idempotency(self):
        self.record('1', {'赞同': '0', '评论': '3'}, '2024-01-08T23:00:00+08:00')
        self.record('1', {'赞同': '0', '评论': '3'}, '2024-01-08T23:00:00+08:00')
        self.record('1', {'赞同': '2', '评论': '2'}, '2024-01-09T23:00:00+08:00')
        article = evolution.contract(7, as_of='2024-01-09')['articles'][0]
        self.assertEqual(len(article['observations']), 2)
        self.assertEqual(article['horizon_observation']['likes'], 0)
        self.assertIsNone(article['horizon_observation']['reads'])
        self.assertEqual(article['latest_change']['metrics']['likes'], 2)
        self.assertEqual(article['latest_change']['metrics']['comments'], -1)
        self.assertIsNone(article['latest_change']['metrics']['collects'])
        self.assertIsNone(article['window_deltas']['7_14']['likes'])

    def test_metric_windows_are_independent_and_late_totals_do_not_backfill(self):
        self.record('1', {'阅读': '100', '评论': '0'}, '2024-01-08T23:00:00+08:00')
        self.record('1', {'阅读': '180', '评论': '4', '赞同': '12'}, '2024-01-15T23:00:00+08:00')
        self.record('1', {'阅读': '260', '评论': '7', '赞同': '20'}, '2024-01-31T23:00:00+08:00')
        self.record('1', {'阅读': '5000', '评论': '500', '赞同': '800'}, '2024-02-15T23:00:00+08:00')
        self.assign('1')
        result = evolution.contract(7, 'reads', as_of='2024-02-16')
        article = result['articles'][0]
        self.assertEqual(article['horizon_observation']['reads'], 100)
        self.assertIsNone(article['horizon_observations']['likes'])
        self.assertEqual(article['window_deltas']['7_14']['reads']['value'], 80)
        self.assertEqual(article['window_deltas']['14_30']['likes']['value'], 8)
        self.assertEqual(result['nodes'][-1]['metrics']['median'], 100)
        self.assertEqual(result['nodes'][-1]['metrics']['by_metric']['likes']['valid_n'], 0)

    def test_daily_file_mirrors_do_not_zero_the_real_daily_change(self):
        data_dir = self.root / 'data'
        data_dir.mkdir()
        for day, likes in (('2024-01-08', 0), ('2024-01-09', 3)):
            row = {'aid': '1', 'publish_date': '2024-01-01', 'metrics': {'赞同': str(likes)}}
            (data_dir / f'published_answers_{day}.json').write_text(json.dumps([row]), encoding='utf-8')
            evolution.ingest_snapshot([row], day + 'T23:00:00+08:00')
        article = evolution.contract(7, as_of='2024-01-10')['articles'][0]
        self.assertEqual(len(article['observations']), 2)
        self.assertEqual(article['latest_change']['metrics']['likes'], 3)

    def test_publication_age_and_missing_are_distinct_from_unassigned(self):
        self.record('old', {'赞同': '500'}, '2024-02-15T23:00:00+08:00')
        self.record('young', {'赞同': '1'}, '2024-02-15T23:00:00+08:00', '2024-02-14')
        evolution.ingest_snapshot([{'aid': 'unknown', 'likes': 2}], '2024-02-15')
        for aid in ('old', 'young', 'unknown'):
            self.assign(aid)
        result = evolution.contract(7, as_of='2024-02-16')
        states = {a['aid']: a['window_status'] for a in result['articles']}
        self.assertEqual(states, {'old': 'missing', 'young': 'immature', 'unknown': 'unknown_publish'})
        metrics = result['nodes'][-1]['metrics']
        self.assertEqual((metrics['observed'], metrics['missing'], metrics['immature'], metrics['unknown_publish']),
                         (0, 1, 1, 1))

    def test_receipt_version_remains_original_and_manual_clear_survives_refresh(self):
        with patch.object(evolution, 'VERSION', '5.2.0'):
            evolution.record_generation('first.md', 'first')
        evolution.record_draft('https://www.zhihu.com/question/1', 'first.md')
        evolution.record_publication({'ok': True, 'qid': '1', 'url': 'https://www.zhihu.com/answer/2'})
        self.record('2', {'赞同': '3'}, '2024-01-08')
        node = evolution.contract()['nodes'][-1]['id']
        evolution.assign_article('2', node)
        evolution.assign_article('2', None)
        with patch.object(evolution, 'VERSION', '5.4.0'):
            self.record('2', {'赞同': '6'}, '2024-01-09')
            article = evolution.contract(7, as_of='2024-01-10')['articles'][0]
        self.assertEqual(article['generation_version'], '5.2.0')
        self.assertIsNone(article['node_id'])
        self.assertIsNone(article['suggested_node_id'])
        self.assertEqual(article['assignment_source'], 'manual_clear')

    def test_refresh_nodes_survive_restart_and_failure_retains_last_success(self):
        first = evolution.start_refresh('automation', 'daily-key')
        evolution.finish_refresh(first['id'], status='done', count=1,
                                 observed_at='2024-01-08T23:00:00+08:00',
                                 source_file='/private/data/published_answers_2024-01-08.json')
        pending = evolution.start_refresh('automation', 'next-day')
        with patch.object(evolution, '_RUN_ID', 'new-process'):
            status = evolution.refresh_status()
        self.assertEqual(status['latest']['status'], 'interrupted')
        self.assertEqual(status['last_success']['id'], first['id'])
        self.assertEqual(status['last_success']['source_file'], 'published_answers_2024-01-08.json')
        evolution.finish_refresh(pending['id'], status='error', message='offline')
        self.assertEqual(evolution.refresh_status()['latest']['message'], 'offline')
        self.assertEqual(evolution.refresh_status()['total'], 2)

    def test_filtered_query_and_cli_are_read_only(self):
        self.record('1', {'赞同': '3'}, '2024-01-08')
        self.record('2', {'赞同': '5'}, '2024-02-08', '2024-02-01')
        state = evolution._path()
        before = state.read_bytes()
        self.assertEqual([a['aid'] for a in evolution.contract(since='2024-02-01')['articles']], ['2'])
        output = self.root / 'reports' / 'query.json'
        with patch('builtins.print'):
            self.assertEqual(evolution_feedback.main(['--data-root', str(self.root), '--aid', '1',
                                                      '--horizon', '7', '--output', str(output)]), 0)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['articles'][0]['aid'], '1')
        self.assertEqual(state.read_bytes(), before)
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            evolution_feedback.main(['--output', str(state)])
        self.assertEqual(state.read_bytes(), before)

    def test_corrupt_state_is_preserved_and_query_errors_are_explicit(self):
        evolution._path().parent.mkdir(parents=True)
        evolution._path().write_text('{bad', encoding='utf-8')
        with self.assertRaises(ValueError):
            evolution.start_refresh()
        self.assertEqual(evolution._path().read_text(encoding='utf-8'), '{bad')
        with self.assertRaises(ValueError):
            evolution.contract(since='invalid')

    def test_old_generation_without_version_is_not_labeled_as_current_release(self):
        evolution.record_generation('old.md', 'body')
        state, _ = evolution._load()
        state['generations'][0].pop('version')
        evolution._save(state)
        with patch.object(evolution, 'VERSION', '5.4.0'):
            node = evolution.contract()['nodes'][-1]
        self.assertEqual(node['releases'], [])
        self.assertIn('版本未知', node['label'])


if __name__ == '__main__':
    unittest.main()
