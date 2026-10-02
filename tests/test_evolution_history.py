import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.evolution_history import current_writing_code_id, refresh_history


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


class EvolutionHistoryTest(unittest.TestCase):
    def make_repo(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        d = Path(temporary.name)
        git(d, "init", "-q")
        git(d, "config", "user.email", "a@b")
        git(d, "config", "user.name", "A")
        (d / "story_prompt.py").write_text("one", encoding="utf8")
        git(d, "add", "."); git(d, "commit", "-qm", "base"); git(d, "tag", "v1.0.0")
        (d / "README.md").write_text("eng", encoding="utf8")
        git(d, "add", "."); git(d, "commit", "-qm", "engineering")
        git(d, "tag", "v1.1.0")
        (d / "story_prompt.py").write_text("two", encoding="utf8")
        git(d, "add", "."); git(d, "commit", "-qm", "writing")
        git(d, "tag", "v1.2.0")
        return d

    def test_intervals_order_and_fingerprint(self):
        d = self.make_repo(); out = d / "manifest.json"
        m = refresh_history(d, out)
        rs = m["releases"]
        self.assertEqual([r["tag"] for r in rs], ["v1.0.0", "v1.1.0", "v1.2.0"])
        self.assertEqual(rs[1]["summaries"], ["engineering"])
        self.assertEqual(rs[1]["change_kind"], "engineering")
        self.assertEqual(rs[2]["change_kind"], "writing")
        self.assertNotEqual(rs[0]["writing_code_id"], rs[2]["writing_code_id"])
        self.assertEqual(rs[1]["writing_code_id"], rs[0]["writing_code_id"])
        self.assertEqual(rs[1]["parent"], rs[0]["source_commit"])
        self.assertEqual(json.loads(out.read_text())["schema_version"], 1)

    def test_missing_fingerprint_is_explicit(self):
        d = self.make_repo(); (d / "story_prompt.py").unlink()
        git(d, "add", "-u"); git(d, "commit", "-qm", "remove")
        git(d, "tag", "v1.3.0")
        r = refresh_history(d)["releases"][-1]
        self.assertEqual(len(r["writing_code_id"]), 64)
        self.assertEqual(r["writing_files_changed"], ["story_prompt.py"])

    def test_pending_records_working_changes_without_false_commit(self):
        directory = self.make_repo()
        (directory / "story_prompt.py").write_text("new unsaved release", encoding="utf-8")
        manifest = refresh_history(directory, current_version="1.3.0", pending_summary="v1.3.0: improve")
        pending = manifest["releases"][-1]
        self.assertTrue(pending["pending"])
        self.assertIsNone(pending["commit"])
        self.assertEqual(pending["writing_code_id"], current_writing_code_id(directory))
        self.assertIn("story_prompt.py", pending["writing_files_changed"])
        self.assertEqual(pending["summary"], "v1.3.0: improve")

    def test_off_branch_tag_is_excluded_and_versions_sort_numerically(self):
        directory = self.make_repo()
        main_branch = git(directory, "branch", "--show-current").strip()
        git(directory, "checkout", "-qb", "detached-test")
        (directory / "off.txt").write_text("branch", encoding="utf-8")
        git(directory, "add", "off.txt")
        git(directory, "commit", "-qm", "off branch")
        git(directory, "tag", "v99.0.0")
        git(directory, "checkout", "-q", main_branch)
        git(directory, "tag", "v1.9.0")
        git(directory, "tag", "v1.10.0")
        tags = [row["tag"] for row in refresh_history(directory)["releases"]]
        self.assertNotIn("v99.0.0", tags)
        self.assertEqual(tags[-2:], ["v1.9.0", "v1.10.0"])

    def test_recent_window_uses_previous_tag_and_counts_all_files(self):
        directory = self.make_repo()
        for patch in range(1, 18):
            (directory / "README.md").write_text(str(patch), encoding="utf-8")
            git(directory, "add", "README.md")
            git(directory, "commit", "-qm", "engineering " + str(patch))
            git(directory, "tag", "v2.0." + str(patch))
        rows = refresh_history(directory)["releases"]
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0]["tag"], "v2.0.2")
        self.assertEqual(rows[0]["changed_files"], ["README.md"])
        self.assertEqual(rows[0]["parent"], git(directory, "rev-parse", "v2.0.1").strip())
        for index in range(45):
            (directory / ("a%02d.txt" % index)).write_text("new", encoding="utf-8")
        (directory / "story_prompt.py").write_text("changed", encoding="utf-8")
        git(directory, "add", ".")
        git(directory, "commit", "-qm", "writing plus files")
        git(directory, "tag", "v2.0.18")
        row = refresh_history(directory)["releases"][-1]
        self.assertGreater(row["changed_file_count"], 40)
        self.assertEqual(len(row["changed_files"]), 40)
        self.assertEqual(row["writing_files_changed"], ["story_prompt.py"])

    def test_baseline_persists_after_new_release(self):
        directory = self.make_repo(); out = directory / "manifest.json"
        for patch in range(1, 14):
            (directory / "README.md").write_text(str(patch), encoding="utf-8")
            git(directory, "add", "README.md")
            git(directory, "commit", "-qm", "engineering " + str(patch))
            git(directory, "tag", "v2.0." + str(patch))
        first = refresh_history(directory, out)
        baseline = first["baseline_tag"]
        self.assertEqual(len(first["releases"]), 16)
        (directory / "README.md").write_text("new", encoding="utf-8")
        git(directory, "add", "README.md"); git(directory, "commit", "-qm", "new release")
        git(directory, "tag", "v2.0.14")
        second = refresh_history(directory, out)
        self.assertEqual(second["baseline_tag"], baseline)
        self.assertEqual(second["releases"][0]["tag"], baseline)
        self.assertEqual(len(second["releases"]), 17)

    def test_pending_is_replaced_when_tag_appears(self):
        directory = self.make_repo(); out = directory / "manifest.json"
        (directory / "story_prompt.py").write_text("pending", encoding="utf-8")
        pending = refresh_history(directory, out, current_version="1.3.0")
        self.assertTrue(pending["releases"][-1]["pending"])
        git(directory, "add", "story_prompt.py"); git(directory, "commit", "-qm", "release")
        git(directory, "tag", "v1.3.0")
        published = refresh_history(directory, out, current_version="1.3.0")
        rows = [row for row in published["releases"] if row["tag"] == "v1.3.0"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("pending", False))
        self.assertIsNotNone(rows[0]["commit"])

    def test_schema1_without_baseline_migrates_and_bad_baseline_does_not_overwrite(self):
        directory = self.make_repo(); out = directory / "manifest.json"
        original = refresh_history(directory, out)
        legacy = dict(original); legacy.pop("baseline_tag")
        out.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = refresh_history(directory, out)
        self.assertEqual(migrated["baseline_tag"], "v1.0.0")
        legacy["baseline_tag"] = "v9.9.9"
        out.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaises(ValueError):
            refresh_history(directory, out)
        self.assertEqual(out.read_text(encoding="utf-8"), json.dumps(legacy))

    def test_corrupt_history_does_not_overwrite(self):
        directory = self.make_repo(); out = directory / "manifest.json"
        out.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            refresh_history(directory, out)
        self.assertEqual(out.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
