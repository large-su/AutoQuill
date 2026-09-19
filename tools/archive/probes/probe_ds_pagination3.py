import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config
config.BROWSER_HEADLESS = True
import applications.zhihu_story.browser_adapter  # noqa
from web_drivers.browser_pool import get_browser, close_shared_browser

SITE = "https://chat.deepseek.com/"
records = []

def on_request(req):
    if "fetch_page" in req.url:
        try:
            hdrs = dict(req.all_headers())
        except Exception:
            hdrs = {}
        hdrs.pop("cookie", None)
        for k in [k for k in hdrs if k.startswith(":")]:
            hdrs.pop(k, None)
        records.append({"url": req.url, "method": req.method, "headers": hdrs})

b = get_browser()
page = b.context.new_page()
page.on("request", on_request)
page.goto(SITE, wait_until="domcontentloaded", timeout=45000)
page.wait_for_timeout(8000)
for i in range(3):
    page.mouse.move(120, 400)
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(1500)

for r in records[:4]:
    print("URL:", r["url"])
    print("  ALL headers:", json.dumps(r["headers"], ensure_ascii=False))
    print()

# 逐个复现：先原样带全部 header，再只带 authorization
if records:
    tgt = records[-1]
    tok = json.loads(page.evaluate("() => localStorage.getItem('userToken')")).get("value")
    for label, hdrs in (("原样全部 header", {k: v for k, v in tgt["headers"].items()
                                             if k.lower() not in ("authorization",
                                                                  "host", "content-length")}),
                        ("只 authorization", {})):
        h = dict(hdrs)
        h["authorization"] = "Bearer " + tok
        r = page.request.get(tgt["url"], headers=h, timeout=30000)
        try:
            d = r.json()
            biz = ((d.get("data") or {}).get("biz_data") or {})
            it = biz.get("chat_sessions") or []
            print("[%s] HTTP %s n=%s first=%s %s" % (
                label, r.status, len(it),
                it[0]["id"][:8] if it else None,
                it[0]["updated_at"] if it else None))
        except Exception as exc:
            print("[%s] HTTP %s 解析失败 %s" % (label, r.status, exc))
page.close(); close_shared_browser()
