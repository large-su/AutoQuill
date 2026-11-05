"""Offline regression coverage for the configurable public-web drivers."""
import sys
import types
import unittest
from unittest import mock
from types import SimpleNamespace

from config import WEB_DRIVERS
from web_drivers import _DRIVER_REGISTRY
from web_drivers.browser_pool import WebLoginCheckError, WebLoginRequired
from web_drivers.kimi import KimiDriver
from web_drivers.qianwen import QianwenDriver
from web_drivers.yuanbao import YuanbaoDriver
from web_drivers.chatglm import ChatGLMDriver


class WebProviderDriverTests(unittest.TestCase):
    def test_all_new_providers_are_configured_and_registered(self):
        for name, module, cls in (
            ("Kimi", "web_drivers.kimi", "KimiDriver"),
            ("Qianwen", "web_drivers.qianwen", "QianwenDriver"),
            ("Yuanbao", "web_drivers.yuanbao", "YuanbaoDriver"),
            ("ChatGLM", "web_drivers.chatglm", "ChatGLMDriver"),
        ):
            self.assertEqual(_DRIVER_REGISTRY[name], (module, cls))
            self.assertTrue(WEB_DRIVERS[name]["url"].startswith("https://"))
            self.assertEqual(WEB_DRIVERS[name]["parallel_tabs"], 1)
            self.assertTrue(WEB_DRIVERS[name]["label"])

    def test_public_probe_composer_and_new_chat_selectors_are_preserved(self):
        self.assertIn("div.chat-input-editor[contenteditable='true']", KimiDriver.input_selectors)
        self.assertIn("a[data-testid='sidebar-new-chat']", KimiDriver.new_chat_selectors)
        self.assertIn("div[role='textbox'][data-slate-editor='true'][contenteditable='true']",
                      QianwenDriver.input_selectors)
        self.assertIn("[data-session-switch-target='new-chat']", QianwenDriver.new_chat_selectors)
        self.assertIn("div.ql-editor[contenteditable='true']", YuanbaoDriver.input_selectors)
        self.assertIn(".yb-new-chat-entry__item[aria-label='新建对话']",
                      YuanbaoDriver.new_chat_selectors)
        self.assertIn("textarea.scroll-display-none", ChatGLMDriver.input_selectors)

    def test_navigation_is_bound_to_driver_config_not_global_selection(self):
        class Page:
            is_closed = lambda self: False
            def goto(self, url, **_kwargs): self.url = url
            def wait_for_timeout(self, _ms): pass
        class Driver(KimiDriver):
            def _raise_if_logged_out(self, require_ready=True): return {"status": "ready"}
        driver = Driver({"url": "https://provider.invalid/own-url"})
        driver._page = Page()
        self.assertIs(driver.open_session(), driver)
        self.assertEqual(driver._page.url, "https://provider.invalid/own-url")

    def test_timeout_cannot_return_partial_text(self):
        driver = KimiDriver(WEB_DRIVERS["Kimi"])
        with mock.patch.object(driver, "_raise_if_logged_out"), mock.patch.object(driver, "_new_reply_text", return_value="partial"):
            with self.assertRaisesRegex(RuntimeError, "部分回复"):
                driver.read_result()

    def test_explicit_logged_out_is_login_required(self):
        module = types.ModuleType("web_drivers.account_status")
        module.inspect_account = lambda *args: {"status": "logged_out"}
        old = sys.modules.get("web_drivers.account_status")
        sys.modules["web_drivers.account_status"] = module
        try:
            driver = KimiDriver(WEB_DRIVERS["Kimi"])
            driver._browser = types.SimpleNamespace(context=object())
            driver._page = types.SimpleNamespace(is_closed=lambda: False)
            with self.assertRaises(WebLoginRequired):
                driver._raise_if_logged_out()
        finally:
            if old is None:
                del sys.modules["web_drivers.account_status"]
            else:
                sys.modules["web_drivers.account_status"] = old

    def test_unknown_is_not_misreported_as_logged_out(self):
        module = types.ModuleType("web_drivers.account_status")
        module.inspect_account = lambda *args: {"status": "unknown"}
        old = sys.modules.get("web_drivers.account_status")
        sys.modules["web_drivers.account_status"] = module
        try:
            driver = KimiDriver(WEB_DRIVERS["Kimi"])
            driver._browser = types.SimpleNamespace(context=object())
            driver._page = types.SimpleNamespace(is_closed=lambda: False)
            with self.assertRaises(WebLoginCheckError):
                driver._raise_if_logged_out()
        finally:
            if old is None:
                del sys.modules["web_drivers.account_status"]
            else:
                sys.modules["web_drivers.account_status"] = old


