# 只读探测 #3：会话消息结构 + AutoQuill 指纹取样
#
# 取样几条「像写故事」的历史会话，打印消息结构与用户消息开头，
# 供清理工具的指纹规则标定。只读。
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import config  # noqa: E402
config.BROWSER_HEADLESS = True

import applications.zhihu_story.browser_adapter  # noqa: F401,E402
from web_drivers.browser_pool import get_browser, close_shared_browser  # noqa: E402

SITE = "https://chat.deepseek.com/"
SAMPLES = sys.argv[1:] or [
    "6c12a5b4-1011-4f49-9438-658aca89ffe6",   # 恋爱脑小姐嫁鬼
    "24744ad2-8280-4baa-9434-5e972362a178",   # 故事潜力判断
    "ce53d5ea-dc41-43c7-956d-121ab9583373",   # 故事潜力评分
    "65108e7d-05f4-4867-9cfe-034a5ad9e377",   # 故事潜力评分2
    "105e094a-1df2-443d-90e3-0f3a86b821c7",   # 口腔医生甜文
]


def token(page):
    raw = page.evaluate("() => localStorage.getItem('userToken') || ''")
    try:
        return json.loads(raw).get("value") or ""
    except Exception:
        return raw or ""


def main():
    browser = get_browser()
    page = browser.context.new_page()
    page.goto(SITE, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(6000)
    tok = token(page)
    hdr = {"Authorization": "Bearer " + tok}

    for sid in SAMPLES:
        r = page.request.get(
            SITE + "api/v0/chat/history_messages?chat_session_id=" + sid,
            headers=hdr)
        try:
            data = r.json()
        except Exception:
            print(sid, "HTTP", r.status, "非 JSON"); continue
        biz = ((data.get("data") or {}).get("biz_data") or {})
        sess = biz.get("chat_session") or {}
        msgs = biz.get("chat_messages") or []
        print("=" * 70)
        print(f"session {sid} | HTTP {r.status} | title={sess.get('title')!r} "
              f"| updated_at={sess.get('updated_at')} | msgs={len(msgs)}")
        print("biz_data 键 =", list(biz.keys()))
        if msgs:
            print("message 字段 =", list(msgs[0].keys()))
        for m in msgs[:4]:
            role = m.get("role")
            content = str(m.get("content") or "")
            think = str(m.get("thinking_content") or "")
            print(f"  - role={role} len={len(content)} think_len={len(think)}"
                  f" files={len(m.get('files') or [])}")
            if role == "USER":
                print("    USER 前 700 字：")
                print("    " + content[:700].replace("\n", "\n    "))
        print()

    page.close()
    close_shared_browser()


if __name__ == "__main__":
    main()
