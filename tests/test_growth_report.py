import json
import os
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

    def test_all_interaction_rates_report_quantiles_and_effective_reads(self):
        self.write("published_answers_2026-01-08.json", [
            self.raw_row("a", reads=100, likes=10, comments=2, collects=1, favors=0),
            self.raw_row("b", reads=1000, likes=50, comments=10, collects=5, favors=2),
        ])
        stats = build_report(self.tmp.name)["latest"]["stats"]
        for metric in ("likes", "comments", "collects", "favors"):
            rate = stats[f"{metric}_per_1000_reads"]
            self.assertEqual(rate["eligible_n"], 2)
            self.assertEqual(rate["effective_reads"], 1100)
            self.assertEqual(rate["reads_sum"], 1100)
            self.assertIsNotNone(rate["median"])
            self.assertIsNotNone(rate["p25"])

    def test_fixed_groups_keep_genre_inference_and_verified_attribution_separate(self):
        digest = "text-v1:" + ("a" * 64)
        self.write_state({"schema_version": 1, "generations": [
            {"id": "g1", "episode": "ep1", "version": "5.13.0", "settings": {"WORKFLOW_MODE": "classic", "LLM_PROVIDER": "stale-settings-provider"},
             "model_chain": {"channel": "api", "provider": "actual-provider", "model": "actual-model", "options": {"mode": "fast", "secret": "must-not-appear"}}},
        ], "drafts": [{"id": "d1", "generation_id": "g1", "content_fingerprint": digest}],
            "publication_receipts": [{"id": "r1", "aid": "verified", "draft_content_fingerprint": digest},
                                      {"id": "r2", "aid": "sameqid", "draft_content_fingerprint": "text-v1:" + ("b" * 64)}],
            "articles": {
                "verified": {"aid": "verified", "title": "悬疑灵异故事", "publish_date": "2026-01-01",
                              "generation_id": "g1", "publication_evidence": {"draft_id": "d1", "receipt_id": "r1", "generation_id": "g1", "content_fingerprint": digest},
                              "observations": [{"observed_at": "2026-01-08T00:00:00+08:00", "source": "fresh", "reads": 100, "likes": 10, "comments": 2, "collects": 1, "favors": 0}]},
                "unverified": {"aid": "unverified", "title": "悬疑灵异未核验", "publish_date": "2026-01-01",
                                "generation_id": "g1", "observations": [{"observed_at": "2026-01-08T00:00:00+08:00", "source": "fresh", "reads": 100, "likes": 5, "comments": 1, "collects": 0, "favors": 0}]},
                "sameqid": {"aid": "sameqid", "title": "悬疑灵异指纹不符", "publish_date": "2026-01-01", "generation_id": "g1",
                             "publication_evidence": {"draft_id": "d1", "receipt_id": "r2", "generation_id": "g1", "content_fingerprint": "text-v1:" + ("b" * 64)},
                             "observations": [{"observed_at": "2026-01-08T00:00:00+08:00", "source": "fresh", "reads": 100, "likes": 4, "comments": 1, "collects": 0, "favors": 0}]},
                "manual": {"aid": "manual", "title": "悬疑灵异手工方案", "publish_date": "2026-01-01", "generation_id": "g1",
                            "node_id": "ep1", "assignment_source": "manual", "observations": [{"observed_at": "2026-01-08T00:00:00+08:00", "source": "fresh", "reads": 100, "likes": 3, "comments": 1, "collects": 0, "favors": 0}]},
            }})
        groups = build_report(self.tmp.name)["fixed_horizon"]["groups"]
        self.assertEqual(groups["genre"]["悬疑/灵异"]["n"], 4)
        self.assertEqual(groups["attribution"]["confirmed"]["n"], 2)
        self.assertEqual(groups["attribution"]["unknown"]["n"], 2)
        self.assertEqual(groups["attribution"]["by_dimension"]["version"]["5.13.0"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["workflow"]["classic"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["episode"]["ep1"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["channel"]["api"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["provider"]["actual-provider"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["model"]["actual-model"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["model_mode"]["fast"]["n"], 1)
        self.assertEqual(groups["attribution"]["by_dimension"]["version"]["unknown"]["n"], 3)
        cohorts = list(groups["cohorts"].values())
        self.assertTrue(any(c["labels"]["model_mode"] == "fast" and c["n"] == 1 for c in cohorts))
        self.assertTrue(any(c["labels"]["attribution"] == "confirmed" and c["labels"]["model_mode"] == "unknown" for c in cohorts))
        encoded = json.dumps(groups, ensure_ascii=False)
        self.assertNotIn("stale-settings-provider", encoded)
        self.assertNotIn("secret", encoded)

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
        for encoding in ("cp1252", "cp936"):
            with self.subTest(encoding=encoding):
                env = os.environ.copy()
                env.update({"PYTHONIOENCODING": encoding, "PYTHONUTF8": "0"})
                proc = subprocess.run([sys.executable, "tools/growth_report.py", "--data-root", self.tmp.name], capture_output=True, env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
                output = proc.stdout.decode("utf-8")
                report = json.loads(output)
                self.assertIsNone(report["observation_cutoff_date"])
                self.assertIn("描述性全量公开快照上下文", report["all_public_context"])

        bad = subprocess.run([sys.executable, "tools/growth_report.py", "--data-root", self.tmp.name, "--since", "2026-99-01"], capture_output=True, env=env)
        self.assertNotEqual(bad.returncode, 0, bad.stdout.decode("utf-8", errors="replace"))
        self.assertIn("--since must be YYYY-MM-DD", bad.stderr.decode("utf-8", errors="replace"))

    def test_corpus_comparison_reports_retained_absent_new_without_ids(self):
        self.write("published_answers_2026-01-08.json", [
            self.raw_row("keep", reads=100, likes=10),
            self.raw_row("gone", reads=200, likes=20),
            self.raw_row("zero", reads=0, likes=0),
        ])
        self.write("published_answers_2026-01-10.json", [
            self.raw_row("keep", reads=150, likes=15),
            self.raw_row("new", reads=50, likes=5),
            self.raw_row("zero", reads=None, likes=None),
        ])
        report = build_report(self.tmp.name, compare_from="2026-01-08",
                              compare_to="2026-01-10")
        comparison = report["corpus_comparison"]
        self.assertEqual((comparison["before_n"], comparison["after_n"]), (3, 3))
        self.assertEqual((comparison["retained_n"], comparison["absent_from_snapshot_n"], comparison["new_in_snapshot_n"]), (2, 1, 1))
        likes = comparison["metrics"]["likes"]
        self.assertEqual(likes["matched_n"], 1)
        self.assertEqual(likes["retained_before"]["sum"], 10)
        self.assertEqual(likes["retained_after"]["sum"], 15)
        self.assertEqual(likes["delta"], 5)
        self.assertEqual(likes["missing_before_total"], {"valid_n": 1, "sum": 20})
        self.assertEqual(likes["new_after_total"], {"valid_n": 1, "sum": 5})
        self.assertEqual(likes["retained_before_missing_n"], 0)
        self.assertEqual(likes["retained_after_missing_n"], 1)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("keep", encoded)
        self.assertNotIn("private body", encoded)

    def test_corpus_comparison_requires_exact_snapshot_dates_and_both_bounds(self):
        self.write("published_answers_2026-01-08.json", [self.raw_row("a", reads=1, likes=1)])
        with self.assertRaisesRegex(ValueError, "provided together"):
            build_report(self.tmp.name, compare_from="2026-01-08")
        with self.assertRaisesRegex(ValueError, "no published snapshot"):
            build_report(self.tmp.name, compare_from="2026-01-07", compare_to="2026-01-08")
        with self.assertRaisesRegex(ValueError, "not be later"):
            build_report(self.tmp.name, compare_from="2026-01-09", compare_to="2026-01-08")
        with self.assertRaises(ValueError):
            build_report(self.tmp.name, compare_from="", compare_to="2026-01-08")

    def test_empty_snapshot_is_a_valid_comparison_endpoint(self):
        self.write("published_answers_2026-01-08.json", [], generated_at="2026-01-08T01:00:00")
        self.write("published_answers_2026-01-10.json", [self.raw_row("a", reads=2, likes=1)])
        report = build_report(self.tmp.name, since="2026-01-10",
                              compare_from="2026-01-08", compare_to="2026-01-10")
        comparison = report["corpus_comparison"]
        self.assertEqual((comparison["before_n"], comparison["after_n"]), (0, 1))
        self.assertEqual(comparison["new_in_snapshot_n"], 1)
        self.assertEqual(comparison["comparison_scope"], "full_snapshots_unfiltered_by_since")

    def test_malformed_snapshot_schema_is_not_an_empty_endpoint(self):
        self.write("published_answers_2026-01-08.json", [self.raw_row("a", reads=2, likes=1)])
        (self.data / "published_answers_2026-01-10.json").write_text(
            json.dumps({"rows": "error"}), encoding="utf-8")
        report = build_report(self.tmp.name)
        self.assertIn({"file": "published_answers_2026-01-10.json",
                       "error": "invalid_schema"}, report["source_errors"])
        with self.assertRaisesRegex(ValueError, "no published snapshot"):
            build_report(self.tmp.name, compare_from="2026-01-08",
                         compare_to="2026-01-10")

    def test_nonempty_to_legal_empty_snapshot_reports_absent_set(self):
        self.write("published_answers_2026-01-08.json", [
            self.raw_row("a", reads=2, likes=1),
            self.raw_row("b", reads=3, likes=2),
        ])
        self.write("published_answers_2026-01-10.json", [])
        comparison = build_report(
            self.tmp.name, compare_from="2026-01-08", compare_to="2026-01-10"
        )["corpus_comparison"]
        self.assertEqual((comparison["before_n"], comparison["after_n"]), (2, 0))
        self.assertEqual(comparison["absent_from_snapshot_n"], 2)
        self.assertEqual(comparison["new_in_snapshot_n"], 0)


if __name__ == "__main__":
    unittest.main()
