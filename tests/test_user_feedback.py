# -*- coding: utf-8 -*-
"""core.user_feedback 记录/读取意见反馈的行为单测。"""
import os
import shutil
import unittest
from unittest import mock

from core import user_feedback

# 临时目录建在工作区根下（沙箱禁止写系统 %TEMP%，且 tempfile 创建
# 的目录也会被沙箱拦截写入，故用固定工作区路径 + os.makedirs）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_DIR = os.path.join(_ROOT, "_tmp_fb_test")


class UserFeedbackTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
        os.makedirs(_TMP_DIR, exist_ok=True)
        self.patch = mock.patch.object(
            user_feedback, "FEEDBACK_FILE",
            os.path.join(_TMP_DIR, "feedback.md"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    def test_record_and_read_roundtrip(self):
        user_feedback.record("选题老是选到非故事题", category="选题")
        user_feedback.record("生成的故事缺引言", category="生成",
                             context="问题链接 q/123")
        entries = user_feedback.read()
        self.assertEqual(len(entries), 2)
        # 新→旧
        self.assertEqual(entries[0]["category"], "生成")
        self.assertEqual(entries[0]["context"], "问题链接 q/123")
        self.assertIn("缺引言", entries[0]["text"])
        self.assertEqual(entries[1]["category"], "选题")
        self.assertIn("非故事", entries[1]["text"])

    def test_empty_text_ignored(self):
        self.assertIsNone(user_feedback.record("   "))
        self.assertEqual(user_feedback.read(), [])

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(user_feedback.read(), [])

    def test_limit(self):
        for i in range(5):
            user_feedback.record(f"问题{i}", category="其他")
        self.assertEqual(len(user_feedback.read(limit=3)), 3)


if __name__ == "__main__":
    unittest.main()
