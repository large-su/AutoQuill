# -*- coding: utf-8 -*-
"""批量质量优先（BATCH_QUALITY_FIRST）测试：
- run_batch 的四个质量分支走单轮语义（源码锚点守护）
- 单轮式素材精选：循环 extract_content、去重、异常继续、空轮上限
- 质量优先生成：每篇走带反馈重试（generate_story_with_retry）；API 并行、Web 串行
- 批量择优排序叠加账号题材先验（TOPIC_PRIOR_IN_SCORE）
"""
import unittest
from unittest import mock

from workflows.base import WorkflowBase
from workflows.workflow_batch import BatchGenerationMixin


class RunBatchQualityAnchorsTest(unittest.TestCase):
    """源码锚点守护：run_batch 的质量优先分支必须存在（防回归）。"""

    def test_run_batch_single_style_collect_branch(self):
        import inspect
        src = inspect.getsource(WorkflowBase.run_batch)
        self.assertIn("_collect_materials_single_style(target)", src)
        self.assertIn("BATCH_QUALITY_FIRST", src)
        self.assertIn("collect_materials_batch(target)", src)  # 旧模式仍可切回

    def test_run_batch_quality_generation_branches(self):
        import inspect
        src = inspect.getsource(WorkflowBase.run_batch)
        self.assertIn("_batch_generate_api_quality", src)
        self.assertIn("_batch_generate_web_quality", src)
        self.assertIn("_apply_prior_to_scores(scored)", src)


class _FakeCollector:
    """仅实现 extract_content 的桩：依次吐素材/重复/异常，最后耗尽。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def extract_content(self):
        self.calls += 1
        r = self.responses.pop(0) if self.responses else None
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise RuntimeError("候选池枯竭（模拟）")
        return r  # (title, answer, footer, url)


class CollectSingleStyleTest(unittest.TestCase):

    def test_collects_target_and_dedupes(self):
        fake = _FakeCollector([
            ("题A", "答A", {}, "u1"),
            ("题B", "答B", {}, "u2"),
            ("题A", "答A", {}, "u1"),   # 重复 → 跳过
            ("题C", "答C", {}, "u3"),
        ])
        mats = WorkflowBase._collect_materials_single_style(fake, 3)
        self.assertEqual([m["title"] for m in mats], ["题A", "题B", "题C"])
        self.assertGreaterEqual(fake.calls, 4)

    def test_exception_continues_next_round(self):
        fake = _FakeCollector([
            RuntimeError("选题失败（模拟）"),
            ("题A", "答A", {}, "u1"),
            ("题B", "答B", {}, "u2"),
        ])
        mats = WorkflowBase._collect_materials_single_style(fake, 2)
        self.assertEqual([m["title"] for m in mats], ["题A", "题B"])

    def test_empty_rounds_limit_stops(self):
        # 连续空轮超限即停，不无限循环
        fake = _FakeCollector([
            RuntimeError("失败1"), RuntimeError("失败2"),
            RuntimeError("失败3"), RuntimeError("失败4"),
            RuntimeError("失败5"), RuntimeError("失败6"),
            RuntimeError("失败7"), RuntimeError("失败8"),
            RuntimeError("失败9"),
        ])
        with mock.patch("config.story.BATCH_COLLECT_MAX_EMPTY_ROUNDS", 5):
            mats = WorkflowBase._collect_materials_single_style(fake, 2)
        self.assertEqual(mats, [])


class _FakeGenerator:
    """提供质量生成所需两接口的桩。"""

    def __init__(self, story="合格故事文本" * 200, ok=True):
        self.story = story or None
        self.ok = ok
        self.retry_calls = 0
        self.saved = []

    def generate_story_with_retry(self, title, answer, max_attempts=None,
                                  min_length=500):
        self.retry_calls += 1
        return self.story, self.ok

    def save_story_file(self, story, index=None):
        self.saved.append((story, index))
        return "output/story_%s.md" % index


class RunOneStoryQualityTest(unittest.TestCase):

    def test_success_sets_mat_fields(self):
        fake = _FakeGenerator()
        mat = {"index": 3, "title": "题", "answer": "答"}
        ok = BatchGenerationMixin._run_one_story_quality(fake, mat)
        self.assertTrue(ok)
        self.assertEqual(mat["story"], fake.story)
        self.assertTrue(mat["ok"])
        self.assertEqual(mat["md_path"], "output/story_3.md")
        self.assertIn("format_score", mat)

    def test_failure_keeps_best_and_marks_not_ok(self):
        fake = _FakeGenerator(story=None, ok=False)
        mat = {"index": 1, "title": "题", "answer": "答"}
        ok = BatchGenerationMixin._run_one_story_quality(fake, mat)
        self.assertFalse(ok)
        self.assertFalse(mat["ok"])


class BatchQualityGenerationTest(unittest.TestCase):

    def test_api_quality_parallel_processes_all(self):
        class Fake(BatchGenerationMixin):
            def __init__(self, mats):
                self.mats = mats

            def _run_one_story_quality(self, mat):
                mat["story"] = "s-" + str(mat["index"])
                mat["ok"] = True
                return True

        mats = [{"index": i, "title": "t%d" % i, "answer": "a"}
                for i in (1, 2)]
        fake = Fake(mats)
        BatchGenerationMixin._batch_generate_api_quality(
            fake, mats, lambda p, n: None, lambda: None)
        self.assertEqual({m["story"] for m in mats},
                         {"s-1", "s-2"})

    def test_web_quality_serial_order(self):
        class Fake(BatchGenerationMixin):
            def __init__(self):
                self.seen = []

            def _run_one_story_quality(self, mat):
                self.seen.append(mat["index"])

        fake = Fake()
        mats = [{"index": i, "title": "t%d" % i, "answer": "a"} for i in (1, 2, 3)]
        BatchGenerationMixin._batch_generate_web_quality(fake, mats)
        self.assertEqual(fake.seen, [1, 2, 3])


class ApplyPriorToScoresTest(unittest.TestCase):

    def test_sort_by_score_times_genre_prior(self):
        scored = [
            {"title": "量子计算书籍推荐", "score": 20},
            {"title": "有没有甜甜的小说推荐？", "score": 10},
        ]

        def _mult(title):
            return 3.0 if "甜" in title else 1.0

        with mock.patch("core.feedback_loop.topic_genre_multiplier",
                        side_effect=_mult):
            out = BatchGenerationMixin._apply_prior_to_scores(scored)
        self.assertEqual(out[0]["title"], "有没有甜甜的小说推荐？")
        self.assertEqual(out[0]["prior_boost"], 3.0)
        self.assertEqual(out[0]["score_weighted"], 30.0)

    def test_disabled_keeps_original_order(self):
        scored = [
            {"title": "量子计算书籍推荐", "score": 20},
            {"title": "有没有甜甜的小说推荐？", "score": 10},
        ]
        with mock.patch("config.story.TOPIC_PRIOR_IN_SCORE", False):
            out = BatchGenerationMixin._apply_prior_to_scores(scored)
        self.assertEqual(out[0]["title"], "量子计算书籍推荐")

    def test_multiplier_failure_does_not_block(self):
        scored = [{"title": "题", "score": 5}]
        with mock.patch("core.feedback_loop.topic_genre_multiplier",
                        side_effect=RuntimeError("no data")):
            out = BatchGenerationMixin._apply_prior_to_scores(scored)
        self.assertEqual(out[0]["prior_boost"], 1.0)
        self.assertEqual(out[0]["score_weighted"], 5.0)


if __name__ == "__main__":
    unittest.main()
