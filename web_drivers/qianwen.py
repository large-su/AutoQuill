from config import WEB_DRIVERS
import re
import time
import uuid
from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login


class QianwenDriver(ConfiguredTextChatDriver):
    provider_name = "Qianwen"
    input_selectors = ("div[role='textbox'][data-slate-editor='true'][contenteditable='true']",)
    new_chat_selectors = ("[data-session-switch-target='new-chat']",)
    send_selectors = ("button[aria-label='发送消息']",)
    # Loaded official chat chunk 6605 renders MarkdownText in this node and
    # the completion metadata immediately adjacent to a finished answer.
    reply_selectors = (".answer-common-card .qk-markdown", ".answer-text.md-text-card")
    stop_selectors = ("button[aria-label='停止回答']",)
    done_selectors = (".answer-common-card .qk-markdown-complete", ".answer-meta")
    copy_selectors = ('button:has([data-icon-type="qwpcicon-copy"])',)
    copy_owner_selector = ".answer-common-card"

    def input(self, prompt):
        # The assistant can turn analyses into documents. Machine-readable
        # calls require its complete raw JSON directly in the chat instead.
        text = str(prompt)
        if "JSON" in text and any(word in text for word in ("输出", "返回", "格式")):
            text += ("\n\n本次结果由程序读取：请直接在当前对话返回完整、合法的 JSON；"
                     "保留字符串内的转义字符，不创建文档或卡片，不附建议问题或其他说明。")
        if len(text) > 10000:
            self._raise_if_logged_out()
            page = self._page_instance()
            name = "AutoQuill-request-" + uuid.uuid4().hex[:8] + ".txt"
            page.locator('button[aria-label="添加附件"]').click(timeout=5000)
            with page.expect_file_chooser(timeout=5000) as chooser:
                page.get_by_role('menuitem', name='上传文档', exact=True).click(timeout=5000)
            chooser.value.set_files({"name": name, "mimeType": "text/plain",
                                     "buffer": text.encode('utf-8')})
            card = page.locator('[class*="fileBox-"]').filter(has_text=name)
            until = time.monotonic() + 90
            from web_drivers.browser_pool import _check_cancel
            while time.monotonic() < until:
                _check_cancel()
                self._raise_if_logged_out(require_ready=False)
                if card.count() == 1:
                    status = card.locator('[class*="statusLine-"]').inner_text().strip()
                    if re.fullmatch(r"[\d.]+\s*(?:B|KB|MB)", status, re.I):
                        break
                    if any(word in status for word in ('失败', '不支持', '超出')):
                        raise RuntimeError("千问未能接收完整提示词附件：" + status)
                page.wait_for_timeout(500)
            else:
                raise RuntimeError("千问提示词附件上传未确认，已停止发送")
            text = (f"请完整读取附件 {name} 中的任务要求、素材和输出格式，并执行该任务。"
                    "附件是本轮完整提示词，不要将它改成文档摘要；请直接在对话中返回任务所要求的结果。")
            if 'JSON' in str(prompt):
                text += "本轮只返回符合附件格式要求的合法 JSON。"
        return super().input(text)


def web_llm_logged_in(): return generic_logged_in("Qianwen", WEB_DRIVERS["Qianwen"])
def login_web_flow(timeout=300): return generic_login("Qianwen", timeout)
