# ============================================================
# probe_profile_web.py — 实测 Web 通道剖析失败根因（真实浏览器）
#
# 复现用户报告：文风提炼走 Web 通道，「文本稳定 2 轮」判定完成后
# 读回的剖析 JSON 解析失败（两次都失败，2842/2760 字符）。
#
# 本脚本用真实浏览器完整复现剖析流程，逐秒记录：
#   - 正文长度 / 思考长度（文本是否在 ~2800 处暂停）
#   - 停止按钮逐个候选 selector 的命中情况（selector 是否失效）
#   - 暂停期间停止按钮是否还在（流式暂停 vs 真的完成）
#   - 读回全文尾部（是否残缺 JSON）
#
# 运行：python tools/probe_profile_web.py
# 注意：控制台输出保持 ASCII（GBK 安全）
# ============================================================

import asyncio  # noqa: F401  (align with repo probe style)
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

_STOP_SELECTORS = (
    # 新版 UI（2026-08-15 实测）：发送/停止是同一圆形 primary DIV，
    # 生成中移除 ds-button--disabled（停止态），完成恢复
    "div[class*='ds-button--circle'][class*='ds-button--primary']"
    ":not([class*='ds-button--disabled'])",
    "button[aria-label*='stop']",
    "button[data-testid*='stop']",
    "button[class*='stop']",
    "div[role=button][class*='stop']",
    "div[aria-label*='stop']",
)
_RESULT_SELECTORS = (
    "div[class*='ds-assistant-message-main-content']",
    "div[class*='message'] div[class*='markdown']",
    "div[class*='ds-markdown']",
    "div[class*='assistant'] div[class*='markdown']",
)


