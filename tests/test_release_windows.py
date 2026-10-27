import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core import evolution, paths, release_windows as rw


HISTORY = [
    {'tag': 'v1.0.0', 'version': '1.0.0', 'date': '2024-01-01',
     'released_at': '2024-01-01T08:00:00+08:00', 'release_time_source': 'github-release'},
    {'tag': 'v1.1.0', 'version': '1.1.0', 'date': '2024-01-02',
     'released_at': '2024-01-02T10:30:20+08:00', 'release_time_source': 'github-release'},
    {'tag': 'v1.2.0', 'version': '1.2.0', 'date': '2024-01-02',
     'released_at': '2024-01-02T11:00:00+08:00', 'release_time_source': 'github-release'},
]


class ReleaseWindowsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        p = patch.object(paths, 'DATA_ROOT', self.root); p.start(); self.addCleanup(p.stop)
        self.windows = rw.intervals(HISTORY)

    def infer(self, value, precision='second'):
        return rw.infer({'publish_time': value, 'publish_date_precision': precision}, self.windows)

    def test_boundaries_timezone_and_half_open_intervals(self):
        self.assertEqual(self.infer('2024-01-02T10:30:19+08:00')['version'], '1.0.0')
        self.assertEqual(self.infer('2024-01-02T02:30:20Z')['version'], '1.1.0')
        self.assertEqual(self.infer('2024-01-02T11:00:00+08:00')['version'], '1.2.0')
        self.assertEqual(self.infer('2024-01-02T12:00:00+08:00')['source'], 'github-release')

    def test_day_and_minute_crossing_boundary_stay_ambiguous(self):
        for value, precision in [('2024-01-02', 'day'), ('2024-01-02T10:30:00+08:00', 'minute')]:
            result = self.infer(value, precision)
            self.assertIsNone(result['version'])
            self.assertEqual(result['reason'], 'boundary_ambiguous')
        self.assertEqual(self.infer('2024-01-03', 'day')['version'], '1.2.0')
        self.assertEqual(self.infer('2023-12-31', 'day')['reason'], 'before_history')
        self.assertEqual(self.infer('2024-01-01', 'day')['reason'], 'boundary_ambiguous')

    def test_no_publication_time_does_not_use_receipt_or_observation_time(self):
        self.assertEqual(rw.infer({'published_at': '2024-01-02', 'observed_at': '2024-01-03'}, self.windows)['reason'], 'missing_publish_time')
        self.assertEqual(rw.infer({'publish_date': '2024-01-03'}, self.windows, False)['reason'], 'missing_public_snapshot')

    def test_release_date_without_time_does_not_invent_midnight_boundary(self):
        windows = rw.intervals([HISTORY[0], {'version': '1.1.0', 'tag': 'v1.1.0', 'date': '2024-01-02'}])
        for value in ('2024-01-02T00:00:00+08:00', '2024-01-02T23:59:00+08:00'):
            result = rw.infer({'publish_time': value, 'publish_date_precision': 'minute'}, windows)
            self.assertEqual(result['reason'], 'boundary_ambiguous')
            self.assertIsNone(result['version'])
        self.assertEqual(rw.infer({'publish_date': '2024-01-03'}, windows)['version'], '1.1.0')

    def test_zhihu_precision_is_preserved_and_not_downgraded(self):
        evolution.ingest_snapshot([{'aid': '1', 'publish_date': '2024-01-02', 'publish': '01-02 10:45', 'likes': 0}], '2024-01-09T23:00:00+08:00')
        evolution.ingest_snapshot([{'aid': '1', 'publish_date': '2024-01-02', 'likes': 4}], '2024-01-10T23:00:00+08:00')
        with patch.object(evolution, '_history', return_value=HISTORY):
            report = evolution.contract(7, as_of='2024-01-11', version='1.1.0', attribution_basis='publication')
        article = report['articles'][0]
        self.assertEqual(article['publish_time'], '2024-01-02T10:45:00+08:00')
        self.assertEqual(article['publish_date_precision'], 'minute')
        self.assertEqual(article['attributed_version'], '1.1.0')
        self.assertFalse(article['version_confirmed'])
        self.assertEqual(report['releases'][1]['time_metrics']['median'], 0)
        self.assertIsNone(report['releases'][1]['metrics']['median'])

    def test_legacy_precise_time_survives_old_day_only_state(self):
        evolution.ingest_snapshot([{'aid': '1', 'publish_date': '2024-01-02', 'likes': 2}], '2024-01-10')
        root = self.root / 'data'; root.mkdir(exist_ok=True)
        (root / 'published_answers_2024-01-09.json').write_text(json.dumps([
            {'aid': '1', 'publish': '01-02 10:45', 'metrics': {'赞同': '0'}}]), encoding='utf8')
        before = evolution._path().read_bytes()
        with patch.object(evolution, '_history', return_value=HISTORY):
            report = evolution.contract(7, as_of='2024-01-11')
        self.assertEqual(report['articles'][0]['attributed_version'], '1.1.0')
        self.assertEqual(evolution._path().read_bytes(), before)

    def test_year_rollover_and_invalid_time(self):
        a = {}; evolution._set_publication(a, {'publish': '12-31 23:59'}, '2025-01-01')
        self.assertEqual(a['publish_time'], '2024-12-31T23:59:00+08:00')
        a = {}; evolution._set_publication(a, {'publish': '2024-01-02 29:99'}, '2024-01-03')
        self.assertEqual(a, {})
        evolution._set_publication(a, {'publish_date': '2021-03-29', 'publish': '03-29 12:30'}, '2026-10-27')
        self.assertEqual(a['publish_time'], '2021-03-29T12:30:00+08:00')
        a = {}; evolution._set_publication(a, {'publish_date': '2021-03-29', 'publish': '04-30 12:30'}, '2026-10-27')
        self.assertEqual(a['publish_date'], '2021-03-29')
        self.assertIsNone(a['publish_time'])

    def response(self, rows):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(rows).encode()
        return response

    def test_sync_stable_only_and_overlay_new_boundaries(self):
        rows = [{'tag_name': 'v1.2.0', 'published_at': '2024-01-02T04:00:00Z'},
                {'tag_name': 'v1.3.0', 'published_at': '2024-01-04T04:00:00Z'},
                {'tag_name': 'v1.4.0', 'published_at': '2024-01-05T04:00:00Z', 'prerelease': True},
                {'tag_name': 'v1.5.0', 'published_at': '2024-01-06T04:00:00Z', 'draft': True}]
        with patch.object(rw, 'urlopen', return_value=self.response(rows)):
            result = rw.sync_releases()
        self.assertTrue(result['ok'])
        merged = rw.overlay(HISTORY)
        self.assertEqual(len(merged), 4)
        self.assertEqual(merged[2]['released_at'], '2024-01-02T12:00:00+08:00')
        self.assertEqual(rw.intervals(merged)[2]['interval_end'], '2024-01-04T12:00:00+08:00')

    def test_sync_failure_keeps_cache(self):
        with patch.object(rw, 'urlopen', return_value=self.response([
                {'tag_name': 'v1.2.0', 'published_at': '2024-01-02T04:00:00Z'}])):
            rw.sync_releases()
        before = rw._path().read_bytes()
        with patch.object(rw, 'urlopen', side_effect=OSError('offline')):
            self.assertFalse(rw.sync_releases()['ok'])
        with patch.object(rw, 'urlopen', return_value=self.response({'message': 'bad response'})):
            self.assertFalse(rw.sync_releases()['ok'])
        self.assertEqual(before, rw._path().read_bytes())

    def test_sync_can_restore_older_release_windows_without_inventing_code(self):
        with patch.object(rw, 'urlopen', return_value=self.response([
                {'tag_name': 'V0.9.0', 'published_at': '2023-12-01T04:00:00Z'}])):
            self.assertTrue(rw.sync_releases()['ok'])
        history = rw.overlay(HISTORY)
        self.assertEqual(history[0]['tag'], 'V0.9.0')
        self.assertNotIn('writing_code_id', history[0])
        self.assertEqual(rw.infer({'publish_date': '2023-12-02'}, rw.intervals(history))['version'], '0.9.0')

    def test_manual_generation_assignment_does_not_rewrite_time_cohort(self):
        evolution.ingest_snapshot([{'aid': '1', 'publish': '2024-01-03', 'likes': 2}], '2024-01-10')
        with patch.object(evolution, '_history', return_value=HISTORY):
            evolution.assign_article('1', None)
            report = evolution.contract(7, as_of='2024-01-11')
        self.assertEqual(report['articles'][0]['attribution_reason'], 'manual_clear')
        self.assertEqual(report['articles'][0]['attributed_version'], '1.2.0')


if __name__ == '__main__':
    unittest.main()
