"""Shared DOM lifecycle for provider-bound text chats."""
import logging
import time

from web_drivers.base import MARKDOWN_REBUILD_JS, WebLLMDriver

log = logging.getLogger(__name__)


class ConfiguredTextChatDriver(WebLLMDriver):
    """Small common driver whose navigation always uses its own config URL."""

    provider_name = "Web LLM"
    input_selectors = ()
    send_selectors = ()
    new_chat_selectors = ()
    # These require a logged-in DOM probe before they can be called verified.
    reply_selectors = ()
    stop_selectors = ()
    thinking_selectors = ()
    done_selectors = ()

    def _raise_if_logged_out(self, require_ready=True):
        """Only explicit account outcomes alter the normal generation path."""
        from web_drivers.account_status import inspect_account
        state = inspect_account(self.provider_name, self._get_browser().context,
                                self._page_instance()) or {}
        status = state.get("status", "unknown")
        if status == "ready" or (status == "unknown" and not require_ready):
            return state
        from web_drivers.browser_pool import WebLoginCheckError, WebLoginRequired
        if status == "restricted":
            from web_drivers.browser_pool import WebAccountRestricted
            raise WebAccountRestricted(f"{self.provider_name}：{state.get('message', '网页账号受限')}")
        if status in {"challenge", "unknown", "busy"}:
            raise WebLoginCheckError(f"{self.provider_name}：{state.get('message', '网页登录状态无法确认')}")
        if status == "logged_out":
            raise WebLoginRequired(f"{self.provider_name} 网页需要重新登录")
        return state

    def open_session(self):
        """Do not use base.open_session: it follows global WEB_DRIVER_NAME."""
        from web_drivers.browser_pool import _check_cancel
        from web_drivers.browser_pool import WebLoginCheckError, WebLoginRequired
        url = self.config["url"]
        page = self._page_instance()
        last_exc = None
        for timeout in (20000, 35000):
            _check_cancel()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                # DOMContentLoaded precedes SPA hydration. Allow the account UI
                # to appear instead of treating every newly opened page as failure.
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = self._raise_if_logged_out(require_ready=False)
                    if state.get("status") == "ready":
                        return self
                    page.wait_for_timeout(250)
                self._raise_if_logged_out()
                return self
            except (WebLoginRequired, WebLoginCheckError):
                raise
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"{self.provider_name} 网页打开失败：{url}") from last_exc

    def setup(self):
        self._raise_if_logged_out()

    def new_chat(self):
        self._prepare_new_session()
        self.open_session()
        selector = self._visible_selector(self.new_chat_selectors)
        if not selector:
            self._dump_page_state(f"未找到 {self.provider_name} 新对话入口")
        page = self._page_instance()
        try:
            page.locator(selector).first.click(timeout=5000)
            page.wait_for_timeout(500)
        except Exception as exc:
            raise RuntimeError(f"{self.provider_name} 无法创建新对话") from exc
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self._raise_if_logged_out(require_ready=False)
            if self._reply_count() == 0 and self._visible(self.input_selectors):
                return self
            page.wait_for_timeout(250)
        raise RuntimeError(f"{self.provider_name} 未能确认空白新对话，已停止发送")

    def input(self, prompt):
        self._raise_if_logged_out()
        selector = self._visible_selector(self.input_selectors)
        if not selector:
            self._dump_page_state(f"未找到 {self.provider_name} 输入框")
        try:
            self._page_instance().locator(selector).first.fill(str(prompt), timeout=8000)
        except Exception as exc:
            raise RuntimeError(f"{self.provider_name} 输入失败") from exc

    def _mark_reply_anchor(self):
        self._completed = False
        # Keep node identities as well as count: virtualized history must never
        # become a new reply merely because old DOM nodes disappeared.
        self._reply_baseline = self._safe_evaluate(
            """(selectors, done) => {
              const nodes = [...document.querySelectorAll(selectors.join(','))];
              window.__aqReplyAnchor = new WeakSet(nodes);
              window.__aqDoneAnchor = new WeakSet(done.length
                ? document.querySelectorAll(done.join(',')) : []);
              return nodes.length;
            }""", list(self.reply_selectors), list(self.done_selectors))
        if self._reply_baseline is None:
            raise RuntimeError(f"{self.provider_name} 无法记录本轮回复起点")

    def send(self):
        self._raise_if_logged_out()
        self._mark_reply_anchor()
        page = self._page_instance()
        selector = self._visible_selector(self.send_selectors)
        if selector:
            try:
                page.locator(selector).first.click(timeout=5000)
                return
            except Exception:
                pass
        input_selector = self._visible_selector(self.input_selectors)
        if not input_selector:
            self._dump_page_state(f"未找到 {self.provider_name} 发送入口")
        try:
            page.locator(input_selector).first.press("Enter", timeout=5000)
        except Exception as exc:
            raise RuntimeError(f"{self.provider_name} 发送失败") from exc

    def _reply_count(self):
        if not self.reply_selectors:
            return 0
        js = """(selectors) => {
          const found = [...document.querySelectorAll(selectors.join(','))]
            .filter(el => el.offsetWidth || el.offsetHeight);
          return found.length;
        }"""
        count = self._safe_evaluate(js, list(self.reply_selectors))
        if count is None:
            raise RuntimeError(f"{self.provider_name} 无法确认当前会话内容，已停止发送")
        return int(count)

    def _new_reply_text(self):
        if not self.reply_selectors:
            return ""
        js = """(selectors, thinking) => {""" + MARKDOWN_REBUILD_JS + """
          if (!window.__aqReplyAnchor) return '';
          const found = [...document.querySelectorAll(selectors.join(','))]
            .filter(el => !window.__aqReplyAnchor.has(el) && (el.offsetWidth || el.offsetHeight)
              && !thinking.some(s => el.closest(s)));
          const fresh = found.filter(el => !found.some(other => other !== el && el.contains(other)));
          if (!fresh.length) return '';
          const node = fresh[fresh.length - 1];
          // Clone into an invisible rendered host so innerText keeps paragraph
          // semantics while excluding reasoning and toolbar descendants.
          const host = document.createElement('div');
          host.style.cssText = 'position:fixed;left:-100000px;top:0;pointer-events:none';
          const copy = node.cloneNode(true);
          thinking.forEach(s => copy.querySelectorAll(s).forEach(el => el.remove()));
          host.appendChild(copy); document.body.appendChild(host);
          try { return toMarkdown(copy) || (copy.innerText || '').trim(); }
          finally { host.remove(); }
        }"""
        return (self._safe_evaluate(js, list(self.reply_selectors),
                                    list(self.thinking_selectors)) or "").strip()

    def _visible(self, selectors):
        return self._visible_selector(selectors) is not None

    def _visible_selector(self, selectors):
        # Playwright text selectors (e.g. :has-text) are supported here as well.
        for selector in selectors:
            if self._page_instance().locator(selector).first.is_visible():
                return selector
        return None

    def _stop_visible(self):
        if not self.stop_selectors:
            return False
        return self._visible(self.stop_selectors)

    def _done_visible(self):
        if not self.done_selectors:
            return False
        return bool(self._safe_evaluate("""(selectors) =>
          !!window.__aqDoneAnchor && [...document.querySelectorAll(selectors.join(','))]
            .some(e => !window.__aqDoneAnchor.has(e) && (e.offsetWidth || e.offsetHeight)
              && getComputedStyle(e).visibility !== 'hidden')
        """, list(self.done_selectors)))

    def wait_complete(self, max_wait=None):
        from web_drivers.browser_pool import _check_cancel
        deadline = time.time() + float(max_wait or self.config.get("max_wait", 600))
        interval = float(self.config.get("poll_interval", 2))
        stable_needed = int(self.config.get("stable_count", 2))
        previous, stable, stop_seen = "", 0, False
        self._completed = False
        while time.time() < deadline:
            _check_cancel()
            self._raise_if_logged_out(require_ready=False)
            generating = self._stop_visible()
            stop_seen = stop_seen or generating
            text = self._new_reply_text()
            if text and text == previous and not generating:
                stable += 1
                if stable >= stable_needed and (stop_seen or self._done_visible()):
                    self._completed = True
                    return True
            else:
                stable = 0
            previous = text
            self._page_instance().wait_for_timeout(int(interval * 1000))
        return False

    def read_result(self):
        self._raise_if_logged_out()
        if not getattr(self, "_completed", False):
            raise RuntimeError(f"{self.provider_name} 尚未确认生成结束，未采用部分回复")
        text = self._new_reply_text()
        if not text:
            self._dump_page_state(f"未读取到 {self.provider_name} 的新回复，请检查网站页面是否有变化")
        return text


def generic_logged_in(provider_name, _config):
    """Central account status owns profile locking and strict login evidence."""
    from web_drivers.account_status import logged_in
    return logged_in(provider_name)


def generic_login(provider_name, timeout=300):
    from web_drivers.account_status import login_account
    return login_account(provider_name, timeout=timeout)
