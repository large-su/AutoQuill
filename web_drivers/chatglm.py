from config import WEB_DRIVERS
from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login


class ChatGLMDriver(ConfiguredTextChatDriver):
    provider_name = "ChatGLM"
    input_selectors = ("textarea.scroll-display-none",)
    # Public page's sidebar and composer, checked 2026-11-04.
    new_chat_selectors = (".aside-subjects .new-session",)
    send_selectors = ("#search-input-box .enter:not(.searching) .enter-icon-container:not(.empty):not(.disable)",)
    # Official live chat chunk 8461 supplies these conversation nodes.
    reply_selectors = (".answer-content .markdown-body",)
    stop_selectors = ("#search-input-box .enter.searching .enter-icon-container",)
    thinking_selectors = (".advance-thinking",)
    done_selectors = (".interact .shim.copy.canuse",)
    copy_selectors = (".interact .shim.copy.canuse",)
    copy_owner_selector = ".answer"


def web_llm_logged_in(): return generic_logged_in("ChatGLM", WEB_DRIVERS["ChatGLM"])
def login_web_flow(timeout=300): return generic_login("ChatGLM", timeout)
