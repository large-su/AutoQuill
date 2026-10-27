import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import feedback_archive as archive


class FeedbackArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.rows = [{"aid": "1", "metrics": {"阅读": "3", "赞同": "0"}}]

    def tearDown(self): self.tmp.cleanup()

    def test_same_day_multiple_snapshots_and_idempotency(self):
        a = archive.save(self.rows, "2026-10-27T23:00:00+08:00", archive_dir=self.root)
        b = archive.save([{"aid": "1", "metrics": {"阅读": "4"}}], "2026-10-27T23:05:00+08:00", archive_dir=self.root)
        c = archive.save(self.rows, "2026-10-27T23:00:00+08:00", archive_dir=self.root)
        self.assertFalse(a["idempotent"]); self.assertFalse(b["idempotent"]); self.assertTrue(c["idempotent"])
        self.assertEqual(len(list(self.root.glob("*/snapshot-*.json"))), 2)

    def test_corrupt_is_skipped_and_status_reports(self):
        archive.save(self.rows, "2026-10-25T23:00:00+08:00", archive_dir=self.root)
        archive.save(self.rows, "2026-10-27T23:00:00+08:00", archive_dir=self.root)
        path = sorted(self.root.glob("*/snapshot-*.json"))[-1]; path.write_text("{}", encoding="utf8")
        entries, warnings = archive.load(self.root)
        info = archive.status("2026-10-28T23:30:00+08:00", entries=entries, warnings=warnings, archive_dir=self.root)
        self.assertEqual(len(entries), 1); self.assertEqual(info["invalid_count"], 1)
        self.assertIn("2026-10-26", info["missing_dates"])

    def test_legacy_import_is_read_only_and_day_precision(self):
        source = self.data / "published_answers_2026-10-20.json"
        source.write_text(json.dumps(self.rows), encoding="utf8")
        before = source.read_bytes()
        result = archive.import_legacy(archive_dir=self.root, data_dir=self.data)
        self.assertEqual(result["imported"], 1); self.assertEqual(source.read_bytes(), before)
        entries, warnings = archive.load(self.root)
        self.assertFalse(warnings); self.assertEqual(entries[0]["precision"], "day")
        self.assertEqual(entries[0]["source"], "legacy-import")

    def test_timezone_and_atomic_failure(self):
        result = archive.save(self.rows, "2026-10-27T16:00:00Z", archive_dir=self.root)
        self.assertEqual(Path(result['path']).parent.name, "2026-10-28")
        with mock.patch.object(archive.os, "link", side_effect=OSError("disk")):
            with self.assertRaises(OSError): archive.save([{"aid": "2"}], "2026-10-28T00:00:00+08:00", archive_dir=self.root)
        self.assertFalse(list(self.root.glob("**/*.tmp")))

    def test_microseconds_are_preserved_and_invalid_or_tampered_evidence_is_never_reused(self):
        stamp = '2026-10-27T23:00:00.123456+08:00'
        result = archive.save(self.rows, stamp, archive_dir=self.root)
        entries, warnings = archive.load(self.root)
        self.assertFalse(warnings)
        self.assertEqual(entries[0]['observed_at'], stamp)
        path = Path(result['path'])
        path.write_text(path.read_text(encoding='utf8').replace('"3"', '"999"'), encoding='utf8')
        with self.assertRaises(ValueError):
            archive.save(self.rows, stamp, archive_dir=self.root)
        self.assertFalse(archive.load(self.root)[0])
        with self.assertRaises(ValueError):
            archive.save([{'aid': '', 'metrics': {}}], stamp, archive_dir=self.root)

    def test_tracking_records_first_failed_day_and_status_uses_as_of_timezone(self):
        first = archive.initialize('2026-10-20T23:05:00+08:00', archive_dir=self.root)
        self.assertFalse(first['idempotent'])
        self.assertTrue(archive.initialize('2026-10-21T23:00:00+08:00', archive_dir=self.root)['idempotent'])
        archive.save(self.rows, '2026-10-22T23:00:00+08:00', archive_dir=self.root)
        info = archive.status('2026-10-23T22:59:00+08:00', archive_dir=self.root)
        self.assertEqual(info['tracking_since'], '2026-10-20T23:05:00+08:00')
        self.assertEqual(info['missing_dates'], ['2026-10-20', '2026-10-21'])
        self.assertEqual(info['today_status'], 'pending')
        self.assertEqual(archive.status('2026-10-23T23:00:00+08:00', archive_dir=self.root)['today_status'], 'missing')

    def test_legacy_is_not_reimported_after_mutation_or_when_capture_exists(self):
        legacy = self.data / 'published_answers_2026-10-20.json'
        legacy.write_text(json.dumps(self.rows), encoding='utf8')
        self.assertEqual(archive.import_legacy(archive_dir=self.root, data_dir=self.data)['imported'], 1)
        legacy.write_text(json.dumps([{'aid': '1', 'metrics': {'阅读': '999'}}]), encoding='utf8')
        again = archive.import_legacy(archive_dir=self.root, data_dir=self.data)
        self.assertEqual(again['imported'], 0)
        archive.save(self.rows, '2026-10-21T23:00:00+08:00', archive_dir=self.root)
        (self.data / 'published_answers_2026-10-21.json').write_text(json.dumps(self.rows), encoding='utf8')
        self.assertEqual(archive.import_legacy(archive_dir=self.root, data_dir=self.data)['imported'], 0)

    def test_state_import_requires_original_observation_and_status_counts_rows(self):
        with self.assertRaises(ValueError):
            archive.save(self.rows, '2026-10-20T01:00:00+08:00', source='state-import',
                         completeness='unknown', archive_dir=self.root)
        state_rows = [dict(row, _observation={'observed_at': '2026-10-20T01:00:00+08:00', 'source': 'fresh'})
                      for row in self.rows]
        archive.save(state_rows, '2026-10-20T01:00:00+08:00', source='state-import',
                     completeness='unknown', archive_dir=self.root)
        self.assertEqual(archive.status('2026-10-21T00:00:00+08:00', archive_dir=self.root)['state_import_count'], 1)

    def test_concurrent_same_snapshot_is_verified_before_reporting_idempotency(self):
        original_link = archive.os.link
        def race(source, destination):
            original_link(source, destination)
            raise FileExistsError('another process won')
        with mock.patch.object(archive.os, 'link', side_effect=race):
            result = archive.save(self.rows, '2026-10-24T23:00:00+08:00', archive_dir=self.root)
        self.assertTrue(result['idempotent'])
        self.assertEqual(len(archive.load(self.root)[0]), 1)


if __name__ == "__main__": unittest.main()
