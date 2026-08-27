# -*- coding: utf-8 -*-
"""单轮链路大模型选题筛选：_ai_pick_best 行为单测。"""
import unittest
from unittest import mock

from workflows.zhihu import ZhihuWorkflow


def _good():
    return [
        {"q": {"title": "如何学Python", "href": "u1"}, "answer": "教程" * 300,
         "footer": {"likes": 500}},
        {"q": {"title": "有没有瞬间泪目的经历", "href": "u2"}, "answer": "故事" * 300,
         "footer": {"likes": 400}},
        {"q": {"title": "求推荐一款手机", "href": "u3"}, "answer": "选购" * 300,
         "footer": {"likes": 300}},
    ]


class ZhihuAiPickTest(unittest.TestCase):
    def setUp(self):
        self.wf = ZhihuWorkflow()

    @mock.patch("config.story.QUESTION_AI_SCREEN", True)
    def test_ai_pick_best_replaces(self):
        # LLM 挑中第三条（u3），即使它点赞最低也应返回
        with mock.patch("story_scoring.screen_question_pool") as scr:
            scr.return_value = [{"index": 3, "_raw": _good()[2]}]
            best = self.wf._ai_pick_best(_good())
        self.assertIsNotNone(best)
        self.assertEqual(best["q"]["href"], "u3")

    @mock.patch("config.story.QUESTION_AI_SCREEN", False)
    def test_switch_off_returns_none(self):
        with mock.patch("story_scoring.screen_question_pool") as scr:
            best = self.wf._ai_pick_best(_good())
        scr.assert_not_called()
        self.assertIsNone(best)

    def test_llm_failure_falls_back(self):
        with mock.patch("config.story.QUESTION_AI_SCREEN", True),              mock.patch("story_scoring.screen_question_pool",
                        side_effect=RuntimeError("boom")):
            self.assertIsNone(self.wf._ai_pick_best(_good()))

    def test_single_candidate_skips(self):
        with mock.patch("story_scoring.screen_question_pool") as scr:
            self.assertIsNone(self.wf._ai_pick_best(_good()[:1]))
        scr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