def _build_prompt():
    """用本机采集库真实数据构建剖析 prompt（截断至 10 篇贴近复现）。"""
    from applications.zhihu_story.author_profiler import (
        compute_text_stats, load_author_stories,
        _format_consistency_for_prompt, _format_stats_for_prompt,
        _format_stories_for_prompt)
    from applications.zhihu_story.prompts import AUTHOR_PROFILE_PROMPT
    authors = set()
    for line in open("data/collected_stories.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        a = r.get("author")
        if a:
            authors.add(a)
    author = sorted(authors)[0]
    stories = load_author_stories(author)[:10]
    stats = compute_text_stats(stories)
    return AUTHOR_PROFILE_PROMPT.format(
        author=author,
        text_stats=_format_stats_for_prompt(stats),
        consistency=_format_consistency_for_prompt(stats),
        stories=_format_stories_for_prompt(stories),
    ), author, stories


def _probe_sel(page, candidates, attr="tagName"):
    js = (
        "async function() {"
        "  for (const s of arguments[0]) {"
        "    const el = document.querySelector(s);"
        "    if (el) return {sel: s, val: el.%s};"
        "  }"
        "  return {sel: null, val: null};"
        "}" % attr
    )
    try:
        r = page.evaluate(js, list(candidates)) or {}
        return r.get("sel"), r.get("val")
    except Exception:
        return None, None


def main():
    from applications.zhihu_story.browser_adapter import get_browser
    browser = get_browser()
    page = browser.context.new_page()
    try:
        page.goto("https://chat.deepseek.com", wait_until="domcontentloaded",
                  timeout=30000)
        page.wait_for_timeout(2500)
        if "sign_in" in page.url:
            print("[FAIL] 本机 DeepSeek 未登录，无法实测。"
                  "先手动登录一次再运行。")
            return 1
        print("[OK] 已登录 chat.deepseek.com")

        prompt, author, stories = _build_prompt()
        print(f"[INFO] 剖析作者={author} 篇数={len(stories)} "
              f"prompt={len(prompt)} chars")

        inp, _ = _probe_sel(page, (
            "textarea#chat-input",
            "textarea[data-testid='chat_input_input']",
            "textarea[placeholder*='给 DeepSeek']",
            "div[contenteditable='true']",
        ))
        if not inp:
            print("[FAIL] 找不到输入框")
            return 1
        page.locator(inp).fill(prompt)
        page.keyboard.press("Enter")
        print("[INFO] prompt 已发送，开始轮询…")

        start = time.time()
        last_len = -1
        stable = 0
        stop_hits = set()
        dumped_buttons = False
        while time.time() - start < 150:
            cur_sel, cur_text = _probe_sel(page, _RESULT_SELECTORS,
                                           attr="innerText")
            stop_sel, _ = _probe_sel(page, _STOP_SELECTORS)
            think_sel, think_text = _probe_sel(
                page, ("div[class*='ds-think-content']",
                       "div[class*='ds-think']"), attr="innerText")
            cur_len = len(cur_text or "")
            if stop_sel:
                stop_hits.add(stop_sel)
            state = "?"
            if stop_sel:
                state = "STOP-BTN-ON"
            elif last_len >= 0 and cur_len == last_len:
                stable += 1
                state = f"stable x{stable}"
            else:
                stable = 0
            print(f"  t={time.time() - start:6.1f}s len={cur_len:5d} "
                  f"think={len(think_text or ''):5d} {state} "
                  f"btn={stop_sel or '-'}")
            if cur_len > 100 and not dumped_buttons:
                # 生成中 dump 一次按钮 DOM，找新 UI 停止按钮形态
                dumped_buttons = True
                try:
                    buttons = page.evaluate(
                        "() => Array.from(document.querySelectorAll('button'))"
                        ".slice(0, 25).map(b => ({"
                        "  cls: (b.className || '').toString().slice(0, 60),"
                        "  aria: b.getAttribute('aria-label') || '',"
                        "  text: (b.innerText || '').trim().slice(0, 12),"
                        "  child: b.firstElementChild ? "
                        "b.firstElementChild.tagName : ''}))")
                    for b in buttons:
                        print(f"    <button> cls={b['cls']!r} "
                              f"aria={b['aria']!r} text={b['text']!r} "
                              f"child={b['child']}")
                except Exception as exc:
                    print(f"    button dump 失败: {exc}")
            last_len = cur_len
            if not stop_sel and last_len > 0 and stable >= 2:
                # 停止按钮消失 + 文本稳定 2 轮 = 线上 wait_complete 判定点
                sel0, text0 = _probe_sel(page, _RESULT_SELECTORS,
                                         attr="innerText")
                t0 = (text0 or "").strip()
                print(f"[INFO] 判定点立即读回: len={len(t0)}")
                print(f"[INFO] 判定点尾部: {ascii(t0[-120:])}")
                print("[INFO] 判定点后再轮询 15s，观察输出是否继续增长…")
                final_len = len(t0)
                grew = False
                for _ in range(15):
                    page.wait_for_timeout(1000)
                    _, t2 = _probe_sel(page, _RESULT_SELECTORS,
                                       attr="innerText")
                    n2 = len(t2 or "")
                    if n2 != final_len:
                        final_len = n2
                        grew = True
                        print(f"  [WARN] 判定后输出仍在增长 → len={n2}")
                    _, s2 = _probe_sel(page, _STOP_SELECTORS)
                    if s2:
                        print(f"  [WARN] 判定后停止按钮又出现 → {s2}")
                _, t3 = _probe_sel(page, _RESULT_SELECTORS, attr="innerText")
                t3 = (t3 or "").strip()
                if t3 != t0:
                    grew = True
                print(f"[RESULT] 判定后最终 len={len(t3)} "
                      f"{'[WARN] 内容变化：判定点读回的是中间态' if grew else '[OK] 判定点读回即最终内容'}")
                if t3 != t0:
                    print(f"[RESULT] 判定点尾部: {ascii(t0[-120:])}")
                    print(f"[RESULT] 最终尾部: {ascii(t3[-120:])}")
                break
            page.wait_for_timeout(1000)

        # 读回全文
        sel, text = _probe_sel(page, _RESULT_SELECTORS, attr="innerText")
        full = (text or "").strip()
        print(f"\n[RESULT] 命中容器={sel} 总长={len(full)}")
        print(f"[RESULT] 停止按钮命中候选: {sorted(stop_hits) or '（从未命中）'}")
        tail = ascii(full[-200:])
        head = ascii(full[:80])
        print(f"[RESULT] 头部: {head}")
        print(f"[RESULT] 尾部: {tail}")
        profile = None
        try:
            cleaned = full.strip()
            if cleaned.startswith("```"):
                parts = cleaned.split("\n", 1)
                cleaned = parts[1] if len(parts) > 1 else ""
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].rstrip()
            start_i, end_i = cleaned.find("{"), cleaned.rfind("}")
            if start_i >= 0 and end_i > start_i:
                profile = json.loads(cleaned[start_i:end_i + 1])
        except json.JSONDecodeError as exc:
            print(f"[FAIL] json.loads 失败: {exc}")
        except Exception as exc:
            print(f"[FAIL] 解析异常: {exc}")
        if profile:
            print(f"[OK] 完整 JSON 解析成功: {len(profile)} 字段")
            return 0
        print("[FAIL] JSON 解析失败（与线上一致的失败点）")
        return 1
    finally:
        page.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
