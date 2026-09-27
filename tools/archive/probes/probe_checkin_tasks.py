# -*- coding: utf-8 -*-
# ============================================================
# probe_checkin_tasks.py - 打卡挑战「今日任务」条目结构 + 评论卡片结构
#
# 目标：拿到可用于写选择器的真实 DOM——
#   1) 打卡页每个任务条目的：标题、状态（已完成/未完成）、按钮元素；
#   2) 评论管理页每张评论卡片：作者、时间、正文、回复按钮。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_checkin_tasks.py
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

CAMPAIGN_URL = "https://www.zhihu.com/parker/campaign/2083977679355889635?zh_hide_nav_bar=true"
COMMENT_URL = "https://www.zhihu.com/creator/manage/comment/answer"

# 打卡页：把「今日任务」区域里所有条目连同后代元素摊平输出
CHECKIN_JS = """
() => {
  const desc = el => ({
    tag: el.tagName,
    cls: (typeof el.className === "string" ? el.className : "").slice(0, 100),
    text: ((el.innerText || el.textContent) || "").trim().slice(0, 80)
  });
  const items = Array.from(document.querySelectorAll("[class*=ClockInItem],[class*=tasktree-item],[class*=ClockInTitle]")).map(el => ({
    info: desc(el),
    kids: Array.from(el.querySelectorAll("*")).slice(0, 25).map(desc)
  }));
  const acts = Array.from(document.querySelectorAll("div,span,a,button,p")).filter(el => {
    const t = ((el.innerText || "") || "").trim();
    return ["去发布", "去评论", "去提问", "去收听", "去完成", "已完成"].includes(t);
  }).map(desc);
  const chain = el => {
    const out = [];
    let cur = el;
    for (let i = 0; i < 5 && cur; i++) { out.push(desc(cur)); cur = cur.parentElement; }
    return out;
  };
  return { url: location.href, title: document.title, items: items, actions: acts,
           action_chains: acts.slice(0, 8).map(a => ({ text: a.text, chain: null })).map((x, i) => x),
           chains: Array.from(document.querySelectorAll("div,span,a,button")).filter(el => ["去发布", "去评论", "去提问", "去收听"].includes(((el.innerText || "") || "").trim())).slice(0, 6).map(chain) };
}
"""

# 评论页：评论卡片结构化抽取 + 首卡 outerHTML
COMMENT_JS = """
() => {
  const cards = Array.from(document.querySelectorAll("[class*=CommentManage-CommentCard]"));
  const info = cards.slice(0, 12).map(c => ({
    text: ((c.innerText || "") || "").trim().slice(0, 300),
    cls: (typeof c.className === "string" ? c.className : ""),
    html: c.outerHTML.slice(0, 2500)
  }));
  const creation = Array.from(document.querySelectorAll("[class*=CommentManage-CreationCard]")).slice(0, 3).map(c => ({
    text: ((c.innerText || "") || "").trim().slice(0, 200),
    html: c.outerHTML.slice(0, 1500)
  }));
  return { url: location.href, title: document.title, count: cards.length, cards: info, creation: creation,
           body: ((document.body.innerText || "") || "").slice(0, 4000) };
}
"""

SCROLL_JS = "() => { window.scrollTo(0, document.body.scrollHeight); return true; }"


def grab(b, url, js, wait=8, scrolls=3):
    b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(wait)
    for _ in range(scrolls):
        b._safe_evaluate(SCROLL_JS)
        time.sleep(1.5)
    return b._safe_evaluate(js) or {}


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        print("登录态：%s" % b.is_logged_in())
        out["checkin"] = grab(b, CAMPAIGN_URL, CHECKIN_JS, scrolls=1)
        out["comments"] = grab(b, COMMENT_URL, COMMENT_JS, scrolls=4)
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "probe_tasks_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("原始结构已存：%s" % path)
    ci = out["checkin"]
    print("=== 打卡页 actions ===")
    for a in ci.get("actions") or []:
        print("   ", a.get("tag"), "|", a.get("text"), "|", a.get("cls"))
    print("=== 打卡页 items ===")
    for it in (ci.get("items") or [])[:14]:
        i = it.get("info") or {}
        print("   [%s] %s | %s" % (i.get("tag"), (i.get("cls") or "")[:60], (i.get("text") or "").replace(chr(10), " / ")))
    cm = out["comments"]
    print("=== 评论卡片 %s 张 ===" % cm.get("count"))
    for c in (cm.get("cards") or [])[:6]:
        print("   ---", (c.get("text") or "").replace(chr(10), " / ")[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())