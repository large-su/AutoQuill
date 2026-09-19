# -*- coding: utf-8 -*-
"""诊断：用 browser_state.json 里的登录态试访问知乎，判断"会话失效"还是"profile 里的 cookie 旧"。"""
import json, os, sys, time
sys.path.insert(0, os.getcwd())
from playwright.sync_api import sync_playwright
from applications.zhihu_story.browser_utils import STORAGE_STATE_PATH
print("state =", STORAGE_STATE_PATH, os.path.exists(STORAGE_STATE_PATH))
state = json.load(open(STORAGE_STATE_PATH, encoding="utf-8"))
cks = state.get("cookies", [])
print("cookies =", len(cks))
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context()
    ctx.add_cookies(cks)
    page = ctx.new_page()
    for url in ("https://www.zhihu.com/", "https://www.zhihu.com/creator/manage/creation/answer"):
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        info = page.evaluate("() => ({url: location.href, cards: document.querySelectorAll('.CreationManage-CreationCard').length, any: document.querySelectorAll('[class*=Card]').length})")
        print("%-52s -> %s | cards=%s any=%s" % (url[:52], info["url"][:70], info["cards"], info["any"]))
    b.close()
