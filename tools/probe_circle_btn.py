# ============================================================
# probe_circle_btn.py — 圆形发送/停止按钮完整 class 监控
#
# probe_stop_button2 发现：停止按钮 = 发送按钮（同一圆形 primary
# DIV），生成中 class 后缀为哈希（_52c986b），完成后恢复
# ds-button--disa...（疑似 --disabled）。class 在 dump 中截断到
# 120 字符——本脚本只监控这一个按钮，打印完整 className，
# 验证「生成中 / 完成」两种形态及输入框有内容时的形态。
#
# 运行：PYTHONPATH="D:/Code/AutoQuill" python tools/probe_circle_btn.py
# 输出保持 ASCII（GBK 安全）
# ============================================================

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

_JS = (
    "() => {"
    "  const btn = document.querySelector("
    "      \"div[class*='ds-button--circle']\");"
    "  const ta = document.querySelector('textarea');"
    "  const main = document.querySelector("
    "      \"div[class*='ds-assistant-message-main-content']\");"
    "  return {"
    "    cls: btn ? btn.className : '(no btn)',"
    "    input: ta ? ta.value.length : -1,"
    "    main: main ? main.innerText.length : 0,"
    "  };"
    "}"
)


def main():
    from applications.zhihu_story.browser_adapter import get_browser
    browser = get_browser()
    page = browser.context.new_page()
    try:
        page.goto("https://chat.deepseek.com", wait_until="domcontentloaded",
                  timeout=30000)
        page.wait_for_timeout(3000)
        if "sign_in" in page.url:
            print("[FAIL] 未登录")
            return 1

        from web_drivers.deepseek import _INPUT_SELECTORS
        sel = None
        for s in _INPUT_SELECTORS:
            try:
                if page.query_selector(s):
                    sel = s
                    break
            except Exception:
                pass
        if not sel:
            print("[FAIL] 找不到输入框")
            return 1

        def snap(tag):
            try:
                s = page.evaluate(_JS)
                print(f"[{tag}] cls={s['cls']!r} input={s['input']} "
                      f"main={s['main']}")
            except Exception as exc:
                print(f"[{tag}] 探测失败: {exc}")

        snap("发送前(空输入)")
        page.locator(sel).fill("写一个 300 字微小说：深夜便利店。")
        page.wait_for_timeout(500)
        snap("已输入未发送")
        page.keyboard.press("Enter")
        print("[INFO] 已发送，监控按钮形态…")

        start = time.time()
        prev_cls = None
        done_stable = 0
        while time.time() - start < 90:
            page.wait_for_timeout(1000)
            try:
                s = page.evaluate(_JS)
            except Exception as exc:
                print(f"  t={time.time()-start:.1f}s 探测失败: {exc}")
                continue
            if s["cls"] != prev_cls:
                print(f"  t={time.time()-start:5.1f}s [class变化] "
                      f"main={s['main']} input={s['input']}")
                print(f"      cls={s['cls']!r}")
                prev_cls = s["cls"]
            # 完成信号 = 圆形按钮恢复 disabled 态（含 ds-button--disabled）
            if "ds-button--disabled" in s["cls"] and s["main"]:
                done_stable += 1
                if done_stable >= 3:
                    print(f"[DONE] 按钮恢复 disabled，正文稳定，"
                          f"main={s['main']}")
                    return 0
            else:
                done_stable = 0
        return 0
    finally:
        page.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
