# -*- coding: utf-8 -*-
"""P1 质量与可靠性：生成自检清单、Web 降级断路器、提取门槛自适应、
看板日均互动指标。均为纯逻辑/桩测试，不触网不触浏览器。
"""
import datetime
import unittest
from unittest import mock


class PromptSelfCheckTest(unittest.TestCase):

    def test_build_prompt_contains_self_check_section(self):
        from story_prompt import build_story_prompt
        msg, _ = build_story_prompt("测试问题")
        self.assertIn("发布前自检", msg)
        self.assertIn("引言：正文第一行必须直接是故事正文", msg)

    def test_self_check_constant_mirrors_validator_deductions(self):
        from story_prompt import FORMAT_SELF_CHECK_RULE
        self.assertGreater(len(FORMAT_SELF_CHECK_RULE), 100)
        for kw in ("引言", "章节", "量化克制", "环境空镜", "篇幅"):
            self.assertIn(kw, FORMAT_SELF_CHECK_RULE)


class WebFailoverTest(unittest.TestCase):

    def _make(self):
        from workflows.workflow_generation import GenerationMixin

        class Fake(GenerationMixin):
            def __init__(self):
                self.web_calls = 0
                self.api_calls = 0
                self.web_error = RuntimeError(
                    "找不到 DeepSeek 输入框。DeepSeek 前端可能改版")

            def _generate_web(self, *a, **k):
                self.web_calls += 1
                raise self.web_error

            def _generate_api(self, *a, **k):
                self.api_calls += 1
                return "api-story"

        return Fake()

    def _patch(self):
        return [
            mock.patch("config.LLM_MODE", "web"),
            mock.patch("config.story.WEB_FAILOVER_TO_API", True),
            mock.patch("config.story.WEB_FAILOVER_MAX_CONSECUTIVE", 2),
        ]

    def test_single_failure_falls_back_to_api(self):
        g = self._make()
        with self._patch()[0], self._patch()[1], self._patch()[2]:
            story = g.generate_story("题目", "回答")
        self.assertEqual(story, "api-story")
        self.assertEqual(g.web_calls, 1)
        self.assertEqual(g.api_calls, 1)

    def test_circuit_breaker_skips_web_after_n_failures(self):
        g = self._make()
        with self._patch()[0], self._patch()[1], self._patch()[2]:
            g.generate_story("题目", "回答")   # web 失败 → api
            g.generate_story("题目", "回答")   # web 失败 → api
            g.generate_story("题目", "回答")   # 断路器：跳过 web 直走 api
        self.assertEqual(g.web_calls, 2)
        self.assertEqual(g.api_calls, 3)

    def test_non_ui_error_not_caught(self):
        g = self._make()
        g.web_error = RuntimeError("网络超时 500")
        with self._patch()[0], self._patch()[1]:
            with self.assertRaises(RuntimeError):
                g.generate_story("题目", "回答")
        self.assertEqual(g.api_calls, 0)

    def test_success_resets_breaker_count(self):
        from workflows.workflow_generation import GenerationMixin

        class Fake(GenerationMixin):
            def __init__(self):
                self.web_calls = 0
                self.api_calls = 0
                self.web_results = iter([
                    RuntimeError("找不到 DeepSeek 输入框"),  # 1 失败 → api
                    "ok-web",                                # 2 成功，计数清零
                    RuntimeError("找不到 DeepSeek 输入框"),  # 3 失败 → api
                    "ok-web",                                # 4 成功
                ])

            def _generate_web(self, *a, **k):
                self.web_calls += 1
                r = next(self.web_results)
                if isinstance(r, Exception):
                    raise r
                return r

            def _generate_api(self, *a, **k):
                self.api_calls += 1
                return "api-story"

        g = Fake()
        with self._patch()[0], self._patch()[1], self._patch()[2]:
            g.generate_story("题目", "回答")  # 失败 → api
            g.generate_story("题目", "回答")  # 成功，计数清零
            g.generate_story("题目", "回答")  # 再次失败 → api（计数从 1 起）
        self.assertEqual(g.web_calls, 3)
        self.assertEqual(g.api_calls, 2)


class AdaptiveThresholdTest(unittest.TestCase):

    def test_length_steps_and_floor(self):
        from workflows.zhihu import ZhihuWorkflow
        with mock.patch("config.story.EXTRACT_ADAPTIVE_RELAX", True),              mock.patch("config.story.MIN_ANSWER_LENGTH", 500),              mock.patch("config.story.EXTRACT_LENGTH_FACTORS",
                        (1.0, 0.8, 0.6)),              mock.patch("config.story.EXTRACT_MIN_LENGTH_FLOOR", 250):
            self.assertEqual(ZhihuWorkflow._adaptive_min_length(0), 500)
            self.assertEqual(ZhihuWorkflow._adaptive_min_length(1), 400)
            self.assertEqual(ZhihuWorkflow._adaptive_min_length(3), 300)

    def test_disabled_keeps_base(self):
        from workflows.zhihu import ZhihuWorkflow
        with mock.patch("config.story.EXTRACT_ADAPTIVE_RELAX", False),              mock.patch("config.story.MIN_ANSWER_LENGTH", 500):
            self.assertEqual(ZhihuWorkflow._adaptive_min_length(9), 500)
        with mock.patch("config.story.EXTRACT_ADAPTIVE_RELAX", False),              mock.patch("config.story.MATERIAL_MIN_LIKES", 30):
            self.assertEqual(ZhihuWorkflow._adaptive_min_likes(9), 30)

    def test_likes_steps_and_floor(self):
        from workflows.zhihu import ZhihuWorkflow
        with mock.patch("config.story.EXTRACT_ADAPTIVE_RELAX", True),              mock.patch("config.story.MATERIAL_MIN_LIKES", 200),              mock.patch("config.story.EXTRACT_LIKES_FACTORS",
                        (1.0, 0.6, 0.3)),              mock.patch("config.story.EXTRACT_MIN_LIKES_FLOOR", 20):
            self.assertEqual(ZhihuWorkflow._adaptive_min_likes(0), 200)
            self.assertEqual(ZhihuWorkflow._adaptive_min_likes(1), 120)
            self.assertEqual(ZhihuWorkflow._adaptive_min_likes(2), 60)


class EngagementRateTest(unittest.TestCase):

    def test_rates_per_day(self):
        from webui.published import _engagement_rates
        today = datetime.date.today()
        pd = (today - datetime.timedelta(days=3)).isoformat()
        r = _engagement_rates({
            "publish_date": pd, "reads": 300, "likes": 9,
            "comments": 1, "collects": 1, "favors": 0,
        })
        self.assertEqual(r["likes_per_day"], 3.0)
        self.assertEqual(r["reads_per_day"], 100.0)
        self.assertAlmostEqual(r["engagement_per_day"], round(14.5 / 3, 2))

    def test_bad_date_gives_zero(self):
        from webui.published import _engagement_rates
        r = _engagement_rates({"publish_date": "", "likes": 9})
        self.assertEqual(r["likes_per_day"], 0.0)
        self.assertEqual(r["engagement_per_day"], 0.0)


if __name__ == "__main__":
    unittest.main()
