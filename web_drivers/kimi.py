from config import WEB_DRIVERS
from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login


class KimiDriver(ConfiguredTextChatDriver):
    provider_name = "Kimi"
    input_selectors = ("div.chat-input-editor[contenteditable='true']",)
    new_chat_selectors = ("a[data-testid='sidebar-new-chat']",)
    send_selectors = (".send-button-container", "button[aria-label*='发送']")
    # Official Kimi chat bundle renders assistant segments as segment-content;
    # user segments contain user-content.  Logged-in output still needs E2E
    # confirmation because the public probe contains no conversation.
    reply_selectors = (".segment-content:not(:has(.user-content)) .segment-content-box",)
    stop_selectors = (".stop-tip-wrapper", ".segment-pending-loading")
    thinking_selectors = (".thinking-container",)
    # The official segment renderer exposes this actions bar only after the
    # segment is no longer streaming (including its Copy action).
    done_selectors = (".segment-assistant-actions",)

    def _raise_if_logged_out(self, require_ready=True):
        state = super()._raise_if_logged_out(require_ready=require_ready)
        notice = self._safe_evaluate(r"""()=>[...document.querySelectorAll(
          '[role="dialog"],[role="alertdialog"],[data-testid="confirm-dialog"]')]
          .filter(e=>e.offsetWidth||e.offsetHeight).map(e=>e.innerText||'').join('\n')""") or ''
        if '和Kimi聊天的人太多了' in notice or ('优先队列' in notice and '订阅' in notice):
            from web_drivers.browser_pool import WebModelUnavailable
            raise WebModelUnavailable('Kimi 当前模型繁忙，平台提示需要等待或会员优先队列，请稍后重试或手动选择其他模型')
        return state

    def new_chat(self):
        # Kimi's responsive sidebar keeps a hidden off-canvas new-chat link.
        # Its root URL is the site's explicit blank composer, so navigating
        # there in this driver-owned page creates an independent conversation.
        self._prepare_new_session()
        self.open_session()
        if self._reply_count() or not self._visible(self.input_selectors):
            raise RuntimeError("Kimi 未确认空白新会话，已停止发送")
        return self


def web_llm_logged_in(): return generic_logged_in("Kimi", WEB_DRIVERS["Kimi"])
def login_web_flow(timeout=300): return generic_login("Kimi", timeout)
