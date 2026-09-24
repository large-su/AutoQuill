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


class TopicPriorTest(unittest.TestCase):
    """选题先验（2026-09-23 复盘 P1/P3）：题型打折、求推荐类降权。

    依据：46 篇有反馈稿件的阅读/天中位——命题作文/微小说 2.0、求推荐 13.4、
    观点/讨论 28.5。题型差距远大于任何正文特征差距，所以先修选题。
    """

    def test_topic_type_classification(self):
        from core.detectors import classify_topic_type
        self.assertEqual(classify_topic_type("请以「我醒来」为开头写一篇微小说"), "命题作文")
        self.assertEqual(classify_topic_type("用十个字写一个故事？"), "命题作文")
        self.assertEqual(classify_topic_type("古代的聪明女子如何将烂牌打好？"), "观点讨论")
        self.assertEqual(classify_topic_type("有没有好看的虐文推荐？"), "求推荐")
        self.assertEqual(classify_topic_type("随便写点什么"), "其他")

    def test_composition_topics_are_discounted(self):
        """命题作文打折——是打折不是排除（选不到别的时仍可用）。"""
        from core.detectors import topic_type_multiplier
        self.assertLess(topic_type_multiplier("请以「我醒来」为开头写一篇微小说"), 0.5)
        self.assertEqual(topic_type_multiplier("有没有好看的虐文推荐？"), 1.0)
        self.assertEqual(topic_type_multiplier(""), 1.0)

    def test_switch_off_restores_old_behavior(self):
        import core.detectors as det
        with mock.patch("config.story.TOPIC_TYPE_PRIOR_ENABLE", False):
            self.assertEqual(det.topic_type_multiplier("写一篇微小说"), 1.0)

    def test_recommend_keywords_demoted_not_blocked(self):
        """「小说推荐」类从硬排除改成降权：本期第一就出在这类题下。"""
        from config.story import (STORY_DOWNWEIGHT_KEYWORDS,
                                  STORY_EXCLUDE_KEYWORDS)
        self.assertTrue(any(k in "有好看的先婚后爱这类型的小说推荐吗？"
                            for k in STORY_DOWNWEIGHT_KEYWORDS))
        self.assertFalse(any(k in "有好看的先婚后爱这类型的小说推荐吗？"
                             for k in STORY_EXCLUDE_KEYWORDS))
        # 纯书单需求仍然硬排除
        self.assertTrue(any(k in "书荒了，求书单" for k in STORY_EXCLUDE_KEYWORDS))

    def test_story_filter_keeps_recommend_questions(self):
        """硬筛选不再拦下「求推荐」类（白名单命中即可通过）。"""
        from workflows.zhihu import ZhihuWorkflow
        wf = ZhihuWorkflow.__new__(ZhihuWorkflow)      # 不建实例（无浏览器）
        qs = [{"href": "u1", "title": "有好看的先婚后爱这类型的小说推荐吗？"},
              {"href": "u2", "title": "书荒了，求书单"},
              {"href": "u3", "title": "如何评价某款手机"}]
        kept = wf._apply_story_filter(qs)
        ids = [q["href"] for q in kept]
        self.assertIn("u1", ids)          # 求推荐：放宽后保留
        self.assertNotIn("u2", ids)       # 纯书单：仍排除
        self.assertNotIn("u3", ids)       # 非故事白名单：排除

    def test_dom_score_applies_downweight(self):
        """打分：同信号下，降权题低于普通题、命题作文最低。

        关掉「题材先验」（读者数据学出来的那一项）再看，否则两条规则叠加、
        比不出题型/降权各自的效果。
        """
        from workflows.zhihu import ZhihuWorkflow
        q = {"followers": 100, "answers": 10}
        with mock.patch("config.story.TOPIC_GENRE_PRIOR_ENABLE", False):
            base = ZhihuWorkflow._dom_score(dict(q, title="你听过哪些匪夷所思的故事"))
            down = ZhihuWorkflow._dom_score(dict(q, title="有没有好看的小说推荐"))
            comp = ZhihuWorkflow._dom_score(dict(q, title="写一篇微小说"))
        self.assertLess(down, base)        # 求推荐类降权 ×0.6
        self.assertLess(comp, down)        # 命题作文再乘 ×0.4
        with mock.patch("config.story.TOPIC_TYPE_PRIOR_ENABLE", False), \
                mock.patch("config.story.STORY_DOWNWEIGHT_FACTOR", 1.0), \
                mock.patch("config.story.TOPIC_GENRE_PRIOR_ENABLE", False):
            self.assertEqual(ZhihuWorkflow._dom_score(
                dict(q, title="写一篇微小说")), base)   # 开关关掉=恢复原打分


