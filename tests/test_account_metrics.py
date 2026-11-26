import json
import tempfile
import unittest
from pathlib import Path

from core.account_metrics import record_observation, report


class AccountMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_validation_zero_and_dedup(self):
        self.assertTrue(record_observation("acct-a", 0, "2026-11-01T01:00:00+08:00", self.tmp.name))
        self.assertFalse(record_observation("acct-a", 0, "2026-11-01T01:00:00+08:00", self.tmp.name))
        self.assertFalse(record_observation("acct-a", True, "2026-11-01T01:00:00+08:00", self.tmp.name))
        self.assertFalse(record_observation("acct-a", -1, "2026-11-01T01:00:00+08:00", self.tmp.name))
        self.assertFalse(record_observation("acct-a", 1, "2026-11-01T01:00:00", self.tmp.name))
        self.assertEqual(report(self.tmp.name)["accounts"][0]["earliest_count"], 0)

    def test_account_switching_is_not_mixed(self):
        record_observation("a", 10, "2026-11-03T00:00:00+08:00", self.tmp.name)
        record_observation("a", 15, "2026-11-01T00:00:00+08:00", self.tmp.name)
        record_observation("b", 100, "2026-11-02T00:00:00+08:00", self.tmp.name)
        result = report(self.tmp.name)
        self.assertEqual(result["account_count"], 2)
        self.assertEqual(result["valid_observation_n"], 3)
        self.assertEqual([x["net_change"] for x in result["accounts"]], [-5, None])
        self.assertNotIn('"id"', json.dumps(result))

    def test_offsets_are_sorted_as_instants_and_dates_use_site_timezone(self):
        record_observation('a', 10, '2026-11-01T23:30:00Z', self.tmp.name)
        record_observation('a', 12, '2026-11-02T08:00:00+08:00', self.tmp.name)
        result = report(self.tmp.name, since='2026-11-02')['accounts'][0]
        self.assertEqual(result['earliest_at'], '2026-11-02T07:30:00+08:00')
        self.assertEqual(result['net_change'], 2)

    def test_single_observation_unavailable_and_since(self):
        record_observation("a", 10, "2026-11-01T00:00:00+08:00", self.tmp.name)
        record_observation("a", 14, "2026-11-05T00:00:00+08:00", self.tmp.name)
        result = report(self.tmp.name, since="2026-11-04")
        self.assertEqual(result["account_count"], 1)
        self.assertEqual(result["accounts"][0]["valid_observation_n"], 1)
        self.assertIsNone(result["accounts"][0]["net_change"])
        self.assertEqual(result["accounts"][0]["change"], "unavailable")

    def test_invalid_lines_are_counted_and_read_is_non_mutating(self):
        path = Path(self.tmp.name) / "data" / "state" / "account_metrics.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"at":"bad"}\nnot-json\n', encoding="utf-8")
        before = path.read_bytes()
        result = report(self.tmp.name)
        self.assertEqual(result["invalid_n"], 2)
        self.assertEqual(result["unknown_n"], 2)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
