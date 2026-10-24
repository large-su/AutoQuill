"""Malformed snapshots and interrupted writes must preserve usable local data."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webui import _snapshot, drafts, published


class SnapshotResilienceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def seed(self, name, rows, stamp):
        path = self.root / name
        path.write_text(json.dumps(rows), encoding="utf-8")
        os.utime(path, (stamp, stamp))
        return path

    def test_invalid_roots_fall_back_in_both_dashboards(self):
        for module, prefix, valid in (
            (published, "published_answers", [{"aid": "1", "metrics": {"赞同": "12"}}]),
            (drafts, "drafts", [{"qid": "1", "content": "完整草稿"}]),
        ):
            with self.subTest(module=module.__name__):
                good = self.seed(prefix + "_old.json", valid, 1)
                for invalid in (None, 42, True, {"error": "unavailable"}):
                    with self.subTest(invalid=invalid):
                        self.seed(prefix + "_new.json", invalid, 2)
                        with mock.patch.object(module, "_DATA_DIR", self.root):
                            result = module.load()
                        self.assertEqual(result["total"], 1)
                        self.assertEqual(result["source_file"], str(good))

    def test_incomplete_flat_rows_do_not_break_quality_fallback(self):
        for module, prefix, valid, bad in (
            (published, "published_answers", [{"aid": "1", "metrics": {"赞同": "12"}}],
             [{"aid": "2", "title": "缺少指标"}]),
            (drafts, "drafts", [{"qid": "1", "content": "完整草稿"}],
             [{"qid": "2", "updated_date": "2026-10-24"}]),
        ):
            with self.subTest(module=module.__name__):
                good = self.seed(prefix + "_old.json", valid, 1)
                self.seed(prefix + "_new.json", bad, 2)
                with mock.patch.object(module, "_DATA_DIR", self.root):
                    self.assertEqual(module.load()["source_file"], str(good))
                good.unlink()
                with mock.patch.object(module, "_DATA_DIR", self.root):
                    self.assertEqual(module.load()["total"], 0)

    def test_prune_write_failure_preserves_original_bytes(self):
        path = self.seed("drafts_old.json", [{"qid": "1"}, {"qid": "2"}], 1)
        before = path.read_bytes()

        def failed_write(value, stream, **kwargs):
            stream.write("[")
            raise OSError("simulated disk full")

        with mock.patch.object(_snapshot.json, "dump", side_effect=failed_write):
            self.assertEqual(_snapshot.prune_rows(self.root, "drafts_*.json", "qid", ["1"]), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
