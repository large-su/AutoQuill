# ============================================================
# tests/test_generate_retry.py — 故事生成「带反馈重试」回归测试
#
# 验证 WorkflowBase.generate_story_with_retry 的核心行为：
#   - 不合格（无输出 / 过短 / 格式不合规）时自动重试
#   - 重试时把上一版失败原因作为 feedback 注入 prompt（针对性修正）
#   - 达到 STORY_GENERATE_MAX_ATTEMPTS 仍未达标时返回最高分版本 + ok=False
#   - build_story_prompt 会把 feedback 渲染成「必须修正」段
#
# 全部用 mock 隔离 generate_story，不走真实 LLM / 浏览器。
# ============================================================

import unittest
from unittest import mock

from config import story as story_config


def _valid_story():
    """构造一段能通过 validate_story_format 的故事（≥6 章、≥4000 字、短段）。"""
    line = "这是第{ch}章的叙事句子，讲述一个具体的生活片段与人物内心抉择。"
    paras = []
    for ch in range(1, 9):  # 8 章
        paras.append(f"## **{ch}**")
        paras.append("")
        for _ in range(30):  # 每章 30 句短段
            paras.append(line.format(ch=ch))
    return "\n".join(paras)


def _invalid_story():
    """一段过短的故事（<500 字，触发字数过短分支）。"""
    return "这是一个不合规的故事，字数太少且没有章节标题。"


def _long_invalid_story():
    """一段长度够（>=500 字）但格式不合规的故事（章节 < 6、总字数 < 4000）。

    会触发「格式不合规」分支（而非字数过短分支），用于测格式反馈内容。
    """
    line = "这是第{ch}章的一句话描述，交代场景与人物状态。"
    paras = []
    for ch in range(1, 4):  # 3 章（< 6，不合规）
        paras.append(f"## **{ch}**")
        paras.append("")
        for _ in range(40):
            paras.append(line.format(ch=ch))
    return "\n".join(paras)


class TestGenerateStoryWithRetry(unittest.TestCase):
    """generate_story_with_retry 的反馈重试行为。"""

    def setUp(self):
        # 强制 API 模式，跳过 Web 通道的清洗/修复（避免污染手工构造的文本）
        self._orig_mode = story_config  # config.story 无 LLM_MODE；改 config 顶层
        import config
        self._orig_llm_mode = config.LLM_MODE
        config.LLM_MODE = "api"
        from workflows.base import WorkflowBase
        self.wf = WorkflowBase()

    def tearDown(self):
        import config
        config.LLM_MODE = self._orig_llm_mode

    def test_succeeds_on_second_attempt_with_feedback(self):
        """首版格式不合规 → 重试版合规 → 返回合规版，且重试带了反馈。"""
        calls = []

        def fake_gen(title, answer, feedback=None):
            calls.append(feedback)
            # 第 1 次格式不合规，第 2 次合规
            return _long_invalid_story() if len(calls) == 1 else _valid_story()

        with mock.patch.object(self.wf, "generate_story",
                               side_effect=fake_gen):
            story, ok = self.wf.generate_story_with_retry("题", "参考")

        self.assertTrue(ok)
        self.assertEqual(story, _valid_story())
        # 第 1 次无反馈；第 2 次带上一版失败原因（格式分支的内容）
        self.assertIsNone(calls[0])
        self.assertIsNotNone(calls[1])
        self.assertIn("章节", calls[1])

    def test_short_story_feedback_mentions_length(self):
        """过短故事的重试反馈应指出字数限制。"""
        calls = []

        def fake_gen(title, answer, feedback=None):
            calls.append(feedback)
            return _invalid_story()

        with mock.patch.object(self.wf, "generate_story",
                               side_effect=fake_gen):
            story, ok = self.wf.generate_story_with_retry(
                "题", "参考", max_attempts=2)
        self.assertFalse(ok)
        self.assertIn("字数过短", calls[1])

    def test_exhaust_returns_best_and_false_all_with_feedback(self):
        """多版都不合规 → 返回最高分版本 + ok=False，且每轮重试带反馈。"""
        calls = []

        def fake_gen(title, answer, feedback=None):
            calls.append(feedback)
            return _long_invalid_story()

        with mock.patch.object(self.wf, "generate_story",
                               side_effect=fake_gen):
            story, ok = self.wf.generate_story_with_retry(
                "题", "参考", max_attempts=3)

        self.assertFalse(ok)
        self.assertEqual(story, _long_invalid_story())
        self.assertEqual(len(calls), 3)
        # 第 1 次无反馈，第 2/3 次都带反馈
        self.assertIsNone(calls[0])
        self.assertIsNotNone(calls[1])
        self.assertIsNotNone(calls[2])

    def test_no_output_returns_none_and_false(self):
        """模型无输出 → 返回 (None, False)，不崩溃。"""
        with mock.patch.object(self.wf, "generate_story", return_value=None):
            story, ok = self.wf.generate_story_with_retry(
                "题", "参考", max_attempts=2)
        self.assertIsNone(story)
        self.assertFalse(ok)

    def test_first_attempt_valid_returns_immediately(self):
        """首版即合规 → 不重试，直接返回。"""
        with mock.patch.object(self.wf, "generate_story",
                               side_effect=lambda *a, **k: _valid_story()) as m:
            story, ok = self.wf.generate_story_with_retry(
                "题", "参考", max_attempts=3)
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 1)

    def test_max_attempts_default_is_three(self):
        """重试上限默认 3（含首次），可配置。"""
        self.assertEqual(story_config.STORY_GENERATE_MAX_ATTEMPTS, 3)


class TestRetryFeedbackRendering(unittest.TestCase):
    """build_story_prompt 必须把反馈渲染成「必须修正」段。"""

    def test_build_prompt_appends_feedback_section(self):
        from story_prompt import build_story_prompt
        user_message, _mode = build_story_prompt(
            "测试问题?", reference_answer="参考文本",
            feedback=["字数过短（仅 1234 字，要求至少 500 字）"])
        self.assertIn("上一版不符合发布要求", user_message)
        self.assertIn("字数过短（仅 1234 字", user_message)
        self.assertIn("不少于 6 节", user_message)
        self.assertIn("不少于 4000 字", user_message)

    def test_build_prompt_without_feedback_is_unchanged(self):
        from story_prompt import build_story_prompt
        user_message, _mode = build_story_prompt(
            "测试问题?", reference_answer="参考文本")
        self.assertNotIn("上一版不符合发布要求", user_message)

    def test_format_format_failure_renders_dict(self):
        from workflows.base import WorkflowBase
        fmt = WorkflowBase._format_format_failure(
            {"章节": "5个(-1)", "字数": "3456字(-2)"})
        self.assertIn("章节", fmt)
        self.assertIn("5个(-1)", fmt)
        self.assertIn("3456字(-2)", fmt)


if __name__ == "__main__":
    unittest.main()
