"""Ending closure switch is isolated, reversible, and provenance-safe."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from story_prompt import (
    DEAI_STYLE_RULE,
    COMPLETE_ENDING_STYLE_RULE,
    READER_EXPERIENCE_RULE,
    build_clean_prompt,
    build_story_prompt,
)
from core import evolution


class EndingClosureTest(unittest.TestCase):
    TITLE = "社区修补摊因租约到期，裁缝和年轻人协商继续经营，完整结局"
    REFERENCE = "她把账本推过去，没有先解释。"
    RECIPE = {"genre": "现实", "hook": "租约到期"}

    def _prompt(self, enabled, material="sample"):
        with patch("config.story.STORY_MATERIAL_MODE", material), \
             patch("config.story.STORY_ENDING_CLOSURE_ENABLE", enabled), \
             patch("config.story.READER_EXPERIENCE_ENABLE", True):
            return build_story_prompt(self.TITLE, self.REFERENCE,
                                      recipe=self.RECIPE, opening_auto=False)

    def test_classic_switch_is_one_reversible_block(self):
        for material in ("sample", "reference", "recipe", "recipe_and_reference"):
            with self.subTest(material=material):
                learned, mode = self._prompt(True, material)
                legacy, _ = self._prompt(False, material)
                self.assertEqual(learned.count(COMPLETE_ENDING_STYLE_RULE), 1)
                self.assertNotIn(DEAI_STYLE_RULE, learned)
                self.assertEqual(legacy.count(DEAI_STYLE_RULE), 1)
                self.assertNotIn(COMPLETE_ENDING_STYLE_RULE, legacy)
                self.assertEqual(
                    learned.replace(COMPLETE_ENDING_STYLE_RULE, ""),
                    legacy.replace(DEAI_STYLE_RULE, ""),
                )
                self.assertEqual(learned.count(READER_EXPERIENCE_RULE), 1)

    def test_retry_feedback_stays_after_ending_rule(self):
        with patch("config.story.STORY_ENDING_CLOSURE_ENABLE", True):
            message, _ = build_story_prompt(
                self.TITLE, self.REFERENCE, recipe=self.RECIPE,
                opening_auto=False, feedback="上一版结尾没有交代租约结果")
        self.assertGreater(message.rfind("上一版结尾"),
                           message.rfind(COMPLETE_ENDING_STYLE_RULE))

    def test_only_contradictory_ending_instruction_changes(self):
        old_line = '- 结尾停在画面或悬念上，禁止"这件事让我明白……"式升华收尾'
        new_line = ('- 结尾按题意兑现主线；题目要求完整结局时，不新增未解释的谜团或续篇悬念。'
                    '可用已经发生的动作或画面收束，禁止"这件事让我明白……"式升华收尾')
        self.assertIn(old_line, DEAI_STYLE_RULE)
        self.assertNotIn(old_line, COMPLETE_ENDING_STYLE_RULE)
        self.assertEqual(COMPLETE_ENDING_STYLE_RULE,
                         DEAI_STYLE_RULE.replace(old_line, new_line))
        message, _ = self._prompt(True)
        self.assertIn(new_line, message)
        self.assertNotIn(old_line, message)

    def test_clean_prompt_is_byte_for_byte_independent(self):
        with patch("config.story.STORY_ENDING_CLOSURE_ENABLE", False):
            old, old_mode = build_clean_prompt(self.TITLE, self.REFERENCE)
        with patch("config.story.STORY_ENDING_CLOSURE_ENABLE", True):
            new, new_mode = build_clean_prompt(self.TITLE, self.REFERENCE)
        self.assertEqual((old, old_mode), (new, new_mode))

    def test_generation_settings_record_switch_without_private_fields(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("core.paths.data", lambda *p: str(Path(folder, *p))), \
             patch("core.paths.program", lambda *p: str(Path(folder, *p))):
            with patch("config.story.STORY_ENDING_CLOSURE_ENABLE", False):
                old = evolution.record_generation("old.md", "旧正文", None)
            with patch("config.story.STORY_ENDING_CLOSURE_ENABLE", True):
                new = evolution.record_generation("new.md", "新正文", None)
            self.assertNotEqual(old["fingerprint"], new["fingerprint"])
            data = json.loads(Path(folder, "data", "state", "evolution.json").read_text(encoding="utf-8"))
            self.assertEqual([row["settings"]["STORY_ENDING_CLOSURE_ENABLE"]
                              for row in data["generations"]], [False, True])
            self.assertNotIn("api_key", data["generations"][1]["settings"])


if __name__ == "__main__":
    unittest.main()
