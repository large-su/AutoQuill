# ============================================================
# tests/test_extractors.py — 回答提取接缝（组合器决策逻辑）
#
# 用桩提取器测试 FallbackAnswerExtractor 的回退决策，
# 不触达任何 GUI/OCR 依赖。
# ============================================================

import unittest

from applications.zhihu_story.extractors import (
    AnswerExtractor,
    FallbackAnswerExtractor,
)


class StubExtractor(AnswerExtractor):
    """可编程桩：按预设结果或异常返回。"""

    def __init__(self, name, result=("", "", None), error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    def extract(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


FOOTER_WITH_LIKES = {"likes": 123, "comments": 4}
GOOD = ("标题", "长回答正文" * 100, FOOTER_WITH_LIKES)


class TestFallbackAnswerExtractor(unittest.TestCase):
    def test_primary_success_used(self):
        primary = StubExtractor("UIA", GOOD)
        fallback = StubExtractor("OCR")
        ex = FallbackAnswerExtractor(primary, fallback)
        self.assertEqual(ex.extract(), GOOD)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_primary_failure_falls_back(self):
        primary = StubExtractor("UIA", ("", "", None))
        fallback = StubExtractor("OCR", GOOD)
        ex = FallbackAnswerExtractor(primary, fallback)
        self.assertEqual(ex.extract(), GOOD)
        self.assertEqual(fallback.calls, 1)

    def test_primary_exception_falls_back(self):
        primary = StubExtractor("UIA", error=RuntimeError("boom"))
        fallback = StubExtractor("OCR", GOOD)
        ex = FallbackAnswerExtractor(primary, fallback)
        self.assertEqual(ex.extract(), GOOD)

    def test_likes_gate_triggers_fallback(self):
        no_likes = ("标题", "长回答正文" * 100, None)
        primary = StubExtractor("UIA", no_likes)
        fallback = StubExtractor("OCR", GOOD)
        ex = FallbackAnswerExtractor(primary, fallback, require_likes=True)
        self.assertEqual(ex.extract(), GOOD)

    def test_likes_gate_off_accepts_footerless(self):
        no_likes = ("标题", "长回答正文" * 100, None)
        fallback = StubExtractor("OCR", GOOD)
        ex = FallbackAnswerExtractor(
            StubExtractor("UIA", no_likes), fallback, require_likes=False
        )
        self.assertEqual(ex.extract(), no_likes)
        self.assertEqual(fallback.calls, 0)

    def test_likes_gate_accepts_footer_with_likes(self):
        fallback = StubExtractor("OCR")
        ex = FallbackAnswerExtractor(
            StubExtractor("UIA", GOOD), fallback, require_likes=True
        )
        self.assertEqual(ex.extract(), GOOD)
        self.assertEqual(fallback.calls, 0)

    def test_no_primary_goes_straight_to_fallback(self):
        fallback = StubExtractor("OCR", GOOD)
        ex = FallbackAnswerExtractor(None, fallback)
        self.assertEqual(ex.extract(), GOOD)
        self.assertEqual(fallback.calls, 1)

    def test_interface_contract(self):
        # 接口必须声明 extract，实现者必须返回三元组
        self.assertTrue(hasattr(AnswerExtractor, "extract"))
        with self.assertRaises(NotImplementedError):
            AnswerExtractor().extract()


if __name__ == "__main__":
    unittest.main()
