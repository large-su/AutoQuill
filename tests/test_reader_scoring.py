# -*- coding: utf-8 -*-
"""Reader scoring preview tests (sampling and score result compatibility)."""
import unittest
from unittest.mock import patch

import config
import config.story as story_config
import story_scoring


class ScorePreviewTest(unittest.TestCase):
    def _budgets(self, head=40, middle=60, tail=40):
        return patch.multiple(
            story_config,
            SCORE_STORY_HEAD_CHARS=head,
            SCORE_STORY_MIDDLE_CHARS=middle,
            SCORE_STORY_TAIL_CHARS=tail,
            create=True,
        )

    def test_middle_windows_are_present_in_order_without_duplicate_body(self):
        story = "H" * 200 + "A" * 500 + "M" * 500 + "T" * 500 + "Z" * 200
        with self._budgets():
            preview = story_scoring.build_score_preview(story)
        self.assertIn("中段片段", preview)
        self.assertGreaterEqual(preview.count("中段片段"), 2)
        self.assertLessEqual(sum(preview.count(ch) for ch in "HAMTZ"), 40 + 60 + 40)
        self.assertLess(preview.index("开头片段"), preview.index("中段片段"))
        self.assertLess(preview.index("中段片段"), preview.rindex("中段片段"))
        self.assertLess(preview.rindex("中段片段"), preview.index("结尾片段"))
        self.assertIn("省略", preview)

    def test_short_story_is_unchanged(self):
        story = "短故事🙂，完整保留。"
        with self._budgets(5, 5, 5):
            self.assertEqual(story_scoring.build_score_preview(story), story)

    def test_zero_or_negative_budget_does_not_leak_full_story(self):
        story = "完整正文不应被悄悄放出" * 10
        with self._budgets(0, 0, 0):
            preview = story_scoring.build_score_preview(story)
        self.assertIn("预算为 0", preview)
        self.assertNotIn(story, preview)
        with self._budgets(-1, -2, -3):
            preview = story_scoring.build_score_preview(story)
        self.assertIn("预算为 0", preview)
        self.assertNotIn(story, preview)

    def test_single_middle_char_and_zero_head_tail(self):
        story = "A" * 100 + "M" + "Z" * 100
        with self._budgets(0, 1, 0):
            preview = story_scoring.build_score_preview(story)
        self.assertIn("M", preview)
        self.assertNotIn("A" * 100, preview)
        self.assertNotIn("Z" * 100, preview)

    def test_boundary_story_budget_plus_one(self):
        story = "头" * 40 + "中" * 60 + "尾" * 40 + "溢"
        with self._budgets():
            preview = story_scoring.build_score_preview(story)
        self.assertIn("头", preview)
        self.assertIn("尾", preview)
        self.assertIn("省略", preview)

    def test_unique_unicode_body_is_ordered_and_within_budget(self):
        story = "".join(chr(0x1000 + i) for i in range(1000))
        with self._budgets():
            preview = story_scoring.build_score_preview(story)
        sampled = [ch for ch in preview if ch in story]
        self.assertEqual(len(sampled), len(set(sampled)))
        positions = [story.index(ch) for ch in sampled]
        self.assertEqual(positions, sorted(positions))
        self.assertLessEqual(len(sampled), 140)


class ScoreResultTest(unittest.TestCase):
    def test_legacy_score_field_names_are_mapped_and_prompt_is_single_call(self):
        old_mode = config.LLM_MODE
        config.LLM_MODE = "api"
        original_resolve = story_scoring.resolve_kb_llm_config
        original_call = story_scoring.call_llm_non_streaming
        try:
            story_scoring.resolve_kb_llm_config = lambda: ("key", "url", "model", {})
            calls = []
            def fake_call(*args, **kwargs):
                calls.append(args[0])
                return (
                    '[{"index":1,"hook":8,"plot":7,"emotion":6,"authenticity":5,'
                    '"ending":4,"format":3,"natural":9,"total":33,"comment":"ok"}]',
                    0.1, None)
            story_scoring.call_llm_non_streaming = fake_call
            story = "开" * 200 + "中" * 500 + "结" * 200
            question = '这是一道需要保留上下文的故事题目' * 4 + '请保持第三人称且结局完整'
            with patch.multiple(story_config, SCORE_STORY_HEAD_CHARS=40,
                                SCORE_STORY_MIDDLE_CHARS=60, SCORE_STORY_TAIL_CHARS=40):
                out = story_scoring.score_stories([{"index": 1, "title": question, "story": story}])
        finally:
            story_scoring.resolve_kb_llm_config = original_resolve
            story_scoring.call_llm_non_streaming = original_call
            config.LLM_MODE = old_mode
        self.assertEqual(out[0]["score"], 33)
        self.assertEqual(out[0]["score_detail"]["情节节奏"], 7)
        self.assertEqual(out[0]["score_detail"]["真实感"], 5)
        self.assertEqual(out[0]["score_detail"]["格式体验"], 3)
        self.assertEqual(len(calls), 1)
        self.assertIn("中段片段", calls[0])
        self.assertIn("省略", calls[0])
        self.assertIn(question, calls[0])


if __name__ == "__main__":
    unittest.main()
