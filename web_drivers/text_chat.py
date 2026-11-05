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
    copy_selectors = ()
    copy_owner_selector = ""

    def _read_probe(self):
        text = self._new_reply_text()
        thinking = (self._safe_evaluate("""selectors => selectors.length ?
          [...document.querySelectorAll(selectors.join(','))]
            .filter(e => !window.__aqThinkingAnchor?.has(e))
            .reduce((n,e)=>n+(e.textContent||'').length,0) : 0
        """, list(self.thinking_selectors)) or 0) if self.thinking_selectors else 0
        return {"main": text, "think_len": int(thinking),
                "fresh": 1 if text else 0}

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
        if status == "challenge":
            from web_drivers.browser_pool import WebVerificationRequired
            raise WebVerificationRequired(f"{self.provider_name}：{state.get('message', '网页需要人工验证')}")
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
        self.apply_preferences()

    def _detect_session_id(self):
        from web_drivers.session_ui import detect_session_id
        return detect_session_id(self.provider_name, self._page_instance().url)

    def _delete_current_session_impl(self):
        from web_drivers.session_ui import delete_owned_session
        return delete_owned_session(self)

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
            target = self._page_instance().locator(selector).first
            target.fill(str(prompt), timeout=30000)
            actual = target.evaluate("e=>e.value ?? e.innerText ?? ''")
            normalize = lambda value: ''.join(str(value).replace('\u200b', '').split())
            if normalize(actual) != normalize(prompt):
                raise RuntimeError(f"{self.provider_name} 输入未完整写入，已停止发送")
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"{self.provider_name} 输入失败") from exc

    def _mark_reply_anchor(self):
        self._completed = False
        # Keep node identities as well as count: virtualized history must never
        # become a new reply merely because old DOM nodes disappeared.
        self._reply_baseline = self._safe_evaluate(
            """(selectors, done, thinking, copy) => {
              const nodes = [...document.querySelectorAll(selectors.join(','))];
              window.__aqReplyAnchor = new WeakSet(nodes);
              window.__aqDoneAnchor = new WeakSet(done.length
                ? document.querySelectorAll(done.join(',')) : []);
              window.__aqCopyAnchor = new WeakSet(copy.length
                ? document.querySelectorAll(copy.join(',')) : []);
              window.__aqThinkingAnchor = new WeakSet(thinking.length
                ? document.querySelectorAll(thinking.join(',')) : []);
              return nodes.length;
            }""", list(self.reply_selectors), list(self.done_selectors), list(self.thinking_selectors), list(self.copy_selectors))
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
            except Exception:
                raise RuntimeError(f"{self.provider_name} 发送按钮点击未确认，已停止重复发送")
            self._confirm_sent()
            return
        input_selector = self._visible_selector(self.input_selectors)
        if not input_selector:
            self._dump_page_state(f"未找到 {self.provider_name} 发送入口")
        try:
            page.locator(input_selector).first.press("Enter", timeout=5000)
        except Exception as exc:
            raise RuntimeError(f"{self.provider_name} 发送失败") from exc
        self._confirm_sent()

    def _confirm_sent(self):
        """Do not wait for a response when the website kept an unsent draft."""
        from web_drivers.browser_pool import _check_cancel
        until = time.monotonic() + 8
        while time.monotonic() < until:
            _check_cancel()
            self._raise_if_logged_out(require_ready=False)
            selector = self._visible_selector(self.input_selectors)
            if selector:
                text = self._page_instance().locator(selector).first.evaluate(
                    "e=>e.value ?? e.innerText ?? ''")
                if not str(text).strip():
                    return
            if self._stop_visible() or self._new_reply_text():
                return
            self._page_instance().wait_for_timeout(250)
        raise RuntimeError(f"{self.provider_name} 未确认网页接收提示词，请检查输入及发送入口")

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
            locator = self._page_instance().locator(selector)
            count = locator.count()
            viewport = getattr(self._page_instance(), "viewport_size", None) or {}
            width = viewport.get("width", 10**9)
            height = viewport.get("height", 10**9)
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    box = candidate.bounding_box()
                    in_viewport = (box and box["x"] + box["width"] > 0
                                   and box["y"] + box["height"] > 0
                                   and box["x"] < width and box["y"] < height)
                    if candidate.is_visible() and in_viewport:
                        # Return an exact locator selector for callers that
                        # need to click a specific duplicate menu entry.
                        return f"{selector} >> nth={index}"
                except Exception:
                    continue
        return None

    def _stop_visible(self):
        if not self.stop_selectors:
            return False
        return self._visible(self.stop_selectors)

    def _stop_button_present(self):
        """Compatibility alias for the provider-independent scheduler."""
        return self._stop_visible()

    def _done_visible(self):
        if not self.done_selectors:
            return False
        return bool(self._safe_evaluate("""(selectors) =>
          !!window.__aqDoneAnchor && [...document.querySelectorAll(selectors.join(','))]
            .some(e => !window.__aqDoneAnchor.has(e) && (e.offsetWidth || e.offsetHeight)
              && getComputedStyle(e).visibility !== 'hidden')
        """, list(self.done_selectors)))

    def _generation_probe(self):
        self._raise_if_logged_out(require_ready=False)
        probe = self._read_probe()
        return {
            "text": probe.get("main") or "",
            "thinking_chars": int(probe.get("think_len") or 0),
            "generating": self._stop_visible(),
            "done": self._done_visible(),
        }

    def _generation_completed(self, facts, tracker):
        # A toolbar/action marker is the site confirmation for short answers
        # that finish between two polls; otherwise we require the stop control
        # to have appeared and then disappeared.
        return (bool(facts["text"]) and not facts["generating"]
                and tracker["stable"] >= int(self.config.get("stable_count", 2))
                and (facts["done"] or tracker["stop_seen"]))

    def wait_complete(self, max_wait=None):
        self._completed = super().wait_complete(max_wait=max_wait)
        return self._completed

    def read_result(self):
        self._raise_if_logged_out()
        if not getattr(self, "_completed", False):
            raise RuntimeError(f"{self.provider_name} 尚未确认生成结束，未采用部分回复")
        raw = self._copy_current_reply()
        # Raw Copy preserves structured syntax; rendered prose preserves
        # paragraph boundaries which sites may omit from their clipboard text.
        structured = raw.lstrip().startswith(('{', '[', '```json'))
        text = raw if structured else (self._new_reply_text() or raw)
        if not text:
            self._dump_page_state(f"未读取到 {self.provider_name} 的新回复，请检查网站页面是否有变化")
        return text

    def _copy_current_reply(self):
        """Read a site's own raw copy payload without touching the OS clipboard.

        Some Markdown renderers replace ASCII JSON quotes with typographic
        quotes. Their official Copy action still exposes the exact raw answer.
        Interception is scoped to this page and restored after the one click.
        """
        if not self.copy_selectors:
            return ""
        found = self._safe_evaluate("""(selectors, owners, replies) => {
          document.querySelectorAll('[data-aq-copy-current]').forEach(e=>e.removeAttribute('data-aq-copy-current'));
          const candidates=[...document.querySelectorAll(selectors.join(','))]
            .filter(e=>!window.__aqCopyAnchor?.has(e)&&(e.offsetWidth||e.offsetHeight))
            .filter(e=>{
              const owner=owners?e.closest(owners):null;
              return owner && [owner,...owner.querySelectorAll(replies.join(','))].filter(
                reply=>reply.matches(replies.join(','))).some(
                reply=>!window.__aqReplyAnchor?.has(reply));
            });
          if(!candidates.length)return false;
          candidates[candidates.length-1].setAttribute('data-aq-copy-current','true');
          return true;
        }""", list(self.copy_selectors), self.copy_owner_selector, list(self.reply_selectors))
        if not found:
            return ""
        self._safe_evaluate("""() => {
          window.__aqCopyCapture={text:'',exec:document.execCommand,
            write:navigator.clipboard?.writeText};
          const capture=text=>{window.__aqCopyCapture.text=String(text||'');return Promise.resolve();};
          try { if(navigator.clipboard) navigator.clipboard.writeText=capture; } catch(_){}
          document.execCommand=function(command,...args){
            if(String(command).toLowerCase()==='copy'){
              const active=document.activeElement;
              window.__aqCopyCapture.text=active?.value || window.getSelection()?.toString() || '';
              return true;
            }
            return window.__aqCopyCapture.exec.call(document,command,...args);
          };
        }""")
        try:
            self._page_instance().locator('[data-aq-copy-current="true"]').click(timeout=4000)
            self._page_instance().wait_for_timeout(150)
            return (self._safe_evaluate("() => window.__aqCopyCapture?.text || ''") or '').strip()
        except Exception as exc:
            from web_drivers.browser_pool import WorkflowCancelled, WebLoginRequired, WebLoginCheckError
            if isinstance(exc, (WorkflowCancelled, WebLoginRequired, WebLoginCheckError)):
                raise
            log.warning("%s 官方复制入口暂不可用，读取本轮正文", self.provider_name)
            return ""
        finally:
            from web_drivers.browser_pool import session_cleanup
            with session_cleanup():
                self._safe_evaluate("""() => {
                  const capture=window.__aqCopyCapture;
                  if(!capture)return;
                  document.execCommand=capture.exec;
                  try{if(navigator.clipboard&&capture.write)navigator.clipboard.writeText=capture.write;}catch(_){}
                  delete window.__aqCopyCapture;
                }""")


def generic_logged_in(provider_name, _config):
    """Central account status owns profile locking and strict login evidence."""
    from web_drivers.account_status import logged_in
    return logged_in(provider_name)


def generic_login(provider_name, timeout=300):
    from web_drivers.account_status import login_account
    return login_account(provider_name, timeout=timeout)
