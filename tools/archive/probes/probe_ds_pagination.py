import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config
config.BROWSER_HEADLESS = True
import applications.zhihu_story.browser_adapter  # noqa
from web_drivers.browser_pool import get_browser, close_shared_browser

BASE = "https://chat.deepseek.com/api/v0/chat_session/fetch_page"
b = get_browser()
page = b.context.new_page()
page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=45000)
page.wait_for_timeout(6000)
tok = json.loads(page.evaluate("() => localStorage.getItem('userToken')")).get("value")
H = {"Authorization": "Bearer " + tok}

def call(qs, label):
    url = BASE + (("?" + qs) if qs else "")
    r = page.request.get(url, headers=H, timeout=30000)
    try:
        d = r.json()
    except Exception:
        print(label, "HTTP", r.status, "非 JSON"); return
    biz = ((d.get("data") or {}).get("biz_data") or {})
    items = biz.get("chat_sessions") or []
    first = items[0] if items else {}
    last = items[-1] if items else {}
    print("%-58s HTTP %s n=%3d has_more=%s | first=%s %s | last=%s %s"
          % (label, r.status, len(items), biz.get("has_more"),
             str(first.get("id"))[:8], first.get("updated_at"),
             str(last.get("id"))[:8], last.get("updated_at")))
    return items

a = call("", "无参数")
cur = a[-1]["updated_at"] if a else 0
print("cursor =", repr(cur))
call("lte_cursor.pinned=false", "pinned=false")
call("lte_cursor.pinned=false&lte_cursor.updated_at=%.3f" % float(cur), "pinned=false + updated_at=%.3f")
call("lte_cursor.pinned=false&lte_cursor.updated_at=%s" % cur, "pinned=false + updated_at=%s(原样)")
call("lte_cursor.updated_at=%.3f" % float(cur), "只有 updated_at=%.3f")
call("lte_cursor.pinned=false&lte_cursor.updated_at=%.3f" % (float(cur) - 0.001), "updated_at 减 0.001")
call("count=100&lte_cursor.pinned=false&lte_cursor.updated_at=%.3f" % (float(cur) * 1000), "updated_at 毫秒?")
page.close(); close_shared_browser()
