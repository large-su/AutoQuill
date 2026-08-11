# 富文本粘贴实验：验证知乎编辑器是否接受剪贴板 HTML（粗体落盘）。
# 实验 A：navigator.clipboard 写 text/html + 真实 Ctrl+V（最接近人工粘贴）
# 实验 B：document.execCommand('insertHTML')
# 判定标准：草稿 API 内容是否出现 <strong>/<b>（服务端落盘才算数）。
import json
import sys
import time

sys.path.insert(0, r"D:\Code\AutoQuill")

from applications.zhihu_story.browser_adapter import get_browser

URL = "https://www.zhihu.com/question/2059899000308994593"
SAMPLE_HTML = ("开场白第一段。<br><br>第<b>1</b>节<br><br>"
               "第二段含<strong>加粗词</strong>和普通字。")
SAMPLE_PLAIN = "开场白第一段。\n\n第1节\n\n第二段含加粗词和普通字。"

browser = get_browser()
browser.open_question(URL)

if not browser._find_write_button(timeout=20):
    print("RESULT: 未定位写回答按钮")
    sys.exit(1)
browser.page.wait_for_selector(
    '[contenteditable="true"], .AnswerForm-editor', timeout=10000)
print("RESULT: 编辑器已打开")


def clear_editor():
    browser.page.evaluate("() => { document.execCommand('selectAll'); }")
    browser.page.keyboard.press("Delete")
    browser.page.wait_for_timeout(600)


def draft():
    return browser.get_draft_content() or ""


def editor_html():
    return browser.page.evaluate(
        """() => {
          const el = document.querySelector(
            '.AnswerForm-editor [contenteditable="true"], [contenteditable="true"]');
          return el ? el.innerHTML : '';
        }""")


def report(tag):
    print(f"\n===== {tag} =====")
    d = draft()
    print(f"{tag}-DRAFT({len(d)}): {d[:300]}")
    print(f"{tag}-EDITOR: {editor_html()[:300]}")


# ---- 实验 A：剪贴板富文本 + 真实 Ctrl+V ----
clear_editor()
try:
    browser.page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=URL)
    browser.page.evaluate(
        """([h, p]) => navigator.clipboard.write([
            new ClipboardItem({
              'text/html': new Blob([h], {type: 'text/html'}),
              'text/plain': new Blob([p], {type: 'text/plain'})
            })
          ]).then(() => true)""", [SAMPLE_HTML, SAMPLE_PLAIN])
    browser.page.wait_for_timeout(800)
    editor = browser.page.locator(
        '.AnswerForm-editor [contenteditable="true"], '
        '[contenteditable="true"]').first
    editor.focus()
    browser.page.keyboard.press("Control+V")
    browser.page.wait_for_timeout(4000)
    report("A")
except Exception as e:
    print(f"A-EXCEPTION: {e}")

# ---- 实验 B：execCommand insertHTML ----
if "<strong>" not in draft() and "<b>" not in draft():
    clear_editor()
    try:
        browser.page.evaluate(
            """(h) => { document.execCommand('insertHTML', false, h); }""",
            SAMPLE_HTML)
        browser.page.wait_for_timeout(4000)
        report("B")
    except Exception as e:
        print(f"B-EXCEPTION: {e}")

print("\nRESULT-DONE")
time.sleep(2)
