import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.growth_report import build_report, main, _number


class GrowthReportTests(unittest.TestCase):
    def test_decimal_counts_and_overflow(self):
        self.assertEqual(_number('1,234.5'), 1234.5)
        self.assertEqual(_number('1.2'), 1.2)
        self.assertEqual(_number('1.2万'), 12000)
        self.assertIsNone(_number('1,23'))
        self.assertIsNone(_number(10 ** 1000))
        self.assertIsNone(_number('9' * 1000))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, rows, generated_at=None):
        obj = {"rows": rows}
        if generated_at:
            obj["generated_at"] = generated_at
        (self.data / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def row(self, aid, publish_date="2026-01-01", reads=1000, likes=100, **kw):
        values = {"reads": reads, "likes": likes, "comments": kw.pop("comments", 10), "collects": kw.pop("collects", 0), "favors": kw.pop("favors", 0)}
        values.update(kw)
        return {"aid": aid, "publish_date": publish_date, **values, "content": "private body", "title": "private title", "url": "https://example/" + aid}

    def raw_row(self, aid, publish_date="2026-01-01", **metrics):
        return {"aid": aid, "publish_date": publish_date,
                "metrics": {"阅读": metrics.get("reads"), "赞同": metrics.get("likes"),
                            "评论": metrics.get("comments"), "收藏": metrics.get("collects"),
                            "喜欢": metrics.get("favors")}}

    def write_state(self, state):
        path = self.data / "state"
        path.mkdir(exist_ok=True)
        (path / "evolution.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def test_formats_dedup_missing_and_zero_are_distinct(self):
        self.write("published_answers_2026-01-08.json", [self.row("a", reads=0, likes=0), self.row("b", reads="bad", likes=None), self.row("c", publish_date="not-a-date"), self.raw_row("d", reads=0, likes=0, comments=0, collects=0, favors=0)])
        # Old nested format, same aid/day: later file's values are the one observation.
        (self.data / "published_answers_2026-01-08b.json").write_text(json.dumps({"rows": [{"aid": "a", "publish": "2026-01-01", "metrics": {"阅读": "2 千", "赞同": "10", "评论": "2", "收藏": "1", "喜欢": "0"}}]}), encoding="utf-8")
        report = build_report(self.tmp.name, horizon=7)
        stats = report["latest"]["stats"]
        self.assertEqual(report["latest"]["n"], 3)
        self.assertEqual(stats["reads"]["valid_n"], 2)
        self.assertEqual(stats["reads"]["zero_share"], 0.5)
        self.assertEqual(report["unknown_publish_date"], 1)
        self.assertEqual(stats["likes_per_1000_reads"]["eligible_n"], 1)
        self.assertEqual(stats["likes_per_1000_reads"]["mean"], 5)

    def test_normalized_zero_is_missing_but_fresh_zero_and_none_are_preserved(self):
        self.write("published_answers_2026-01-08.json", [self.row("a", reads=0, likes=0, comments=0)])
        self.write_state({"schema_version": 1, "articles": {
            "a": {"aid": "a", "publish_date": "2026-01-01", "observations": [
                {"observed_at": "2026-01-08T00:00:00+08:00", "source": "fresh", "reads": 0, "likes": None, "comments": 0}
            ]}
        }})
        report = build_report(self.tmp.name)
        stats = report["latest"]["stats"]
        self.assertEqual(stats["reads"]["valid_n"], 1)
        self.assertEqual(stats["reads"]["zero_share"], 1)
        self.assertEqual(stats["likes"]["valid_n"], 0)
        self.assertEqual(report["source_quality"]["ambiguous_legacy_zero_fields"], 5)
        self.assertIn("state/evolution.json", report["source_files"])

    def test_newest_same_day_fresh_wins_and_aid_url_deduplicates_across_sources(self):
        self.write("published_answers_2026-01-08.json", [self.raw_row("123", reads=99, likes=9, comments=9, collects=9, favors=9)])
        self.write_state({"schema_version": 1, "articles": {
            "different-key": {"url": "https://www.zhihu.com/question/1/answer/123", "publish_date": "2026-01-01", "observations": [
                {"observed_at": "2026-01-08T01:00:00+08:00", "source": "fresh", "reads": 1, "likes": 1},
                {"observed_at": "2026-01-08T02:00:00+08:00", "source": "fresh", "reads": 2, "likes": None}
            ]}
        }})
        report = build_report(self.tmp.name)
        self.assertEqual(report["observation_count"], 1)
        stats = report["latest"]["stats"]
        self.assertEqual(stats["reads"]["sum"], 2)
        self.assertEqual(stats["likes"]["valid_n"], 0)

    def test_fixed_window_uses_early_raw_read_not_mature_later_read(self):
        self.write_state({"schema_version": 1, "articles": {
            "a": {"aid": "a", "publish_date": "2026-10-01", "observations": [
                {"observed_at": "2026-10-08T00:00:00+08:00", "source": "fresh", "reads": 7, "likes": 1},
                {"observed_at": "2026-10-09T00:00:00+08:00", "source": "fresh", "reads": 900, "likes": 9}
            ]}
        }})
        report = build_report(self.tmp.name)
        self.assertEqual(report["fixed_horizon"]["stats"]["reads"]["sum"], 7)

    def test_bad_or_empty_state_does_not_break_snapshot(self):
        self.write("published_answers_2026-01-08.json", [self.raw_row("a", reads=1, likes=1, comments=1, collects=1, favors=1)])
        self.write_state({"schema_version": 1, "articles": {}})
        self.assertEqual(build_report(self.tmp.name)["latest"]["n"], 1)
        self.write_state({"schema_version": 1, "articles": []})
        report = build_report(self.tmp.name)
        self.assertEqual(report["latest"]["n"], 1)
        self.assertIn({"file": "state/evolution.json", "error": "invalid_schema"}, report["source_errors"])

    def test_fixed_window_does_not_use_later_observation_and_maturity_uses_observation_date(self):
        self.write("published_answers_2026-01-08.json", [self.row("a", reads=700, likes=70)])
        self.write("published_answers_2026-02-01.json", [self.row("a", reads=3000, likes=300)])
        report = build_report(self.tmp.name, horizon=7)
        self.assertEqual(report["latest"]["n"], 1)
        self.assertEqual(report["fixed_horizon"]["n"], 1)
        self.assertEqual(report["fixed_horizon"]["stats"]["reads"]["sum"], 700)

    def test_fixed_missing_and_per_thousand_denominator(self):
        self.write("published_answers_2026-01-07.json", [self.row("a", reads=1000, likes=25), self.row("b", reads=0, likes=50)])
        self.write("published_answers_2026-01-30.json", [self.row("a", reads=1000, likes=25), self.row("b", reads=0, likes=50)])
        report = build_report(self.tmp.name, horizon=7)
        fixed = report["fixed_horizon"]
        self.assertEqual(fixed["n"], 0)
        self.assertEqual(report["missing"], 2)
        self.assertEqual(report["latest"]["stats"]["likes_per_1000_reads"]["eligible_n"], 1)
        self.assertEqual(report["latest"]["stats"]["likes_per_1000_reads"]["mean"], 25)

    def test_since_and_json_output_contains_no_identifiers_or_content(self):
        self.write("published_answers_2026-01-08.json", [self.row("a"), self.row("b", publish_date="2025-12-01")])
        output = Path(self.tmp.name) / "report.json"
        main(["--data-root", self.tmp.name, "--since", "2026-01-01", "--output", str(output)])
        text = output.read_text(encoding="utf-8")
        self.assertNotIn("private body", text)
        self.assertNotIn("private title", text)
        self.assertNotIn("https://example", text)
        self.assertNotIn('"aid"', text)
        self.assertEqual(json.loads(text)["since"], "2026-01-01")

    def test_list_format_empty_and_bad_snapshot_are_reported(self):
        (self.data / "published_answers_2026-01-08.json").write_text("[]", encoding="utf-8")
        (self.data / "published_answers_bad.json").write_text("{bad", encoding="utf-8")
        report = build_report(self.tmp.name)
        self.assertIsNone(report["observation_cutoff_date"])
        self.assertEqual(report["observation_count"], 0)
        self.assertEqual(report["total_articles"], 0)
        self.assertEqual(report["source_errors"][0]["error"], "invalid_json")

    def test_rates_mean_and_pooled_differ_and_young_work_is_not_missing(self):
        self.write("published_answers_2026-01-07.json", [
            self.row("a", reads=100, likes=10), self.row("b", reads=1000, likes=50),
            self.row("young", publish_date="2026-01-30", reads=1, likes=1),
        ])
        # Mature observations for latest; no fixed-window observations exist.
        self.write("published_answers_2026-02-01.json", [
            self.row("a", reads=100, likes=10), self.row("b", reads=1000, likes=50),
            self.row("young", publish_date="2026-01-30", reads=1, likes=1),
        ])
        report = build_report(self.tmp.name)
        rate = report["latest"]["stats"]["likes_per_1000_reads"]
        self.assertEqual(rate["eligible_n"], 2)
        self.assertNotEqual(rate["mean"], rate["pooled"])
        self.assertEqual(report["missing"], 2)
        self.assertEqual(report["immature"], 1)

    def test_cli_subprocess_and_invalid_since(self):
        proc = subprocess.run([sys.executable, "tools/growth_report.py", "--data-root", self.tmp.name], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0)
        self.assertIn('"observation_cutoff_date": null', proc.stdout)
        bad = subprocess.run([sys.executable, "tools/growth_report.py", "--data-root", self.tmp.name, "--since", "2026-99-01"], capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--since must be YYYY-MM-DD", bad.stderr)


if __name__ == "__main__":
    unittest.main()
