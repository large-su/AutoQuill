from config import WEB_DRIVERS
from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login


class KimiDriver(ConfiguredTextChatDriver):
    provider_name = "Kimi"
    input_selectors = ("div.chat-input-editor[contenteditable='true']",)
    new_chat_selectors = ("a[data-testid='sidebar-new-chat']",)
    send_selectors = ("button[aria-label*='发送']",)
    # Official Kimi chat bundle renders assistant segments as segment-content;
    # user segments contain user-content.  Logged-in output still needs E2E
    # confirmation because the public probe contains no conversation.
    reply_selectors = (".segment-content:not(:has(.user-content)) .segment-content-box",)
    stop_selectors = (".stop-tip-wrapper", ".segment-pending-loading")
    thinking_selectors = (".thinking-container",)
    # The official segment renderer exposes this actions bar only after the
    # segment is no longer streaming (including its Copy action).
    done_selectors = (".segment-assistant-actions",)


def web_llm_logged_in(): return generic_logged_in("Kimi", WEB_DRIVERS["Kimi"])
def login_web_flow(timeout=300): return generic_login("Kimi", timeout)
