from config import WEB_DRIVERS
from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login


class YuanbaoDriver(ConfiguredTextChatDriver):
    provider_name = "Yuanbao"
    input_selectors = ("div.ql-editor[contenteditable='true']",)
    new_chat_selectors = (".yb-new-chat-entry__item[aria-label='新建对话']", "[aria-label='新建对话']")
    send_selectors = ("#yuanbao-send-btn[aria-label='发送']",)
    # Loaded official chunks query AI speech by data-conv-id and this exact
    # class; /chat/naQivTmsDa remains an agent ID, not a conversation ID.
    reply_selectors = ("div[data-conv-id] .agent-chat__conv--ai__speech_show",)
    # Official chat bundle creates this div with aria-label "停止回答" while
    # streaming (and "发送" at rest); it is not a generic text guess.
    stop_selectors = ("#yuanbao-send-btn[aria-label='停止回答']",)

    def _done_visible(self):
        # The same official control changes its label back to 发送 at rest.
        # This also covers short replies which finish between polling ticks;
        # the shared waiter still requires a stable, newly anchored reply.
        return self._visible(self.send_selectors)


def web_llm_logged_in(): return generic_logged_in("Yuanbao", WEB_DRIVERS["Yuanbao"])
def login_web_flow(timeout=300): return generic_login("Yuanbao", timeout)
