# 临时只读探测：DeepSeek 网页端「会话列表 / 会话内容」接口真机校准
#
# 目的：为「清理 30 天前写故事链路残留会话」工具确定真实接口与字段。
#   1. 打开 chat.deepseek.com（复用 data/browser_profile 登录态）
#   2. 监听 SPA 自身发出的 /api/ 请求（侧栏会话列表就是它自己拉的）
#   3. 滚动侧栏触发分页，收集分页参数
#   4. 打印每个接口的 method/path/请求体/响应体（脱敏，只读，不发消息不删会话）
#
# 用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/archive/probes/probe_ds_session_list.py
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import config  # noqa: E402
config.BROWSER_HEADLESS = True          # 探测走无头，不弹窗打扰

import applications.zhihu_story.browser_adapter  # noqa: F401,E402  注册浏览器工厂
from web_drivers.browser_pool import get_browser, close_shared_browser  # noqa: E402

SITE = "https://chat.deepseek.com/"

_SECRET = re.compile(r"[0-9a-fA-F]{24,}|eyJ[A-Za-z0-9._-]{20,}")


def redact(text, limit=800):
    if text is None:
        return ""
    s = str(text)
    s = _SECRET.sub("<redacted>", s)
    return s[:limit]


captured = []      # 请求记录
bodies = {}        # (method, path) -> 响应体样本


def on_request(req):
    try:
        url = req.url
        if "/api/" not in url:
            return
        captured.append({
            "method": req.method,
            "url": url,
            "post": redact(req.post_data, 600),
            "resource": req.resource_type,
        })
    except Exception:
        pass


def on_response(resp):
    try:
        url = resp.url
        if "/api/" not in url:
            return
        key = resp.request.method + " " + url
        if key in bodies:
            return
        try:
            text = resp.text()
        except Exception:
            text = "<no body>"
        bodies[key] = {"status": resp.status, "body": redact(text, 1500)}
    except Exception:
        pass


def main():
    browser = get_browser()
    page = browser.context.new_page()
    page.on("request", on_request)
    page.on("response", on_response)
    print("== 打开站点（无头） ==")
    page.goto(SITE, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(9000)

    print("url =", page.url)
    ls = page.evaluate(
        "() => { const out = {}; for (const k of Object.keys(localStorage)) {"
        " const v = localStorage.getItem(k) || '';"
        " out[k] = /token|user/i.test(k) ? '<len=' + v.length + '>' : v.slice(0, 60); }"
        " return out; }")
    print("localStorage keys =", json.dumps(ls, ensure_ascii=False)[:600])

    # 侧栏滚动触发分页（会话多时会持续拉下一页）
    print("== 滚动侧栏触发分页 ==")
    for i in range(6):
        page.mouse.move(120, 400)
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1500)
    page.wait_for_timeout(2000)

    print("\n== 捕获到的 /api/ 请求（去重） ==")
    seen = set()
    for c in captured:
        path = c["url"].split("?")[0]
        key = c["method"] + " " + path
        if key in seen:
            continue
        seen.add(key)
        print(f"- {c['method']} {path}")
        if c["post"]:
            print(f"    post: {c['post']}")

    print("\n== 响应体样本 ==")
    for key, info in bodies.items():
        print(f"\n--- {key}  [HTTP {info['status']}]")
        print(info["body"][:1200])

    # 侧栏 DOM 结构（兜底方案用）
    print("\n== 侧栏 DOM 样本 ==")
    dom = page.evaluate(
        "() => { const as = Array.from(document.querySelectorAll(\"a[href*='/a/chat/s/']\"));"
        " return {count: as.length,"
        "  items: as.slice(0, 12).map(a => ({href: a.getAttribute('href'),"
        "    text: (a.innerText || '').replace(/\\s+/g, ' ').slice(0, 60),"
        "    parent: (a.parentElement ? a.parentElement.className : '')}))}; }")
    print(json.dumps(dom, ensure_ascii=False, indent=1)[:2000])

    page.close()
    close_shared_browser()
    print("\n== 探测结束（未发送任何消息、未删除任何会话） ==")


if __name__ == "__main__":
    main()
