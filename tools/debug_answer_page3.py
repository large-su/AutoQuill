# 临时探测脚本3：回答列表懒加载机制（滚动加载总量）
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = "https://www.zhihu.com/creator/manage/creation/answer"

COUNT_JS = "() => document.querySelectorAll('.CreationManage-CreationCard').length"
BODY_TAIL_JS = "() => document.body.innerText.slice(-300)"


def main():
    from applications.zhihu_story.browser_adapter import get_browser

    browser = get_browser()
    page = browser.page
    page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)

    n0 = page.evaluate(COUNT_JS)
    print(f"初始卡片数: {n0}")
    print("页面尾部:", repr(page.evaluate(BODY_TAIL_JS)))

    # 分轮滚动，看每次加载多少
    for i in range(1, 6):
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)
        n = page.evaluate(COUNT_JS)
        print(f"滚动 {i} 次后卡片数: {n}")
        if n == n0:
            print("  卡片数未增长，可能已到底或需其他触发")
        n0 = n

    print("\n页面尾部:", repr(page.evaluate(BODY_TAIL_JS)))
    print("\n页面保持打开 10 秒…")
    time.sleep(10)
    browser.close()


if __name__ == "__main__":
    main()