class PriorObservationAgeTest(unittest.TestCase):
    """P5：题材先验只采纳发布满 N 天的篇目（防「用前三天票房否定题材」）。"""

    def test_young_articles_are_ignored(self):
        import tempfile, pathlib, json
        import core.feedback_loop as fb
        tmp = tempfile.TemporaryDirectory()
        perf = pathlib.Path(tmp.name) / "story_performance.jsonl"
        with mock.patch.object(fb, "_perf_path", lambda: perf), \
                mock.patch.object(fb, "seed_from_snapshots", lambda *a, **k: None):
            fb._cache.update(mtime_ns=None, at=0.0, data=None)
            rows = [
                # 3 天前发的爆款（数字漂亮但太新，不该参与先验）
                {"url": "u1", "aid": "1", "title": "追妻火葬场", "genre": "虐文/火葬场",
                 "publish_date": "2026-09-16", "observed": "2026-09-19",
                 "reads": 3000, "likes": 30, "comments": 3, "collects": 13, "favors": 0},
                # 10 天前发的一篇（可参与先验）
                {"url": "u2", "aid": "2", "title": "甜甜的恋爱", "genre": "甜文",
                 "publish_date": "2026-09-09", "observed": "2026-09-19",
                 "reads": 200, "likes": 2, "comments": 0, "collects": 1, "favors": 0},
            ]
            with open(perf, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + chr(10))
            s = fb.summarize(as_of=datetime.date(2026, 9, 19), auto_seed=False)
            genres = s.get("genres") or {}
            self.assertEqual(s["n_articles"], 1)          # 3 天那篇被跳过
            self.assertIn("甜文", genres)
            self.assertNotIn("虐文/火葬场", genres)
            # 关掉保护 → 两篇都参与（对照组，证明是这条规则在起作用）
            with mock.patch("config.story.TOPIC_GENRE_PRIOR_MIN_AGE_DAYS", 0):
                fb._cache.update(mtime_ns=None, at=0.0, data=None)
                s2 = fb.summarize(as_of=datetime.date(2026, 9, 19), auto_seed=False)
            self.assertEqual(s2["n_articles"], 2)
        tmp.cleanup()


    def test_order_prefer_large_audience(self):
        """最终选优要偏向受众大的题型（2026-09-24 日志实证的必要补丁）。

        那天流水线把一篇命题作文挑成了「最适合写故事」，题型折扣等于白打。
        """
        from core.detectors import order_prefer_large_audience as order
        items = [{"title": "怎样以「一觉醒来我变成了一只猫」为题写一篇小说？"},
                 {"title": "有没有好看的双女主小说?"},
                 {"title": "有无小说推荐呀？"}]
        out = order(items)
        self.assertEqual(out[0]["title"], "有没有好看的双女主小说?")
        self.assertEqual(len(out), 3)              # 一个都不丢，只调顺序
        self.assertEqual(out[-1]["title"], items[0]["title"])
        # 全都是命题作文 → 顺序原样（大模型的判断仍然算数）
        same = order([{"title": "写一篇微小说"}, {"title": "以「X」为开头写故事"}])
        self.assertEqual(same[0]["title"], "写一篇微小说")
        self.assertEqual(order([]), [])


class PromptSpecRegressionTest(unittest.TestCase):
    """P2/P4：篇幅口径与命名规则（2026-09-23 复盘）。"""

    def test_length_is_a_floor_not_a_target(self):
        from story_prompt import FORMAT_SELF_CHECK_RULE as R
        self.assertIn("不少于 4000 字", R)
        self.assertIn("5000-7000", R)
        self.assertNotIn("每节 500-800 字", R)

    def test_naming_rule_is_advisory(self):
        from story_prompt import DEAI_STYLE_RULE as D
        self.assertNotIn("硬性·最高优先级", D)
        self.assertIn("【建议】", D)


if __name__ == "__main__":
    unittest.main()
