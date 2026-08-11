# 分离进程：打开知乎问题页并进入编辑器，保持浏览器存活供人工验收。
# 用法：python tools/hold_for_review.py <question_url>
import sys
import time

sys.path.insert(0, r"D:\Code\AutoQuill")

from applications.zhihu_story.browser_adapter import get_browser

url = sys.argv[1] if len(sys.argv) > 1 else \
    "https://www.zhihu.com/question/1910933568240203771"

browser = get_browser()
browser.open_question(url)
browser.page.wait_for_timeout(3000)
if browser._find_write_button(timeout=20):
    try:
        browser.page.wait_for_selector(
            '[contenteditable="true"]', timeout=10000)
        print("REVIEW-READY: 编辑器已打开，草稿已加载", flush=True)
    except Exception:
        print("REVIEW-READY: 页面已打开（编辑器未定位）", flush=True)
else:
    print("REVIEW-READY: 页面已打开（无写回答入口）", flush=True)
while True:
    time.sleep(60)
