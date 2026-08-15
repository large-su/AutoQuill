# ============================================================
# tests/test_extractors.py — 回答提取接缝（Playwright DOM 通道）
#
# 用注入的 evaluate 桩测试 PlaywrightAnswerExtractor 的提取
# 决策逻辑，不触达任何浏览器/GUI 依赖。
# ============================================================

import unittest

from applications.zhihu_story.extractors import (
    AnswerExtractor,
    PlaywrightAnswerExtractor,
)


def make_evaluate(result, error=None):
    """返回可编程 evaluate 桩：按预设结果或异常返回。"""
    def evaluate(js):
        if error is not None:
            raise error
        return result
    return evaluate


FOOTER = {"likes": 123, "comments": 4, "collects": 5, "hearts": 2,
          "publish_time": "昨天", "answer_url": "https://zhihu.com/q/1"}
GOOD = {"title": "标题", "answer": "长回答正文" * 100, "footer": FOOTER}


class TestPlaywrightAnswerExtractor(unittest.TestCase):
    def tearDown(self):
        PlaywrightAnswerExtractor._evaluate = None

    def test_success_returns_triple(self):
        PlaywrightAnswerExtractor.bind_evaluate(make_evaluate(GOOD))
        title, answer, footer = PlaywrightAnswerExtractor().extract()
        self.assertEqual(title, "标题")
        self.assertTrue(answer.startswith("长回答正文"))
        self.assertEqual(footer, FOOTER)

    def test_unbound_returns_empty(self):
        self.assertEqual(
            PlaywrightAnswerExtractor().extract(), ("", "", None))

    def test_short_answer_rejected(self):
        PlaywrightAnswerExtractor.bind_evaluate(make_evaluate(
            {"title": "标题", "answer": "短", "footer": FOOTER}))
        self.assertEqual(
            PlaywrightAnswerExtractor().extract(), ("", "", None))

    def test_missing_title_rejected(self):
        PlaywrightAnswerExtractor.bind_evaluate(make_evaluate(
            {"title": "", "answer": "长回答正文" * 100, "footer": FOOTER}))
        self.assertEqual(
            PlaywrightAnswerExtractor().extract(), ("", "", None))

    def test_exception_returns_empty(self):
        PlaywrightAnswerExtractor.bind_evaluate(
            make_evaluate(None, error=RuntimeError("boom")))
        self.assertEqual(
            PlaywrightAnswerExtractor().extract(), ("", "", None))

    def test_interface_contract(self):
        self.assertTrue(hasattr(AnswerExtractor, "extract"))
        with self.assertRaises(NotImplementedError):
            AnswerExtractor().extract()


if __name__ == "__main__":
    unittest.main()
