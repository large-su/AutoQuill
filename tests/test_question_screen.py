# -*- coding: utf-8 -*-
"""大模型问题池筛选单测：keep 过滤 / 最佳置顶 / 失败回退 / Web 跳过。"""
import unittest

import story_scoring
import config


def _cands():
    return [
        {"index": 1, "title": "如何学会 Python？", "answer": "教程向，不适合故事"},
        {"index": 2, "title": "有没有让你瞬间泪目的亲身经历？", "answer": "适合写故事……"},
        {"index": 3, "title": "某款手机哪个型号值得买？", "answer": "选购建议"},
        {"index": 4, "title": "你身边有没有特别狗血的故事？", "answer": "非常适合……"},
    ]


class QuestionScreenTest(unittest.TestCase):
    def tearDown(self):
        story_scoring.call_llm_non_streaming = None
        story_scoring.resolve_kb_llm_config = None

    def _patch_llm(self, reply_json, error=None, mode="api"):
        config.LLM_MODE = mode

        def fake_call(user_message, max_tokens=None, temperature=None,
                      timeout=120, api_key=None, base_url=None, model=None,
                      extra_body=None, report_usage=True):
            if error:
                return None, 1.0, error
            return reply_json, 1.0, None

        story_scoring.resolve_kb_llm_config = lambda: (
            "sk-test", "https://x", "m", {})
        story_scoring.call_llm_non_streaming = fake_call

    def test_keep_filter_best_first(self):
        reply = '{"items": [{"index":1,"keep":false,"score":0,"reason":"教程"},' \
                '{"index":2,"keep":true,"score":8,"reason":"情感经历"},' \
                '{"index":3,"keep":false,"score":0,"reason":"选购"},' \
                '{"index":4,"keep":true,"score":9,"reason":"狗血"}], "best_index":4}'
        self._patch_llm(reply)
        out = story_scoring.screen_question_pool(_cands())
        self.assertEqual([c["index"] for c in out], [4, 2], "应排除1、3且最佳4置顶")

    def test_all_rejected_returns_original(self):
        reply = '{"items": [{"index":1,"keep":false,"score":0},' \
                '{"index":2,"keep":false,"score":0},' \
                '{"index":3,"keep":false,"score":0},' \
                '{"index":4,"keep":false,"score":0}], "best_index":-1}'
        self._patch_llm(reply)
        out = story_scoring.screen_question_pool(_cands())
        self.assertEqual(len(out), 0, "全部排除应返回空（调用方会回退原候选）")

    def test_llm_error_falls_back(self):
        self._patch_llm(None, error="HTTP 500: server error")
        out = story_scoring.screen_question_pool(_cands())
        self.assertEqual(len(out), 4, "失败应原样返回")

    def test_web_mode_skips(self):
        self._patch_llm('{"items":[]}', mode="web")
        out = story_scoring.screen_question_pool(_cands())
        self.assertEqual(len(out), 4, "Web 模式应跳过筛选")

    def test_keep_best_only(self):
        reply = '{"items": [{"index":2,"keep":true,"score":8},' \
                '{"index":4,"keep":true,"score":9}], "best_index":4}'
        self._patch_llm(reply)
        out = story_scoring.screen_question_pool(_cands(), keep_best_only=True)
        self.assertEqual([c["index"] for c in out], [4])


if __name__ == "__main__":
    unittest.main()
