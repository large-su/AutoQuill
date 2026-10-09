import sqlite3
import tempfile
import threading
import unittest

from tools.story_study import canonical_url, catalog, db_path, history, listing, record


URL = "https://www.zhihu.com/market/paid_column/123/section/456"


def study_row(**changes):
    row = {
        "title": "A", "url": URL, "read_date": "2026-01-01", "scope": "partial",
        "coverage": "opening", "lessons": ["one"], "batch_id": "b",
    }
    row.update(changes)
    return row


class StoryStudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_url_canonicalization_and_status_progression(self):
        self.assertEqual(canonical_url(URL + "?x=1#part"), URL)
        catalog([{"title": "A", "url": URL + "?x=1"}], self.root)
        self.assertEqual(listing(self.root)["sources"][0]["status"], "unread")
        row = study_row()
        record([row, row], self.root)
        self.assertEqual(listing(self.root)["sources"][0]["study_count"], 1)
        self.assertEqual(listing(self.root)["sources"][0]["status"], "partial")
        record([study_row(scope="full", read_date="2026-01-02")], self.root)
        self.assertEqual(listing(self.root)["sources"][0]["status"], "complete")
        self.assertEqual(len(history(URL, self.root)["studies"]), 2)

    def test_invalid_batch_does_not_change_existing_database(self):
        record([study_row()], self.root)
        before = history(URL, self.root)
        with self.assertRaises(ValueError):
            record([study_row(read_date="2026-01-02"), study_row(url="https://evil.example/1")], self.root)
        self.assertEqual(history(URL, self.root), before)
        with self.assertRaises(ValueError):
            record([study_row(manuscript="secret")], self.root)

    def test_validation_limits_and_types(self):
        cases = (
            {"scope": []}, {"title": "x" * 161}, {"author": "x" * 161},
            {"coverage": "x" * 241}, {"batch_id": "x" * 81},
            {"applied_version": "x" * 31}, {"lessons": []},
            {"lessons": ["x"] * 7}, {"lessons": ["x" * 501]},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                record([study_row(**changes)], self.root)

    def test_read_only_missing_history_and_corrupt_database(self):
        self.assertEqual(listing(self.root)["counts"]["all"], 0)
        self.assertEqual(history(URL, self.root)["studies"], [])
        self.assertFalse(db_path(self.root).exists())
        path = db_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not sqlite")
        with self.assertRaises(ValueError):
            listing(self.root)
        self.assertEqual(path.read_bytes(), b"not sqlite")

    def test_strict_date_types_and_author_preservation(self):
        for read_date in ("20260101", "2026-02-30"):
            with self.subTest(read_date=read_date), self.assertRaises(ValueError):
                record([study_row(read_date=read_date, author="Author")], self.root)
        record([study_row(author="Author")], self.root)
        record([study_row()], self.root)
        self.assertEqual(history(URL, self.root)["studies"][0]["author"], "Author")
        with self.assertRaises(ValueError):
            listing(self.root, [])

    def test_schema_version_uri_and_invalid_existing_schema(self):
        special = tempfile.TemporaryDirectory(prefix="story # space ")
        try:
            record([study_row(url=URL + "?view=1", scope="full")], special.name)
            self.assertEqual(listing(special.name)["schema_version"], 1)
            db = db_path(special.name)
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version=9")
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError):
                catalog([{"title": "B", "url": URL}], special.name)
        finally:
            special.cleanup()

    def test_existing_schema_without_unique_deduplication_is_rejected(self):
        path = db_path(self.root)
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.executescript("""
            PRAGMA user_version=1;
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
                author TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE studies (
                id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, author TEXT, read_date TEXT NOT NULL,
                scope TEXT NOT NULL, coverage TEXT NOT NULL, lessons TEXT NOT NULL,
                batch_id TEXT NOT NULL, applied_version TEXT, payload TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX idx_studies_hash ON studies(payload_sha256);
        """)
        conn.close()
        with self.assertRaises(ValueError):
            record([study_row()], self.root)

    def test_concurrent_first_catalog_and_record_preserves_all_data(self):
        barrier = threading.Barrier(2)
        failures = []

        def run(operation, rows):
            try:
                barrier.wait()
                operation(rows, self.root)
            except Exception as exc:
                failures.append(exc)

        catalog_item = {"title": "Catalog", "url": URL + "?catalog=1"}
        recorded = study_row(
            title="Recorded",
            url="https://www.zhihu.com/market/paid_column/999/section/888",
        )
        threads = [
            threading.Thread(target=run, args=(catalog, [catalog_item])),
            threading.Thread(target=run, args=(record, [recorded])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        result = listing(self.root)
        self.assertEqual(result["counts"]["all"], 2)
        self.assertEqual(sum(source["study_count"] for source in result["sources"]), 1)
        self.assertEqual(record([recorded], self.root)["count"], 0)


if __name__ == "__main__":
    unittest.main()
