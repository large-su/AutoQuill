"""Regression coverage for bounded full-draft story revision."""
import unittest
from unittest.mock import Mock, patch

import config
from config import story as story_config
from core import story_editor
from workflows.base import WorkflowBase


SAFE_RISK = {"risky": False}
REVISE = {"status": "revise", "passed": False, "reason": "", "issues": [{
    "kind": "relationship", "quotes": ["甲" * 8],
    "problem": "关系变化缺少回应", "fix": "补足双方行动",
}]}
PASS = {"status": "passed", "passed": True, "reason": "", "issues": []}


class RevisionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WorkflowBase()
        self.old_mode = config.LLM_MODE
        config.LLM_MODE = "api"
        self.addCleanup(setattr, config, "LLM_MODE", self.old_mode)
        self.draft = "甲" * 600
        self.revised = "乙" * 600

    def test_initial_dispatch_preserves_legacy_call_shape(self):
        with patch.object(self.workflow, "_generate_api", return_value="ok") as api:
            self.workflow.generate_story("题", "参考", recipe={"x": 1})
        api.assert_called_once_with("题", "参考", {"x": 1}, feedback=None)

        with patch.object(self.workflow, "_generate_api", return_value="ok") as api:
            self.workflow.generate_story("题", "参考", previous_story=self.draft)
        self.assertEqual(api.call_args.kwargs["previous_story"], self.draft)

    def test_web_initial_short_form_preserves_legacy_call_shape(self):
        with patch.object(self.workflow, "_generate_web_short_form", return_value="ok") as short:
            self.workflow._generate_web("题", "参考", feedback="修正")
        short.assert_called_once_with("题", "参考", None, feedback="修正")

        with patch.object(self.workflow, "_generate_web_short_form", return_value="ok") as short:
            self.workflow._generate_web("题", "参考", feedback="修正",
                                        previous_story=self.draft)
        self.assertEqual(short.call_args.kwargs["previous_story"], self.draft)

    def test_web_failover_preserves_draft(self):
        with patch.object(story_config, "WEB_FAILOVER_MAX_CONSECUTIVE", 1), \
             patch.object(self.workflow, "_generate_api", return_value="ok") as api:
            self.workflow._web_fail_consecutive = 1
            self.workflow._generate_web_with_failover(
                "题", "参考", previous_story=self.draft)
        self.assertEqual(api.call_args.kwargs["previous_story"], self.draft)

    def test_clean_api_preserves_draft(self):
        with patch("story_generation.generate_story_clean", return_value="ok") as generate:
            self.workflow.generate_clean_story("题", "参考", previous_story=self.draft)
        self.assertEqual(generate.call_args.kwargs["previous_story"], self.draft)

    def test_editor_revision_reuses_safe_full_draft_within_budget(self):
        chains = [{"model": "initial"}, {"model": "revision"}]
        drafts = [self.draft, self.revised]

        def generate(*_args, **_kwargs):
            self.workflow._last_story_model_chain = chains.pop(0)
            return drafts.pop(0)

        with patch.object(story_config, "STORY_EDITOR_ENABLE", True), \
             patch.object(self.workflow, "generate_story", side_effect=generate) as call, \
             patch("core.originality.opening_copy_signals", return_value=SAFE_RISK), \
             patch("core.story_text.story_content_issues", return_value=[]), \
             patch("core.story_text.validate_story_format", return_value=(10, True, {})), \
             patch("core.story_editor.review_story", side_effect=[REVISE, PASS]):
            story, ok = self.workflow.generate_story_with_retry("题", "参考", max_attempts=2)
        self.assertTrue(ok)
        self.assertEqual(story, self.revised)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[1].kwargs["previous_story"], self.draft)
        self.assertEqual(self.workflow._last_story_model_chain, {"model": "revision"})

    def test_editor_off_keeps_fresh_format_retry(self):
        with patch.object(story_config, "STORY_EDITOR_ENABLE", False), \
             patch.object(self.workflow, "generate_story", side_effect=[self.draft, self.revised]) as call, \
             patch("core.originality.opening_copy_signals", return_value=SAFE_RISK), \
             patch("core.story_text.story_content_issues", return_value=[]), \
             patch("core.story_text.validate_story_format", side_effect=[(4, False, {}), (10, True, {})]):
            story, ok = self.workflow.generate_story_with_retry("题", "参考", max_attempts=2)
        self.assertTrue(ok)
        self.assertEqual(story, self.revised)
        self.assertNotIn("previous_story", call.call_args_list[1].kwargs)

    def test_safe_format_failure_is_revised_and_rechecked(self):
        chains = [{"model": "format-initial"}, {"model": "format-revision"}]

        def generate(*_args, **_kwargs):
            self.workflow._last_story_model_chain = chains.pop(0)
            return self.draft if len(chains) == 1 else self.revised

        with patch.object(story_config, "STORY_EDITOR_ENABLE", True), \
             patch.object(self.workflow, "generate_story", side_effect=generate) as call, \
             patch("core.originality.opening_copy_signals", return_value=SAFE_RISK), \
             patch("core.story_text.story_content_issues", return_value=[]), \
             patch("core.story_text.validate_story_format", side_effect=[(4, False, {"章节": "不足"}), (10, True, {})]) as validate, \
             patch("core.story_editor.review_story", return_value=PASS) as review:
            story, ok = self.workflow.generate_story_with_retry("题", "参考", max_attempts=2)
        self.assertTrue(ok)
        self.assertEqual(story, self.revised)
        self.assertEqual(call.call_args_list[1].kwargs["previous_story"], self.draft)
        self.assertEqual(validate.call_count, 2)
        review.assert_called_once_with("题", self.revised)
        self.assertEqual(self.workflow._last_story_model_chain, {"model": "format-revision"})

    def test_contaminated_or_copied_draft_is_never_reused(self):
        for gate in ("content", "copy"):
            with self.subTest(gate=gate), \
                 patch.object(story_config, "STORY_EDITOR_ENABLE", True), \
                 patch.object(self.workflow, "generate_story", side_effect=[self.draft, self.revised]) as call, \
                 patch("core.story_text.story_content_issues", return_value=[{"message": "污染"}] if gate == "content" else []), \
                 patch("core.originality.opening_copy_signals", return_value={"risky": gate == "copy", "run": 10, "snippet": "相同开头"}), \
                 patch("core.story_text.validate_story_format", return_value=(10, True, {})), \
                 patch("core.story_editor.review_story", return_value=PASS):
                self.workflow.generate_story_with_retry("题", "参考", max_attempts=2)
            self.assertNotIn("previous_story", call.call_args_list[1].kwargs)

    def test_unavailable_editor_stops_without_revision(self):
        with patch.object(story_config, "STORY_EDITOR_ENABLE", True), \
             patch.object(self.workflow, "generate_story", return_value=self.draft) as call, \
             patch("core.originality.opening_copy_signals", return_value=SAFE_RISK), \
             patch("core.story_text.story_content_issues", return_value=[]), \
             patch("core.story_text.validate_story_format", return_value=(10, True, {})), \
             patch("core.story_editor.review_story", return_value=story_editor.unavailable("无结果")):
            story, ok = self.workflow.generate_story_with_retry("题", "参考", max_attempts=3)
        self.assertFalse(ok)
        self.assertEqual(story, self.draft)
        call.assert_called_once()

    def test_clean_revision_only_after_originality_passes(self):
        with patch.object(story_config, "CLEAN_AUDIT_ENABLE", False), \
             patch.object(self.workflow, "generate_clean_story", side_effect=[self.draft, self.revised]) as call, \
             patch("core.story_text.story_content_issues", return_value=[]), \
             patch("core.story_editor.review_story", side_effect=[REVISE, PASS]):
            story, audit = self.workflow.generate_clean_with_retry("题", "参考", max_attempts=2)
        self.assertEqual(story, self.revised)
        self.assertTrue(audit["passed"])
        self.assertEqual(call.call_args_list[1].kwargs["previous_story"], self.draft)

    def test_clean_content_failure_starts_a_fresh_story(self):
        with patch.object(story_config, "CLEAN_AUDIT_ENABLE", False), \
             patch.object(self.workflow, "generate_clean_story", side_effect=[self.draft, self.revised]) as call, \
             patch("core.story_text.story_content_issues", side_effect=[[{"message": "污染"}], []]), \
             patch("core.story_editor.review_story", return_value=PASS):
            story, audit = self.workflow.generate_clean_with_retry("题", "参考", max_attempts=2)
        self.assertEqual(story, self.revised)
        self.assertTrue(audit["passed"])
        self.assertNotIn("previous_story", call.call_args_list[1].kwargs)

    def test_clean_originality_failure_starts_a_fresh_story(self):
        rejected = {"passed": False, "verdict": "洗稿", "reasons": ["结构相似"]}
        accepted = {"passed": True, "verdict": "原创", "reasons": []}
        with patch.object(story_config, "CLEAN_AUDIT_ENABLE", True), \
             patch.object(self.workflow, "generate_clean_story", side_effect=[self.draft, self.revised]) as call, \
             patch("core.story_text.story_content_issues", return_value=[]), \
             patch("core.originality.audit_originality", side_effect=[rejected, accepted]), \
             patch("core.story_editor.review_story", return_value=PASS):
            story, audit = self.workflow.generate_clean_with_retry("题", "参考", max_attempts=2)
        self.assertEqual(story, self.revised)
        self.assertTrue(audit["passed"])
        self.assertNotIn("previous_story", call.call_args_list[1].kwargs)

    def test_api_revision_prompt_contains_draft_without_reference(self):
        import story_generation
        reference = "REFERENCE-MUST-NOT-REENTER"
        feedback = "补足双方回应"
        with patch.object(config, "LLM_API_KEY", "test-key"), \
             patch("llm_client._call_llm_streaming", return_value=("修订结果", 0.01, None)) as stream:
            story_generation.generate_story("题", reference, feedback=feedback,
                                            previous_story=self.draft)
        prompt = stream.call_args.args[0]
        self.assertIn(self.draft, prompt)
        self.assertIn(feedback, prompt)
        self.assertNotIn(reference, prompt)

    def test_web_revision_prompt_contains_draft_without_reference(self):
        reference = "REFERENCE-MUST-NOT-REENTER"
        feedback = "修复时间前后矛盾"
        driver = Mock()
        driver.generate.return_value = self.revised
        with patch("web_drivers.get_driver", return_value=driver), \
             patch("story_generation._load_author_profile_or_none", return_value=None), \
             patch("core.story_model_index.web_chain", return_value={"model": "web-revision"}):
            self.workflow._generate_web_short_form(
                "题", reference, feedback=feedback, previous_story=self.draft)
        prompt = driver.generate.call_args.args[0]
        self.assertIn(self.draft, prompt)
        self.assertIn(feedback, prompt)
        self.assertNotIn(reference, prompt)


if __name__ == "__main__":
    unittest.main()
