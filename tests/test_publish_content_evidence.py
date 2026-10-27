# -*- coding: utf-8 -*-
"""Behavioral checks for optional draft-content provenance on publish results."""
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

    def test_empty_or_capture_error_does_not_block_publish(self):
        for html, raises in (("", False), ("<p>正文</p>", True)):
            with self.subTest(html=html, raises=raises):
                browser = _EvidenceBrowser([_card(0, 111)], draft_html=html)
                browser.raise_first_read = raises

                result = browser.publish_draft()

                self.assertTrue(result["ok"])
                self.assertNotIn("draft_content_fingerprint", result)

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
