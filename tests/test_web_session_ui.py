"""Offline DOM regressions for send confirmation and Kimi availability."""
import itertools
import unittest
from types import SimpleNamespace
from unittest import mock

from config import WEB_DRIVERS
from web_drivers.browser_pool import WebModelUnavailable
from web_drivers.kimi import KimiDriver


class WebSessionUITest(unittest.TestCase):
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
        cls.browser.close(); cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(); self.page = self.context.new_page()
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>"))
        self.page.goto("https://www.kimi.com/")
        self.page.set_content("""
          <div class='chat-input-editor' contenteditable='true'>提示词</div>
          <button class='send-button-container'>发送</button>
          <div class='segment-content'><div class='segment-content-box'>旧答</div><button class='copy'>复制</button></div>
        """)
        self.driver = KimiDriver(WEB_DRIVERS["Kimi"]); self.driver._page = self.page
        self.driver._browser = SimpleNamespace(context=self.context)
        self.driver.reply_selectors = (".segment-content-box",); self.driver.copy_selectors = (".copy",)
        self.driver.copy_owner_selector = ".segment-content"
        self.account = mock.patch("web_drivers.account_status.inspect_account", return_value={"status":"ready"}); self.account.start()

    def tearDown(self): self.account.stop(); self.context.close()

    def test_send_rejects_when_input_stays_and_no_new_reply_or_stop(self):
        self.page.locator(".send-button-container").evaluate("e=>{e.disabled=true}")
        with self.assertRaisesRegex(RuntimeError, "发送按钮点击未确认"):
            self.driver.send()

    def test_kimi_busy_dialog_is_unavailable_but_body_text_is_not(self):
        self.page.set_content("<div class='chat-input-editor' contenteditable='true'></div><p>和Kimi聊天的人太多了</p>")
        self.driver._safe_evaluate = lambda *a, **k: ""
        self.assertIsNotNone(self.driver._raise_if_logged_out())
        self.page.set_content("<div class='chat-input-editor' contenteditable='true'></div><p>和Kimi聊天的人太多了，订阅会员可进入优先队列</p>")
        self.assertIsNotNone(self.driver._raise_if_logged_out())
        self.page.set_content("<div class='chat-input-editor' contenteditable='true'></div><div data-testid='confirm-dialog' class='modal-container'>和Kimi聊天的人太多了，订阅会员可进入优先队列</div>")
        self.driver._safe_evaluate = lambda js, *args, **kwargs: self.page.evaluate(js, list(args))
        with self.assertRaises(WebModelUnavailable): self.driver._raise_if_logged_out()

    def test_copy_only_uses_current_reply_owner(self):
        self.page.set_content("""
          <div class='segment-content old-owner'><div class='segment-content-box'>旧答</div><button class='copy'>旧复制</button></div>
        """)
        self.driver._mark_reply_anchor()
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend', '<div class="segment-content new-owner"><div class="segment-content-box">新答</div><button class="copy">复制</button></div>')""")
        self.page.locator(".old-owner .copy").evaluate("e=>e.onclick=()=>navigator.clipboard.writeText('old-raw')")
        self.page.locator(".new-owner .copy").evaluate("e=>e.onclick=()=>navigator.clipboard.writeText('new-raw')")
        self.assertEqual(self.driver._copy_current_reply(), "new-raw")
        self.assertTrue(self.page.evaluate("() => navigator.clipboard.writeText !== undefined"))

    def test_session_id_rejects_wrong_host_and_short_path(self):
        from web_drivers.session_ui import detect_session_id
        self.assertIsNone(detect_session_id("Kimi", "https://evil.example/chat/abcdef"))
        self.assertIsNone(detect_session_id("Kimi", "https://www.kimi.com/chat/a"))

    def test_session_id_rejects_kimi_provisional_id(self):
        from web_drivers.session_ui import detect_session_id
        provisional = "p1234567-1234-1234-1234-123456789abc"
        self.assertIsNone(detect_session_id("Kimi", "https://www.kimi.com/chat/" + provisional))

    def test_session_id_accepts_exact_provider_route(self):
        from web_drivers.session_ui import detect_session_id
        self.assertEqual(detect_session_id("Qianwen", "https://www.qianwen.com/chat/abcDEF_123"), "abcDEF_123")

    def test_chatglm_session_id_requires_exact_hex_query(self):
        from web_drivers.session_ui import detect_session_id
        self.assertEqual(detect_session_id("ChatGLM", "https://chatglm.cn/?cid=" + "a" * 24), "a" * 24)
        self.assertIsNone(detect_session_id("ChatGLM", "https://chatglm.cn/?cid=not-hex"))

    def test_prose_keeps_dom_paragraphs_and_json_uses_raw_copy(self):
        self.driver._completed = True
        self.driver._copy_current_reply = lambda: "第一段\n第二段"
        self.driver._new_reply_text = lambda: "第一段\n\n第二段"
        self.assertEqual(self.driver.read_result(), "第一段\n\n第二段")
        self.driver._copy_current_reply = lambda: '{"ok":true}'
        self.driver._new_reply_text = lambda: '{“ok”:true}'
        self.assertEqual(self.driver.read_result(), '{"ok":true}')


if __name__ == "__main__": unittest.main()
