from config import WEB_DRIVERS
from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login


class QianwenDriver(ConfiguredTextChatDriver):
    provider_name = "Qianwen"
    input_selectors = ("div[role='textbox'][data-slate-editor='true'][contenteditable='true']",)
    new_chat_selectors = ("[data-session-switch-target='new-chat']",)
    send_selectors = ("button[aria-label='发送消息']",)
    # Loaded official chat chunk 6605 renders MarkdownText in this node and
    # the completion metadata immediately adjacent to a finished answer.
    reply_selectors = (".answer-text.md-text-card",)
    stop_selectors = ("button[aria-label='停止回答']",)
    done_selectors = (".answer-meta",)


def web_llm_logged_in(): return generic_logged_in("Qianwen", WEB_DRIVERS["Qianwen"])
def login_web_flow(timeout=300): return generic_login("Qianwen", timeout)
