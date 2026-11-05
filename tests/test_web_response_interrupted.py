"""Offline regression coverage for interrupted web-model responses."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from web_drivers.aizex import AizexDriver
from web_drivers.base import WebLLMDriver
from web_drivers.browser_pool import WebResponseInterrupted


class AizexInterruptedDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from applications.zhihu_story.browser_utils import EDGE_PATH

        if not EDGE_PATH:
            raise unittest.SkipTest("Edge not installed")
        from playwright.sync_api import sync_playwright

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=EDGE_PATH, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_external_interrupt_alert_wins_over_partial_reply_and_copy_button(self):
        context = self.browser.new_context()
        page = context.new_page()
        page.set_content(
            """
            <main style='width:800px;height:600px'>
              <div data-message-author-role='assistant'>
                <div class='markdown'><h2>第一章</h2><p>已收到的部分正文</p>
                  <button data-testid='copy-turn-action-button'
                          aria-label='复制回复'>复制</button>
                </div>
              </div>
              <div role='alert'>本轮响应中断，已保留接收到的内容。可以重新生成一版。</div>
            </main>
            """
        )
        driver = AizexDriver({"url": "https://aizex.test"})
        driver._page = page
        driver._browser = SimpleNamespace(context=context)
        driver._mark_reply_anchor()
        # The DOM is now the post-send state; the assistant node and toolbar
        # are both present while the interruption notice is outside the message.
        page.evaluate(
            """() => {
              document.querySelector('[data-message-author-role]').remove();
              const answer = document.createElement('div');
              answer.dataset.messageAuthorRole = 'assistant';
              answer.innerHTML = `<div class="markdown"><h2>第一章</h2><p>已收到的部分正文</p>
                <button data-testid="copy-turn-action-button" aria-label="复制回复">复制</button></div>`;
              document.querySelector('main').prepend(answer);
            }"""
        )
        with tempfile.TemporaryDirectory() as data_root, mock.patch(
            "core.paths.DATA_ROOT", data_root
        ):
            with self.assertRaises(WebResponseInterrupted):
                driver.poll_generation()

            self.assertNotEqual(driver.active_generation_status(), {"state": "completed"})
            reports = list(Path(data_root, "logs", "web_model_failures").glob("*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["message"], "本轮响应中断")
            self.assertIn("已收到的部分正文", report["partial"])
            self.assertIn("第一章", report["partial"])
        context.close()

    def test_same_phrase_inside_story_body_is_not_an_interruption(self):
        context = self.browser.new_context()
        page = context.new_page()
        page.set_content(
            """
            <main style='width:800px;height:600px'>
              <div data-message-author-role='assistant'>
                <div class='markdown'><p>角色说：本轮响应中断，已保留接收到的内容。</p>
                  <button data-testid='copy-turn-action-button'
                          aria-label='复制回复'>复制</button>
                </div>
              </div>
            </main>
            """
        )
        driver = AizexDriver({"url": "https://aizex.test"})
        driver._page = page
        driver._browser = SimpleNamespace(context=context)
        driver._mark_reply_anchor()
        page.evaluate(
            """() => {
              const old = document.querySelector('[data-message-author-role]');
              const answer = old.cloneNode(true);
              old.remove();
              document.querySelector('main').append(answer);
            }"""
        )
        facts = driver._generation_probe()
        self.assertIn("本轮响应中断", facts["text"])
        context.close()


class _RetryDriver(WebLLMDriver):
    provider_name = "offline-test"

    def __init__(self, outcomes):
        super().__init__({"model": "fixed-model", "url": "https://offline.test"})
        self.outcomes = iter(outcomes)
        self.calls = []

    def _generate_once(self, prompt, reuse_session=True):
        self.calls.append((prompt, reuse_session, self.config["model"]))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class WebLLMRetryTests(unittest.TestCase):
    def test_interruption_retries_once_with_same_prompt_and_model(self):
        driver = _RetryDriver([WebResponseInterrupted("first"), "answer"])
        self.assertEqual(driver.generate("same prompt"), "answer")
        self.assertEqual(
            driver.calls,
            [("same prompt", True, "fixed-model"),
             ("same prompt", True, "fixed-model")],
        )
        self.assertTrue(driver._session_broken)

    def test_second_interruption_is_transparent_and_does_not_loop(self):
        driver = _RetryDriver([
            WebResponseInterrupted("first"), WebResponseInterrupted("second")
        ])
        with self.assertRaisesRegex(WebResponseInterrupted, "second"):
            driver.generate("same prompt")
        self.assertEqual(len(driver.calls), 2)
        self.assertEqual([call[0] for call in driver.calls], ["same prompt"] * 2)


if __name__ == "__main__":
    unittest.main()
