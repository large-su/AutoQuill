# -*- coding: utf-8 -*-
"""Required full-body content checks and pre-click provenance on publication."""
import unittest
from unittest import mock

from core.evolution import content_fingerprint
from tests.test_automation_publish import _FakeWriteBrowser, _card


class _EvidenceBrowser(_FakeWriteBrowser):
    def __init__(self, *args, draft_html="<p>正文 <b>内容</b></p>", **kwargs):
        super().__init__(*args, **kwargs)
        self.draft_html = draft_html
        self.draft_reads = 0
        self.events = []
        self.raise_first_read = False
        self.reconcile_checks = 0

    def _click_publish_native(self):
        self.events.append("click")
        return super()._click_publish_native()

    def get_draft_content(self, question_id=None):
        self.draft_reads += 1
        self.events.append("read")
        if self.raise_first_read and self.draft_reads == 1:
            raise RuntimeError("draft unavailable")
        if self._published:
            return ""
        return self.draft_html

    def _reconcile_failure(self, target):
        self.reconcile_checks += 1
        return super()._reconcile_failure(target)


class PublishContentEvidenceTest(unittest.TestCase):
    def setUp(self):
        self._sleep = mock.patch("time.sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()

    def test_capture_precedes_click_and_success_carries_hash_only(self):
        browser = _EvidenceBrowser([_card(0, 111)])

        result = browser.publish_draft()

        self.assertTrue(result["ok"])
        self.assertEqual(browser.events[:2], ["read", "click"])
        self.assertEqual(result["draft_content_fingerprint"],
                         content_fingerprint("<p>正文 <b>内容</b></p>"))
        self.assertNotIn("正文", repr(result))

    def test_empty_or_capture_error_blocks_publish_before_click(self):
        for html, raises in (("", False), ("<p>正文</p>", True)):
            with self.subTest(html=html, raises=raises):
                browser = _EvidenceBrowser([_card(0, 111)], draft_html=html)
                browser.raise_first_read = raises

                result = browser.publish_draft()

                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "content_unavailable")
                self.assertTrue(result["certain"])
                self.assertNotIn("click", browser.events)
                self.assertNotIn("draft_content_fingerprint", result)
                self.assertEqual(browser.reconcile_checks, 0)

    def test_polluted_html_is_rejected_before_click_at_any_position(self):
        long_body = "<p>" + ("这是已经写好的正文。" * 120) + "</p>"
        cases = {
            "opening": "&lt;analysis&gt;内部推理&lt;/analysis&gt;<p>正文开头</p>",
            "middle": "<p>正文开头</p>&lt;analysis&gt;中途推理&lt;/analysis&gt;"
                      "<p>正文结尾</p>",
            "trailing_menu": long_body +
                             "<p>还需要我帮你写推荐语？</p>"
                             "<p>好的，帮我写一下</p>",
        }
        for label, html in cases.items():
            with self.subTest(label=label):
                browser = _EvidenceBrowser([_card(0, 111)], draft_html=html)

                result = browser.publish_draft()

                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "content_rejected")
                self.assertTrue(result["certain"])
                self.assertNotIn("click", browser.events)
                self.assertEqual(browser.reconcile_checks, 0)

    def test_dry_run_does_not_read_for_fingerprint(self):
        browser = _EvidenceBrowser([_card(0, 111)])

        result = browser.publish_draft(dry_run=True)

        self.assertEqual(result["reason"], "dry_run")
        self.assertEqual(browser.draft_reads, 0)
        self.assertNotIn("draft_content_fingerprint", result)

    def test_user_cancellation_during_capture_never_reaches_publish_click(self):
        from web_drivers.browser_pool import WorkflowCancelled
        browser = _EvidenceBrowser([_card(0, 111)])
        with mock.patch.object(browser, 'get_draft_content', side_effect=WorkflowCancelled()):
            with self.assertRaises(WorkflowCancelled):
                browser.publish_draft()
        self.assertNotIn('click', browser.events)


if __name__ == "__main__":
    unittest.main()
