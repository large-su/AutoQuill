# -*- coding: utf-8 -*-
"""评分 key 回退单测：知识库 key 401 时自动改用故事生成 key 重试一次。"""
import unittest

import story_scoring
import config


class ScoringFallbackTest(unittest.TestCase):
    def tearDown(self):
        # 避免污染其他用例
        story_scoring.resolve_kb_llm_config = None  # noqa
        story_scoring.call_llm_non_streaming = None  # noqa

    def _patch(self, kb_key="sk-kb-xxxxf344", root_key="sk-root-valid",
               first_error="HTTP 401: Authentication Fails, api key invalid"):
        calls = []

        def fake_call(user_message, max_tokens, temperature=None, timeout=120,
                      api_key=None, base_url=None, model=None,
                      extra_body=None, report_usage=True):
            calls.append((api_key, base_url, model))
            if first_error and len(calls) == 1:
                return None, 1.0, first_error
            scores = '[{"index":1,"hook":8,"plot":7,"emotion":9,"authenticity":8,' \
                     '"natural":5,"ending":7,"format":8,"total":47,"comment":"ok"}]'
            return scores, 1.0, None

        story_scoring.resolve_kb_llm_config = lambda: (
            kb_key, "https://kb.example.com", "kb-model", {})
        story_scoring.call_llm_non_streaming = fake_call
        config.KB_LLM_API_KEY = kb_key
        config.KB_LLM_BASE_URL = "https://kb.example.com"
        config.KB_LLM_MODEL = "kb-model"
        config.KB_LLM_EXTRA_BODY = {}
        config.LLM_API_KEY = root_key
        config.LLM_API_BASE_URL = "https://root.example.com"
        config.LLM_API_MODEL = "root-model"
        config.LLM_API_EXTRA_BODY = {}
        return calls

    def test_kb_key_401_falls_back_to_root_key(self):
        calls = self._patch()
        out = story_scoring.score_stories(
            [{"index": 1, "title": "t", "story": "正文" * 200}])
        self.assertEqual(len(calls), 2, "应重试一次")
        self.assertEqual(calls[0][0], "sk-kb-xxxxf344")
        self.assertEqual(calls[1][0], "sk-root-valid", "第二次应用主 key")
        self.assertTrue(out and "score" in out[0], "评分应合并进结果")

    def test_non_auth_error_no_retry(self):
        calls = self._patch(first_error="HTTP 500: server error")
        story_scoring.score_stories(
            [{"index": 1, "title": "t", "story": "正文" * 200}])
        self.assertEqual(len(calls), 1, "非鉴权错误不应重试")

    def test_same_key_no_retry(self):
        calls = self._patch(kb_key="sk-same", root_key="sk-same")
        story_scoring.score_stories(
            [{"index": 1, "title": "t", "story": "正文" * 200}])
        self.assertEqual(len(calls), 1, "KB 与主 key 相同不应重复重试")


if __name__ == "__main__":
    unittest.main()
