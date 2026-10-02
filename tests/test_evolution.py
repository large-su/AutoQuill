import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution


class EvolutionTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = patch('core.evolution.paths.data', lambda *x: os.path.join(self.d.name, *x))
        self.p.start()
        self.h = patch('core.evolution.paths.program', lambda *x: os.path.join(self.d.name, *x))
        self.h.start()

    def tearDown(self):
        self.h.stop()
        self.p.stop()
        self.d.cleanup()

    def test_numbers_zero_and_null(self):
        evolution.ingest_snapshot([{'aid': '1', 'metrics': {'赞同': '0', '阅读': 'bad'}}], '2026-01-31T00:00:00+00:00')
        a = evolution.contract()['articles'][0]['observations'][0]
        self.assertEqual(a['likes'], 0)
        self.assertIsNone(a['reads'])

    def test_draft_not_public_and_title_does_not_match(self):
        evolution.record_generation('a.md', 'body')
        evolution.record_draft('https://www.zhihu.com/question/9', 'a.md', 'same')
        self.assertIsNone(evolution.record_publication({'ok': True, 'url': 'https://www.zhihu.com/question/9'}))
        evolution.record_publication({'ok': True, 'qid': '8', 'title': 'same', 'url': 'https://www.zhihu.com/answer/1'})
        self.assertEqual(evolution.contract()['articles'][0]['attribution'], 'draft_match_pending')

    def test_idempotent_save_and_outlier(self):
        one = evolution.record_generation('a.md', 'body')
        two = evolution.record_generation('a.md', 'body')
        self.assertEqual(one['id'], two['id'])

    def test_window_never_uses_late_latest_and_manual_assignment_retains(self):
        evolution.ingest_snapshot([{'aid': 'a', 'publish_date': '2026-01-01', 'metrics': {'赞同': '9'}}], '2026-02-10T00:00:00+00:00')
        evolution.record_generation('x.md', 'x')
        node = evolution.contract()['nodes'][-1]['id']
        evolution.assign_article('a', node)
        article = evolution.contract(30)['articles'][0]
        self.assertIsNone(article['horizon_observation'])
        self.assertEqual(article['node_id'], node)

    def test_mature_without_observation_and_safe_snapshot(self):
        evolution.record_generation('x.md', 'body', {'LLM_MODEL_ID': 'test', 'api_key': 'secret'})
        evolution.record_draft('https://www.zhihu.com/question/1', 'x.md')
        evolution.record_publication({'ok': True, 'qid': '1', 'url': 'https://www.zhihu.com/answer/2'})
        evolution.ingest_snapshot([{'aid': '2', 'publish_date': '2020-01-01', 'metrics': {}}], '2020-01-02T00:00:00+00:00')
        evolution.assign_article('2', evolution.contract()['nodes'][-1]['id'])
        out = evolution.contract(30)
        self.assertEqual(out['nodes'][-1]['metrics']['missing'], 1)
        with open(os.path.join(self.d.name, 'data', 'state', 'evolution.json'), encoding='utf8') as f:
            self.assertNotIn('secret', f.read())

    def test_horizon_statistics_resist_an_outlier(self):
        rows = []
        for aid, likes in (('11', '1'), ('12', '2'), ('13', '100')):
            evolution.record_generation(aid + '.md', aid)
            evolution.record_draft('https://www.zhihu.com/question/' + aid, aid + '.md')
            evolution.record_publication({'ok': True, 'qid': aid, 'url': 'https://www.zhihu.com/answer/' + aid})
            rows.append({'aid': aid, 'publish_date': '2026-01-01', 'metrics': {'赞同': likes}})
        evolution.ingest_snapshot(rows, '2026-01-31T00:00:00+00:00')
        node = evolution.contract()['nodes'][-1]['id']
        for aid, _ in (('11', '1'), ('12', '2'), ('13', '100')):
            evolution.assign_article(aid, node)
        m = evolution.contract(30)['nodes'][-1]['metrics']
        self.assertEqual(m['median'], 2)
        self.assertGreater(m['mean'], m['median'])
        self.assertEqual(m['status'], 'insufficient')

    def test_legacy_snapshot_is_contextual_and_assignable(self):
        data_dir = os.path.join(self.d.name, 'data')
        os.makedirs(data_dir)
        legacy = [{'aid': 'old', 'publish': '01-01', 'metrics': {'赞同': '0'}}]
        with open(os.path.join(data_dir, 'published_answers_2026-02-01.json'), 'w', encoding='utf8') as f:
            __import__('json').dump(legacy, f)
        evolution.record_generation('x.md', 'x')
        out = evolution.contract()
        article = next((a for a in out['articles'] if a['aid'] == 'old'))
        self.assertEqual(article['observations'][0]['date_precision'], 'day')
        self.assertEqual(article['observations'][0]['likes'], 0)
        evolution.assign_article('old', out['nodes'][-1]['id'])
        self.assertEqual(next((a for a in evolution.contract()['articles'] if a['aid'] == 'old'))['attribution'], 'confirmed')

    def test_receipt_requires_public_snapshot_before_assignment(self):
        evolution.record_generation('x.md', 'x')
        evolution.record_draft('https://www.zhihu.com/question/1', 'x.md')
        evolution.record_publication({'ok': True, 'qid': '1', 'url': 'https://www.zhihu.com/answer/2'})
        self.assertFalse(evolution._load()[0]['drafts'][0]['public'])
        with self.assertRaises(ValueError):
            evolution.assign_article('2', evolution.contract()['nodes'][-1]['id'])
        evolution.ingest_snapshot([{'aid': '2', 'metrics': {'赞同': '0'}}], '2026-01-31')
        self.assertTrue(evolution._load()[0]['drafts'][0]['public'])

    def test_corrupt_state_is_not_replaced(self):
        path = os.path.join(self.d.name, 'data', 'state')
        os.makedirs(path)
        state = os.path.join(path, 'evolution.json')
        with open(state, 'w', encoding='utf8') as f:
            f.write('{"articles":[]}')
        self.assertIsNone(evolution.record_generation('x.md', 'x'))
        with open(state, encoding='utf8') as f:
            self.assertEqual(f.read(), '{"articles":[]}')
        self.assertTrue(evolution.contract()['warnings'])

    def test_fingerprint_matches_packaged_history_with_missing_files_and_crlf(self):
        from tools.evolution_history import current_writing_code_id
        source = Path(self.d.name, 'story_prompt.py')
        source.write_bytes(b'prompt = "test"\r\n')
        self.assertEqual(evolution._code_id(), current_writing_code_id(Path(self.d.name)))
        first = evolution._code_id()
        source.write_bytes(b'prompt = "test"\n')
        self.assertEqual(first, evolution._code_id())

    def test_restored_scheme_is_new_episode_and_old_feedback_stays(self):
        evolution.record_generation('a.md', 'first', {'LLM_MODEL_ID': 'A'})
        first_node = evolution.contract()['nodes'][-1]['id']
        evolution.ingest_snapshot([{'aid': '1', 'publish_date': '2026-01-01', 'metrics': {'赞同': '20'}}], '2026-01-31')
        evolution.assign_article('1', first_node)
        evolution.record_generation('b.md', 'second', {'LLM_MODEL_ID': 'B'})
        evolution.record_generation('c.md', 'third', {'LLM_MODEL_ID': 'A'})
        result = evolution.contract()
        self.assertEqual(len({node['id'] for node in result['nodes']}), 3)
        self.assertEqual(sum(node['current'] for node in result['nodes']), 1)
        self.assertEqual(result['articles'][0]['node_id'], first_node)
        self.assertEqual(result['nodes'][0]['metrics']['median'], 20)
        self.assertIsNone(result['nodes'][-1]['metrics']['median'])

    def test_clear_suppresses_exact_ledger_hint_and_receipt_replay(self):
        history = Path(self.d.name, 'core', 'evolution_history.json')
        history.parent.mkdir()
        history.write_text(json.dumps({'releases': [{'tag': 'v1.0.0', 'writing_code_id': 'test'}]}), encoding='utf-8')
        evolution.record_generation('a.md', 'first')
        evolution.record_draft('https://www.zhihu.com/question/1', 'a.md')
        receipt = {'ok': True, 'qid': '1', 'url': 'https://www.zhihu.com/answer/2'}
        evolution.record_publication(receipt)
        evolution.ingest_snapshot([{'aid': '2', 'publish_date': '2026-01-01', 'metrics': {'赞同': '3'}}], '2026-01-31')
        ledger = Path(self.d.name, 'data', 'state', 'published_topics.jsonl')
        ledger.write_text(json.dumps({'aid': '2', 'version': '1.0.0'}), encoding='utf-8')
        self.assertIsNotNone(evolution.contract()['articles'][0]['suggested_node_id'])
        evolution.assign_article('2', None)
        evolution.record_publication(receipt)
        article = evolution.contract()['articles'][0]
        self.assertIsNone(article['node_id'])
        self.assertIsNone(article['suggested_node_id'])
        self.assertTrue(all(node['metrics']['assigned'] == 0 for node in evolution.contract()['nodes']))

    def test_same_age_uses_newest_valid_observation_and_missing_does_not_hide_zero(self):
        row = {'aid': '1', 'publish_date': '2026-01-01', 'metrics': {'赞同': '1'}}
        evolution.ingest_snapshot([row], '2026-01-31T08:00:00+08:00')
        row['metrics'] = {'赞同': '0'}
        evolution.ingest_snapshot([row], '2026-01-31T09:00:00+08:00')
        row['metrics'] = {}
        evolution.ingest_snapshot([row], '2026-01-31T10:00:00+08:00')
        observation = evolution.contract()['articles'][0]['horizon_observation']
        self.assertEqual(observation['likes'], 0)
        self.assertIn('09:00', observation['observed_at'])

    def test_legacy_year_and_flat_zero_remain_uncertain(self):
        folder = Path(self.d.name, 'data')
        folder.mkdir()
        rows = [{'aid': 'old', 'publish': '12-31 12:00', 'likes': 0}, None,
                {'aid': 'raw', 'publish': '12-31 12:00', 'metrics': {'赞同': '0'}}]
        (folder / 'published_answers_2024-01-30.json').write_text(json.dumps(rows), encoding='utf-8')
        articles = {article['aid']: article for article in evolution.contract()['articles']}
        self.assertEqual(articles['old']['publish_date'], '2023-12-31')
        self.assertIsNone(articles['old']['horizon_observation'])
        self.assertEqual(articles['raw']['horizon_observation']['likes'], 0)

    def test_invalid_numbers_do_not_break_json_api(self):
        invalid = [float('nan'), float('inf'), -1, '9' * 400, True]
        for index, value in enumerate(invalid):
            evolution.ingest_snapshot([{'aid': str(index), 'metrics': {'赞同': value}}], '2026-01-31')
        self.assertTrue(all(article['latest']['likes'] is None for article in evolution.contract()['articles']))

    def test_state_with_incomplete_generation_refuses_overwrite(self):
        state = Path(self.d.name, 'data', 'state', 'evolution.json')
        state.parent.mkdir(parents=True)
        original = '{"generations":[{}],"drafts":[],"articles":{}}'
        state.write_text(original, encoding='utf-8')
        self.assertIsNone(evolution.record_generation('x.md', 'x'))
        self.assertEqual(state.read_text(encoding='utf-8'), original)
        self.assertTrue(any('稿件记录' in warning for warning in evolution.contract()['warnings']))

    def test_invalid_assignment_api_returns_400(self):
        from fastapi.testclient import TestClient
        from webui.server import app
        response = TestClient(app).post('/api/evolution/assign', json={'aid': 'missing', 'node_id': 'missing'})
        self.assertEqual(response.status_code, 400)

    def test_record_failure_does_not_prevent_story_save(self):
        from workflows.base import WorkflowBase
        with patch.object(evolution, 'record_generation', side_effect=OSError('test failure')):
            with self.assertLogs('workflows.base', level='WARNING'):
                path = WorkflowBase().save_story_file('story content')
        self.assertEqual(Path(path).read_text(encoding='utf-8'), 'story content')

    def test_frozen_history_is_used_without_git(self):
        history = Path(self.d.name, 'core', 'evolution_history.json')
        history.parent.mkdir()
        history.write_text(json.dumps({'releases': [{'tag': 'v1.0.0', 'writing_code_id': 'bundled-code'}]}), encoding='utf-8')
        with patch('subprocess.run', side_effect=AssertionError('runtime must not invoke Git')):
            provenance = evolution.record_generation('a.md', 'test', {})
            self.assertTrue(provenance['fingerprint'].startswith('bundled-code:'))

    def test_engineering_version_change_keeps_one_runtime_scheme(self):
        with patch.object(evolution, 'VERSION', '1.0.0'):
            evolution.record_generation('first.md', 'first story', {'LLM_MODEL_ID': 'A'})
        with patch.object(evolution, 'VERSION', '1.0.1'):
            evolution.record_generation('second.md', 'second story', {'LLM_MODEL_ID': 'A'})
        nodes = evolution.contract()['nodes']
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['releases'], ['v1.0.0', 'v1.0.1'])

    def test_installed_pending_record_uses_actual_build_commit(self):
        history = Path(self.d.name, 'core', 'evolution_history.json')
        history.parent.mkdir()
        history.write_text(json.dumps({'releases': [{'version': '1.0.0', 'tag': 'v1.0.0',
                                                    'pending': True, 'commit': None,
                                                    'writing_code_id': 'code'}]}), encoding='utf-8')
        Path(self.d.name, 'build_info.json').write_text(json.dumps({'version': '1.0.0',
                                                                 'source_commit': 'actual-build-commit'}), encoding='utf-8')
        release = evolution.contract()['releases'][0]
        self.assertEqual(release['commit'], 'actual-build-commit')

    def test_confirmed_legacy_observation_survives_snapshot_pruning(self):
        folder = Path(self.d.name, 'data')
        folder.mkdir()
        snapshot = folder / 'published_answers_2026-01-31.json'
        snapshot.write_text(json.dumps([{'aid': '1', 'publish': '2026-01-01',
                                         'metrics': {'赞同': '0'}}]), encoding='utf-8')
        evolution.record_generation('a.md', 'story', {})
        node = evolution.contract()['nodes'][-1]['id']
        evolution.assign_article('1', node)
        snapshot.unlink()
        result = evolution.contract()
        self.assertEqual(result['articles'][0]['node_id'], node)
        self.assertEqual(result['nodes'][-1]['metrics']['median'], 0)


