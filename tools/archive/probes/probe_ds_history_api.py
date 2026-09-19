# 只读探测 #2：DeepSeek 会话「内容/消息」接口 + fetch_page 分页参数
#
# 目的：为清理工具确定「拉取单个会话消息以做写故事指纹确认」的真实接口。
#   1. 直接调 fetch_page 看完整响应结构（分页游标/是否有 has_more/页大小）
#   2. 打开一条会话页，监听 SPA 自己拉消息的 /api/ 请求
#   3. 用页面登录态（page.request）复现该请求，确认可对任意 session id 调用
# 全程只读：不发消息、不删会话。
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
config.BROWSER_HEADLESS = True

import applications.zhihu_story.browser_adapter  # noqa: F401,E402
from web_drivers.browser_pool import get_browser, close_shared_browser  # noqa: E402

SITE = "https://chat.deepseek.com/"
SAMPLE_SESSION = sys.argv[1] if len(sys.argv) > 1 else ""

_SECRET = re.compile(r"[0-9a-fA-F]{24,}|eyJ[A-Za-z0-9._-]{20,}")


def redact(text, limit=1200):
    if text is None:
        return ""
    return _SECRET.sub("<redacted>", str(text))[:limit]


def token(page):
    raw = page.evaluate("() => localStorage.getItem('userToken') || ''")
    try:
        return json.loads(raw).get("value") or ""
    except Exception:
        return raw or ""


def main():
    browser = get_browser()
    page = browser.context.new_page()
    captured = []

    def on_request(req):
        if "/api/" in req.url:
            captured.append((req.method, req.url.split("?")[0],
                             redact(req.post_data, 300), req.url))

    page.on("request", on_request)
    page.goto(SITE, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(7000)
    tok = token(page)
    print("userToken 长度 =", len(tok))

    # --- 1. fetch_page 完整结构 ---
    resp = page.request.get(
        SITE + "api/v0/chat_session/fetch_page?lte_cursor.pinned=false",
        headers={"Authorization": "Bearer " + tok})
    data = resp.json()
    biz = ((data.get("data") or {}).get("biz_data") or {})
    print("\n[fetch_page] HTTP", resp.status, "顶层键 =", list(data.keys()))
    print("[fetch_page] biz_data 键 =", list(biz.keys()))
    sessions = biz.get("chat_sessions") or []
    print("[fetch_page] 首屏条数 =", len(sessions))
    if sessions:
        print("[fetch_page] 单条字段 =", json.dumps(sessions[0], ensure_ascii=False))
    sid = SAMPLE_SESSION or (sessions[0]["id"] if sessions else "")
    print("[fetch_page] 其余字段 =",
          json.dumps({k: v for k, v in biz.items() if k != "chat_sessions"},
                     ensure_ascii=False)[:400])

    # --- 2. 打开一条会话，看 SPA 自己怎么拉消息 ---
    if sid:
        captured.clear()
        url = f"{SITE}a/chat/s/{sid}"
        print("\n== 打开会话页 ==", url)
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(8000)
        print("页面 URL =", page.url)
        for m, p, post, full in captured:
            if "fetch_page" in p or "client/settings" in p:
                continue
            print(f"- {m} {full[:200]}")
            if post:
                print("    post:", post)

        # 3. 用登录态复现（对任意 session id）
        for path in ("/api/v0/chat/history_messages",
                     "/api/v0/chat_session/history_messages"):
            for method in ("GET", "POST"):
                try:
                    if method == "GET":
                        r = page.request.get(
                            SITE.rstrip("/") + path + f"?chat_session_id={sid}",
                            headers={"Authorization": "Bearer " + tok})
                    else:
                        r = page.request.post(
                            SITE.rstrip("/") + path,
                            data=json.dumps({"chat_session_id": sid}),
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + tok})
                    body = redact(r.text(), 400)
                    print(f"[试探] {method} {path} -> HTTP {r.status} {body[:220]}")
                except Exception as exc:
                    print(f"[试探] {method} {path} 异常 {exc}")

    page.close()
    close_shared_browser()
    print("\n== 探测结束（只读） ==")


if __name__ == "__main__":
    main()
