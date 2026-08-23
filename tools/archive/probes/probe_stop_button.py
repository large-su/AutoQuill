# ============================================================
# probe_stop_button.py — 生成中 dump 停止按钮真实 DOM
#
# 三次剖析 probe 显示 _STOP_SELECTORS 5 个候选全部未命中——新版
# DeepSeek UI 的停止按钮形态变了，wait_complete 主完成路径失效
# （只能靠「文本稳定」兜底）。本脚本发一个短 prompt，生成中
# dump 所有 button/role=button 的 class/aria-label/子元素。
#
# 运行：PYTHONPATH=. python tools/probe_stop_button.py
# 输出保持 ASCII（GBK 安全）
# ============================================================

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)


def _probe_sel(page, candidates):
    js = (
        "async function() {"
        "  for (const s of arguments[0]) {"
        "    const el = document.querySelector(s);"
        "    if (el) return {sel: s, val: el.tagName};"
        "  }"
        "  return {sel: null, val: null};"
        "}"
    )
    try:
        r = page.evaluate(js, list(candidates)) or {}
        return r.get("sel")
    except Exception:
        return None


def main():
    from applications.zhihu_story.browser_adapter import get_browser
    browser = get_browser()
    page = browser.context.new_page()
    try:
        page.goto("https://chat.deepseek.com", wait_until="domcontentloaded",
                  timeout=30000)
        page.wait_for_timeout(2500)
        if "sign_in" in page.url:
            print("[FAIL] 未登录")
            return 1
        inp = _probe_sel(page, (
            "textarea#chat-input",
            "textarea[data-testid='chat_input_input']",
            "textarea[placeholder*='给 DeepSeek']",
            "div[contenteditable='true']",
        ))
        page.locator(inp).fill(
            "写一篇 800 字的小说开头，主角是一名出租车司机，"
            "要求有对话、环境描写和悬念。")
        page.keyboard.press("Enter")
        print("[INFO] 已发送长 prompt，每 3s 对比按钮变化…")

        js = (
            "() => {"
            "  const els = Array.from(document.querySelectorAll("
            "      'button, [role=button]'));"
            "  return els.map(b => ({"
            "    tag: b.tagName,"
            "    cls: (b.className || '').toString().slice(0, 70),"
            "    aria: b.getAttribute('aria-label') || '',"
            "    text: (b.innerText || '').trim().slice(0, 10),"
            "    svg: b.querySelector('svg') ? 'svg' : '',"
            "  }));"
            "}"
        )
        prev = None
        start = time.time()
        while time.time() - start < 90:
            try:
                buttons = page.evaluate(js)
            except Exception as exc:
                print(f"  evaluate 失败: {exc}")
                page.wait_for_timeout(3000)
                continue
            key = [(b["tag"], b["cls"], b["aria"]) for b in buttons]
            if key != prev:
                print(f"\n  t={time.time()-start:.1f}s 按钮集合变化 "
                      f"({len(buttons)} 个):")
                for b in buttons:
                    print(f"    tag={b['tag']} cls={b['cls']!r} "
                          f"aria={b['aria']!r} text={b['text']!r} "
                          f"svg={b['svg']}")
                prev = key
            page.wait_for_timeout(3000)
        return 0
    finally:
        page.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
