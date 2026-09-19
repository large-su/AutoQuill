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
            hdrs = req.all_headers()
        except Exception:
            hdrs = {}
        records.append({"url": req.url, "method": req.method,
                        "headers": {k: v for k, v in hdrs.items()
                                    if k.lower() in ("accept", "content-type",
                                                     "x-ds-request-id",
                                                     "x-hif-request-id",
                                                     "authorization",
                                                     "referer", "x-requested-with",
                                                     "priority", "x-ds-version")}})

b = get_browser()
page = b.context.new_page()
page.on("request", on_request)
page.goto(SITE, wait_until="domcontentloaded", timeout=45000)
page.wait_for_timeout(8000)
for i in range(4):
    page.mouse.move(120, 400)
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(1200)
page.wait_for_timeout(2000)

print("== 站点自身分页请求 ==")
for r in records[:6]:
    print(r["method"], r["url"])
    print("   headers:", json.dumps(r["headers"], ensure_ascii=False)[:400])

# 用同样的 URL 从页面内 fetch（同源 + cookie）对比
if records:
    target = records[-1]["url"]
    tok = json.loads(page.evaluate("() => localStorage.getItem('userToken')")).get("value")
    js = """async (args) => {
      const r = await fetch(args.url, {headers: {'authorization': 'Bearer ' + args.tok}});
      const d = await r.json();
      const biz = ((d.data || {}).biz_data) || {};
      const items = biz.chat_sessions || [];
      return {status: r.status, n: items.length, more: biz.has_more,
              first: items[0] ? items[0].id.slice(0,8) + ' ' + items[0].updated_at : null,
              last: items.length ? items[items.length-1].id.slice(0,8) + ' ' + items[items.length-1].updated_at : null};
    }"""
    print("\n[page.fetch 同 URL]", json.dumps(page.evaluate(js, {"url": target, "tok": tok}), ensure_ascii=False))

    r2 = page.request.get(target, headers={"Authorization": "Bearer " + tok}, timeout=30000)
    d2 = r2.json()
    biz2 = ((d2.get("data") or {}).get("biz_data") or {})
    it2 = biz2.get("chat_sessions") or []
    print("[page.request 同 URL] HTTP", r2.status, "n=", len(it2),
          "first=", it2[0]["id"][:8] if it2 else None,
          it2[0]["updated_at"] if it2 else None)
page.close(); close_shared_browser()
