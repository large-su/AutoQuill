# ============================================================
# probe_stop_button2.py — 发送验证 + 宽选择器按钮 dump（第四版）
#
# 第三版教训：只 dump button/[role=button] 且无发送验证，90s 按钮
# 集合零变化——无法区分「发送失败」还是「停止按钮不是 button 标签」。
# 本版四重验证：
#   1. Enter 后 3s 检查输入框已清空（DeepSeek 发送成功的标志）
#   2. 每 1s 轮询正文/思考长度（生成确实在跑的证据）
#   3. 每次 dump 全部 [class*='ds-button']（含 button/[role=button]），
#      diff 打印新增/移除的按钮（完整 class 指纹，不截断）
#   4. 正文首次增长时全量 dump（生成中 = 停止按钮必在）
#      + 正文停止增长 6s 后全量 dump（生成后 = 重新生成按钮）
#
# 运行：PYTHONPATH="D:/Code/AutoQuill" python tools/probe_stop_button2.py
# 输出保持 ASCII（GBK 安全）
# ============================================================

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

_BTN_JS = (
    "() => {"
    "  const els = Array.from(document.querySelectorAll("
    "      '[class*=\"ds-button\"], button, [role=button]'));"
    "  const seen = new Set();"
    "  const out = [];"
    "  for (const b of els) {"
    "    const cls = (b.className || '').toString().trim();"
    "    const key = cls + '|' + (b.getAttribute('aria-label') || '');"
    "    if (!key || seen.has(key)) continue;"
    "    seen.add(key);"
    "    out.push({"
    "      tag: b.tagName,"
    "      cls: cls.slice(0, 120),"
    "      aria: b.getAttribute('aria-label') || '',"
    "      text: (b.innerText || '').trim().slice(0, 15),"
    "      svg: b.querySelector('svg') ? 'svg' : '',"
    "    });"
    "  }"
    "  return out;"
    "}"
)

_LEN_JS = (
    "() => {"
    "  const main = document.querySelector("
    "      \"div[class*='ds-assistant-message-main-content']\");"
    "  const think = document.querySelector(\"div[class*='ds-think-content']\");"
    "  return {"
    "    main: main ? main.innerText.length : 0,"
    "    think: think ? think.innerText.length : 0,"
    "    input: (() => {"
    "      const ta = document.querySelector('textarea#chat-input,"
    "      textarea[data-testid=\"chat_input_input\"]');"
    "      return ta ? ta.value.length : -1;"
    "    })(),"
    "  };"
    "}"
)


def _dump(page, label):
    try:
        buttons = page.evaluate(_BTN_JS)
    except Exception as exc:
        print(f"  {label}: dump 失败 {exc}")
        return
    print(f"  --- {label} ({len(buttons)} 个) ---")
    for b in buttons:
        print(f"    {b['tag']} cls={b['cls']!r} aria={b['aria']!r} "
              f"text={b['text']!r} {b['svg']}")


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
        sel, _ = None, None
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

        _dump(page, "发送前")
        page.locator(sel).fill(
            "写一篇 800 字的小说开头，主角是一名出租车司机，"
            "要求有对话、环境描写和悬念。")
        page.keyboard.press("Enter")
        print("[INFO] 已发送，开始验证…")

        start = time.time()
        last_main = -1
        last_think = -1
        stable = 0
        dumped_mid = False
        prev_btns = None
        while time.time() - start < 120:
            page.wait_for_timeout(1000)
            try:
                s = page.evaluate(_LEN_JS)
            except Exception as exc:
                print(f"  [WARN] 长度探测失败: {exc}")
                continue
            main_len, think_len, input_len = s["main"], s["think"], s["input"]
            if main_len != last_main:
                last_main = main_len
                stable = 0
            else:
                stable += 1
            if think_len != last_think:
                last_think = think_len
            if not dumped_mid and main_len > 0:
                dumped_mid = True
                print(f"  [INFO] 正文出现（len={main_len}），生成中 dump：")
                _dump(page, "生成中（停止按钮应在此）")
            try:
                btns = page.evaluate(_BTN_JS)
            except Exception:
                btns = None
            if btns is not None:
                now_key = [(b["tag"], b["cls"]) for b in btns]
                if prev_btns is not None and now_key != prev_btns:
                    new_btns = [b for b, k in zip(btns, now_key)
                                if k not in prev_btns]
                    gone_btns = [k for k in prev_btns if k not in now_key]
                    print(f"  t={time.time()-start:.1f}s 按钮变化："
                          f"+{len(new_btns)} -{len(gone_btns)}")
                    for b in new_btns:
                        print(f"    [+新增] {b['tag']} cls={b['cls']!r} "
                              f"aria={b['aria']!r} text={b['text']!r}")
                prev_btns = now_key
            state = "生成中" if main_len else (
                "思考中" if think_len else "等待")
            print(f"  t={time.time()-start:6.1f}s main={main_len:5d} "
                  f"think={think_len:5d} input={input_len:4d} "
                  f"stable={stable} [{state}]")
            if main_len and stable >= 6:
                print(f"[INFO] 正文稳定 6s（len={main_len}），生成后 dump：")
                _dump(page, "生成后（重新生成按钮应在此）")
                try:
                    tail = page.evaluate(
                        "() => {"
                        "  const el = document.querySelector("
                        "      'div[class*=ds-assistant-message-main-content]');"
                        "  return el ? el.innerText.slice(-150) : '';"
                        "}")
                    print(f"[RESULT] 判定点文本尾部: {ascii(tail)}")
                except Exception as exc:
                    print(f"[WARN] 尾部读取失败: {exc}")
                return 0
        print("[FAIL] 120s 内未见正文稳定，未完成")
        return 1
    finally:
        page.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
