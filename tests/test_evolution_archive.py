"""Offline reconstruction and migration must preserve original evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution, feedback_archive, paths
from webui import _snapshot


class EvolutionArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = patch.object(paths, 'DATA_ROOT', self.root)
        self.scope.start()
        self.archive_scope = patch.object(feedback_archive, '_root', return_value=self.root/'data/feedback_archive')
        self.archive_scope.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.scope.stop)
        self.addCleanup(self.archive_scope.stop)

    def report(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
                patch('core.release_windows.urlopen', side_effect=AssertionError('network forbidden')):
            return evolution.contract(7, 'reads', as_of='2026-10-27', attribution_basis='publication')

    def test_reconstruct_from_original_archives_without_derived_state(self):
        rows = [{'aid': '1', 'publish': '2026-10-01 12:00', 'metrics': {'阅读': '0', '赞同': '3'}},
                {'aid': '2', 'publish': '2026-10-01', 'metrics': {'阅读': '20'}}]
        feedback_archive.save(rows, '2026-10-08T23:00:00.123456+08:00')
        report = self.report()
        self.assertFalse(evolution._path().exists())
        self.assertEqual(report['quality']['window_metrics']['valid_n'], 2)
        self.assertEqual(report['quality']['window_metrics']['median'], 10)
        self.assertEqual(report['quality']['public_articles'], 2)
        self.assertEqual(report['archive']['snapshot_count'], 1)

    def test_capture_and_state_are_one_observation_and_pruning_mirror_cannot_erase_it(self):
        rows = [{'aid': '1', 'publish': '2026-10-01', 'metrics': {'阅读': '0'}}]
        stamp = '2026-10-08T23:00:00.123456+08:00'
        feedback_archive.save(rows, stamp)
        evolution.ingest_snapshot(rows, stamp)
        mirror = self.root/'data/published_answers_2026-10-08.json'
        mirror.write_text(json.dumps(rows), encoding='utf8')
        self.assertEqual(_snapshot.prune_rows(mirror.parent, 'published_answers_*.json', 'aid', ['1']), 1)
        merged = evolution._merged(evolution._load()[0])
        self.assertEqual(len(merged['1']['observations']), 1)
        evolution._path().unlink()  # only this temporary fixture's derived cache
        self.assertEqual(self.report()['quality']['public_articles'], 1)

    def test_same_instant_with_different_timezone_is_not_counted_twice(self):
        rows = [{'aid': '1', 'metrics': {'阅读': '8'}}]
        feedback_archive.save(rows, '2026-10-08T15:00:00Z')
        evolution.ingest_snapshot(rows, '2026-10-08T15:00:00Z')
        self.assertEqual(len(evolution._merged(evolution._load()[0])['1']['observations']), 1)

    def test_local_rebuild_preserves_old_state_and_is_repeatable(self):
        rows = [{'aid': '1', 'publish': '2026-10-01', 'metrics': {'阅读': '0'}}]
        evolution.ingest_snapshot(rows, '2026-10-08T23:00:00+08:00')
        evolution.ingest_snapshot(rows, '2026-10-09T23:00:00+08:00')
        mirror = self.root/'data/published_answers_2026-10-08.json'
        mirror.write_text(json.dumps(rows), encoding='utf8')
        before = evolution._path().read_bytes()
        with patch('core.release_windows.urlopen', side_effect=AssertionError('network forbidden')):
            first = evolution.rebuild_local()
            second = evolution.rebuild_local()
        self.assertTrue(first['ok'])
        self.assertTrue(second['ok'])
        self.assertEqual(second['state_observation_groups_preserved'], 0)
        self.assertEqual(evolution._path().read_bytes(), before)
        original = evolution._merged(evolution._load()[0])['1']['observations']
        evolution._path().unlink()
        mirror.write_text('[]', encoding='utf8')
        rebuilt = evolution._merged(evolution._blank())['1']['observations']
        self.assertEqual(rebuilt, original)
        self.assertEqual(len(rebuilt), 2)
        self.assertEqual(first['archive']['day_count'], 2)
        self.assertIsNone(first['archive']['last_success_date'])

    def test_corrupt_archive_is_visible_and_not_used_for_report(self):
        result = feedback_archive.save([{'aid': '1', 'metrics': {'阅读': '8'}}], '2026-10-08T23:00:00+08:00')
        Path(result['path']).write_text('{broken', encoding='utf8')
        report = self.report()
        self.assertEqual(report['quality']['public_articles'], 0)
        self.assertEqual(report['archive']['invalid_count'], 1)
        self.assertFalse(evolution.rebuild_local()['ok'])

    def test_local_api_does_not_start_collection_or_release_sync(self):
        from fastapi.testclient import TestClient
        from webui.server import app
        with patch('webui.published.refresh_feedback', side_effect=AssertionError('collection forbidden')), \
                patch('core.release_windows.urlopen', side_effect=AssertionError('network forbidden')):
            result = TestClient(app).post('/api/evolution/rebuild')
            self.assertTrue(result.json()['ok'])
            self.assertEqual(TestClient(app).get('/api/evolution/archive').json()['snapshot_count'], 0)


if __name__ == '__main__':
    unittest.main()