class ProviderDOMTests(unittest.TestCase):
    """Run the actual injected JS in isolated Edge, with no network or accounts."""
    @classmethod
    def setUpClass(cls):
        from applications.zhihu_story.browser_utils import EDGE_PATH
        if not EDGE_PATH:
            raise unittest.SkipTest("Edge not installed; offline DOM validation runs on Windows")
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(executable_path=EDGE_PATH, headless=True)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>"))
        self.page = self.context.new_page()
        self.page.goto("https://www.kimi.com/")
        self.driver = KimiDriver({"url": "https://www.kimi.com/", "poll_interval": 0.02, "stable_count": 2})
        self.driver.reply_selectors = (".assistant",)
        self.driver.thinking_selectors = (".thinking",)
        self.driver.stop_selectors = ("#stop",)
        self.driver.done_selectors = (".assistant .done",)
        self.driver._page = self.page
        self.driver._browser = SimpleNamespace(context=self.context)
        self.account = mock.patch("web_drivers.account_status.inspect_account", return_value={"status": "ready"})
        self.account.start()

    def tearDown(self):
        self.account.stop()
        self.context.close()

    def test_input_truncation_is_detected_before_send(self):
        self.page.set_content('<textarea oninput="this.value=this.value.slice(0,5)"></textarea>')
        self.driver.input_selectors = ('textarea',)
        with self.assertRaisesRegex(RuntimeError, "输入未完整写入"):
            self.driver.input('这段提示必须完整输入')

    def test_real_js_excludes_old_reply_and_thought_and_preserves_heading(self):
        self.page.set_content('<div class="assistant"><p>旧回答</p></div>')
        self.driver._mark_reply_anchor()
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
            '<div class="assistant"><div class="thinking">内部思考</div><h2>第一章</h2><p>本轮正文</p></div>')""")
        text = self.driver._new_reply_text()
        self.assertIn("## **第一章**", text)
        self.assertIn("本轮正文", text)
        self.assertNotIn("旧回答", text)
        self.assertNotIn("内部思考", text)

    def test_four_provider_reply_containers_extract_real_markdown(self):
        fixtures = (
            (KimiDriver, '<div class="segment-content"><div class="segment-content-box">{}</div></div>'),
            (QianwenDriver, '<div class="answer-text md-text-card">{}</div>'),
            (YuanbaoDriver, '<div data-conv-id="new"><div class="agent-chat__conv--ai__speech_show">{}</div></div>'),
            (ChatGLMDriver, '<div class="answer-content"><div class="markdown-body">{}</div></div>'),
        )
        for cls, markup in fixtures:
            with self.subTest(provider=cls.provider_name):
                self.page.set_content(markup.format('<p>旧回复</p>'))
                driver = cls(WEB_DRIVERS[cls.provider_name])
                driver._page = self.page
                driver._browser = self.driver._browser
                driver._mark_reply_anchor()
                self.page.evaluate("html => document.body.insertAdjacentHTML('beforeend', html)",
                                   markup.format('<h2>第一章</h2><p>新回复</p>'))
                self.assertEqual(driver._new_reply_text(), '## **第一章**\n\n新回复')

    def test_hidden_stop_is_not_generating_and_fast_completed_reply_is_read(self):
        self.page.set_content('<button id="stop" style="display:none">停止</button>')
        self.driver._mark_reply_anchor()
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
            '<div class="assistant"><p>新答案</p><button class="done"></button></div>')""")
        self.assertFalse(self.driver._stop_visible())
        self.assertTrue(self.driver.wait_complete(max_wait=1))
        self.assertEqual(self.driver.read_result(), "新答案")

    def test_visible_selector_skips_offscreen_duplicate(self):
        self.page.set_content('''
          <button class="new" style="position:absolute;left:-1000px">旧入口</button>
          <button class="new">可用入口</button>''')
        selected = self.driver._visible_selector(("button.new",))
        self.assertEqual(selected, "button.new >> nth=1")

    def test_stable_partial_reply_with_stop_remains_incomplete(self):
        self.page.set_content('<button id="stop">停止</button>')
        self.driver._mark_reply_anchor()
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend','<div class="assistant"><p>未写完</p></div>')""")
        self.assertFalse(self.driver.wait_complete(max_wait=0.12))
        with self.assertRaisesRegex(RuntimeError, "部分回复"):
            self.driver.read_result()

    def test_previous_answer_toolbar_cannot_prove_new_answer_completed(self):
        self.page.set_content('<div class="assistant"><p>旧回答</p><button class="done">复制</button></div>')
        self.driver._mark_reply_anchor()
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend','<div class="assistant"><p>部分新回复</p></div>')""")
        self.assertFalse(self.driver.wait_complete(max_wait=0.12))

    def test_yuanbao_short_reply_can_complete_between_poll_ticks(self):
        driver = YuanbaoDriver({**WEB_DRIVERS["Yuanbao"], "poll_interval": 0.02})
        driver._page = self.page
        driver._browser = self.driver._browser
        self.page.set_content('<div id="yuanbao-send-btn" aria-label="发送">发送</div>')
        driver._mark_reply_anchor()
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
            '<div data-conv-id="new"><div class="agent-chat__conv--ai__speech_show"><p>{"ok":true}</p></div></div>')""")
        self.assertTrue(driver.wait_complete(max_wait=1))
        self.assertEqual(driver.read_result(), '{"ok":true}')

    def test_cannot_read_history_without_send_anchor(self):
        self.page.set_content('<div class="assistant"><p>旧回答</p></div>')
        self.assertEqual(self.driver._new_reply_text(), "")

    def test_raw_copy_preserves_json_and_restores_clipboard_function(self):
        self.driver.copy_selectors = (".done",)
        self.driver.copy_owner_selector = ".assistant"
        self.page.set_content('<div class="assistant"><button class="done">旧复制</button></div>')
        self.driver._mark_reply_anchor()
        self.page.evaluate("""() => {
          window.originalClipboardWrite=navigator.clipboard.writeText;
          const reply=document.createElement('div');reply.className='assistant';
          reply.innerHTML='<p>{“ok”:true}</p><button class="done">复制</button>';
          reply.querySelector('button').onclick=()=>navigator.clipboard.writeText('{"ok":true}');
          document.body.appendChild(reply);
        }""")
        self.assertEqual(self.driver._copy_current_reply(), '{"ok":true}')
        self.assertTrue(self.page.evaluate("navigator.clipboard.writeText===window.originalClipboardWrite"))
        self.assertIsNone(self.page.evaluate("window.__aqCopyCapture"))

    def test_account_dom_does_not_treat_story_about_bans_as_account_restriction(self):
        from web_drivers.account_status import _STATE_JS
        self.page.set_content('<div class="chat-input-editor" contenteditable="true"></div><p>主角的账号被封禁</p>')
        self.page.evaluate("localStorage.setItem('access_token','test'); localStorage.setItem('msh_user_id','test')")
        facts = self.page.evaluate(_STATE_JS, "Kimi")
        self.assertTrue(facts["credential"])
        self.assertFalse(facts["restricted"])
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend','<div role="alert">你的账号已被封禁</div>')""")
        self.assertTrue(self.page.evaluate(_STATE_JS, "Kimi")["restricted"])

    def test_anonymous_token_and_hidden_login_do_not_prove_an_account(self):
        from web_drivers.account_status import _STATE_JS
        self.page.set_content('<div class="chat-input-editor" contenteditable="true"></div><button style="display:none">登录</button>')
        self.page.evaluate("localStorage.setItem('anonymous_access_token','guest')")
        facts = self.page.evaluate(_STATE_JS, "Kimi")
        self.assertTrue(facts["composer"])
        self.assertFalse(facts["credential"])
        self.assertFalse(facts["login"])


if __name__ == "__main__":
    unittest.main()
