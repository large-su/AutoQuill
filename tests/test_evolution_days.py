import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta

from core import evolution, paths, release_windows as rw


HISTORY = [
    {'version': '1.0.0', 'tag': 'v1.0.0', 'released_at': '2024-01-01T09:00:00+08:00',
     'release_time_source': 'github-release', 'summary': '改进开头', 'changed_files': ['a.py']},
    {'version': '1.0.1', 'tag': 'v1.0.1', 'released_at': '2024-01-01T23:59:59+08:00',
     'release_time_source': 'github-release', 'summary': '修复收尾', 'changed_files': ['b.py']},
    {'version': '1.1.0', 'tag': 'v1.1.0', 'released_at': '2024-01-03T15:59:59Z',
     'release_time_source': 'github-release', 'summary': '降低耗时', 'changed_files': ['c.py']},
    {'version': '1.2.0', 'tag': 'v1.2.0', 'released_at': '2024-01-03T16:00:00Z',
     'release_time_source': 'github-release', 'summary': '新方案', 'changed_files': ['d.py']},
]


class DailyEvaluationTest(unittest.TestCase):
    def test_same_day_actions_are_kept_under_final_release(self):
        original = copy.deepcopy(HISTORY)
        days = rw.daily_intervals(HISTORY)
        self.assertEqual([d['version'] for d in days], ['1.0.1', '1.1.0', '1.2.0'])
        self.assertEqual(days[0]['evaluation_id'], 'day:2024-01-01')
        self.assertEqual(days[0]['release_count'], 2)
        self.assertEqual(days[0]['changed_files'], ['a.py', 'b.py'])
        self.assertIn('改进开头', days[0]['summary'])
        self.assertIn('修复收尾', days[0]['summary'])
        self.assertEqual(days[0]['included_releases'], HISTORY[:2])
        self.assertEqual(HISTORY, original)
        for day in days[:-1]:
            self.assertGreaterEqual(rw.timestamp(day['interval_end']) - rw.timestamp(day['interval_start']), timedelta(days=1))

    def test_entire_day_and_gap_days_belong_to_final_node(self):
        days = rw.daily_intervals(HISTORY)
        for value, expected in [('2024-01-01', '1.0.1'), ('2024-01-01T00:00:00+08:00', '1.0.1'),
                                ('2024-01-01T10:00:00+08:00', '1.0.1'), ('2024-01-02', '1.0.1'),
                                ('2024-01-03T23:59:59+08:00', '1.1.0'), ('2024-01-03T16:00:00Z', '1.2.0')]:
            with self.subTest(value=value):
                result = rw.infer({'publish_time': value}, days)
                self.assertEqual(result['version'], expected)
                self.assertEqual(result['reason'], 'daily_release_window')
        self.assertEqual(rw.infer({'publish_date': '2023-12-31'}, days)['reason'], 'before_history')
        self.assertEqual(rw.infer({}, days)['reason'], 'missing_publish_time')

    def test_date_only_releases_choose_latest_version_for_same_day(self):
        days = rw.daily_intervals([{'version': v, 'tag': 'v'+v, 'date': '2024-01-01'} for v in ('1.0.9','1.0.10','1.0.2')])
        self.assertEqual(days[0]['version'], '1.0.10')
        self.assertEqual(days[0]['daily_order_source'], 'version-number-fallback')
        self.assertEqual(rw.infer({'publish_date': '2024-01-01'}, days)['version'], '1.0.10')

    def test_report_and_member_version_query_use_same_daily_cohort_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(paths, 'DATA_ROOT', Path(temporary)), \
                patch.object(evolution, '_history', return_value=HISTORY):
            evolution.ingest_snapshot([
                {'aid': '1', 'publish': '2024-01-01 00:01', 'reads': 0},
                {'aid': '2', 'publish': '2024-01-01 10:45', 'reads': 20},
                {'aid': '3', 'publish': '2024-01-02', 'reads': None},
                {'aid': '4', 'publish': '2024-01-03', 'reads': 9},
            ], '2024-01-09T23:00:00+08:00')
            before = evolution._path().read_bytes()
            report = evolution.contract(7, 'reads', as_of='2024-01-10', version='1.0.0', attribution_basis='publication')
            self.assertEqual(report['query']['evaluation_version'], '1.0.1')
            self.assertEqual(report['query']['evaluation_granularity'], 'calendar_day')
            self.assertEqual({a['aid'] for a in report['articles']}, {'1','2','3'})
            self.assertEqual(len(report['release_history']), 4)
            node = report['releases'][0]
            self.assertEqual(node['time_metrics']['assigned'], 3)
            self.assertEqual(node['time_metrics']['valid_n'], 2)
            self.assertEqual(node['time_metrics']['median'], 10)
            self.assertEqual(node['time_metrics']['missing'], 1)
            self.assertEqual(node['metrics']['assigned'], 0)
            self.assertEqual(before, evolution._path().read_bytes())


if __name__ == '__main__':
    unittest.main()
