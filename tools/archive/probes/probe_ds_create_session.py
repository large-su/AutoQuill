import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config
config.BROWSER_HEADLESS = True
import applications.zhihu_story.browser_adapter  # noqa
from web_drivers.browser_pool import get_browser, close_shared_browser

b = get_browser()
page = b.context.new_page()
page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=45000)
page.wait_for_timeout(6000)
tok = json.loads(page.evaluate("() => localStorage.getItem('userToken')")).get("value")
r = page.request.post("https://chat.deepseek.com/api/v0/chat_session/create",
                      data=json.dumps({}),
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + tok})
print("HTTP", r.status)
print(r.text()[:800])
page.close(); close_shared_browser()
