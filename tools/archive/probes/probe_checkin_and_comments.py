# -*- coding: utf-8 -*-
# ============================================================
# probe_checkin_and_comments.py - 打卡挑战页 + 评论管理页 DOM 探测
#
# 用途：为「打卡挑战」与「评论回复」两个新自动化任务做真机探针。
#   页面1：https://www.zhihu.com/parker/campaign/<id>（打卡挑战）
#   页面2：https://www.zhihu.com/creator/manage/comment/answer（评论管理）
# 产出：控制台摘要 + data/cleanup/checkin_comments_<ts>.json（原始结构）
#
# 用法（源码态；复用安装版登录态）：
#   AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python tools/archive/probes/probe_checkin_and_comments.py
# ============================================================

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CAMPAIGN_URL = ("https://www.zhihu.com/parker/campaign/2083977679355889635"
                "?zh_hide_nav_bar=true")
COMMENT_URL = "https://www.zhihu.com/creator/manage/comment/answer"

DUMP_JS = """
() => {
  const vis = el => !!(el && el.offsetParent !== null);
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const node = el => ({ tag: el.tagName, text: txt(el).slice(0, 60), cls: cls(el).slice(0, 90), href: el.getAttribute("href") || "" });
  const classCount = {};
  document.querySelectorAll("*").forEach(el => {
    cls(el).split(" ").forEach(c => { if (c) classCount[c] = (classCount[c] || 0) + 1; });
  });
  const cand = Object.entries(classCount)
      .filter(([c]) => /Card|Task|Mission|Item|Campaign|Parker|Progress|Reward|Comment|Reply|List/i.test(c))
      .sort((a, b) => b[1] - a[1]).slice(0, 50);
  const buttons = Array.from(document.querySelectorAll("button,[role=button],a[href]"))
      .filter(vis).map(node).filter(b => b.text || b.href).slice(0, 80);
  return {
    url: location.href,
    title: document.title,
    body_text: (document.body ? (document.body.innerText || "") : "").slice(0, 6000),
    class_like: cand,
    buttons: buttons,
    answer_links: Array.from(document.querySelectorAll("a[href*='/answer/']")).filter(vis).map(node).slice(0, 20),
    people_links: Array.from(document.querySelectorAll("a[href*='/people/']")).filter(vis).map(node).slice(0, 20),
    html_head: (document.body ? document.body.innerHTML : "").slice(0, 6000)
  };
}
"""

SCROLL_JS = "() => { window.scrollTo(0, document.body.scrollHeight); return true; }"


def grab(b, url, wait=8, scrolls=3):
    b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(wait)
    for _ in range(scrolls):
        b._safe_evaluate(SCROLL_JS)
        time.sleep(1.5)
    return b._safe_evaluate(DUMP_JS) or {}


def show(name, info):
    print("=" * 70)
    print("[%s] url=%s" % (name, info.get("url")))
    print("      title=%s" % info.get("title"))
    print("      class-like=%s" % info.get("class_like")[:15])
    print("      buttons=%s" % [(b.get("text"), b.get("cls")[:30]) for b in (info.get("buttons") or [])[:25]])
    print("      body_text:")
    print((info.get("body_text") or "")[:2500])


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        print("登录态：%s" % b.is_logged_in())
        out["campaign"] = grab(b, CAMPAIGN_URL)
        out["comments"] = grab(b, COMMENT_URL, scrolls=5)
        outdir = os.path.join("data", "cleanup")
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "checkin_comments_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("原始结构已存：%s" % path)
        show("campaign", out["campaign"])
        show("comments", out["comments"])
    finally:
        b.close()
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())