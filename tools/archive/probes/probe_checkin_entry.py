# -*- coding: utf-8 -*-
# ============================================================
# probe_checkin_entry.py - 打卡挑战「当期入口」发现 + 今日任务解析原型
#
# 目的：打卡挑战每月换一期（新 campaign id），需要知道能不能自动发现；
#       同时验证「今日任务」条目解析（标题/状态/按钮）是否稳定可读。
# 全部只读，不点任何按钮。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_checkin_entry.py
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

# 候选入口页：创作中心各页面 + parker 根路径
ENTRY_URLS = [
    "https://www.zhihu.com/creator/manage/creation/all",
    "https://www.zhihu.com/creator",
    "https://www.zhihu.com/parker",
    "https://www.zhihu.com/creator/manage/creation/answer",
]

LINKS_JS = """
() => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const links = Array.from(document.querySelectorAll("a[href]")).map(a => ({
    text: txt(a).slice(0, 30),
    href: a.getAttribute("href") || ""
  })).filter(l => /campaign|parker|checkin|clockin|daka/i.test(l.href) || /打卡/.test(l.text));
  const seen = new Set();
  const uniq = links.filter(l => { if (seen.has(l.href)) return false; seen.add(l.href); return true; });
  return { url: location.href, title: document.title, links: uniq.slice(0, 25),
           body: ((document.body.innerText || "") || "").slice(0, 600) };
}
"""

# 今日任务解析原型：tasktree-item → {title, desc, status, action}
TASKS_JS = """
() => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const items = Array.from(document.querySelectorAll("[class*=tasktree-item]")).map(it => {
    const title = it.querySelector("[class*=tasktree-task-title], [class*=tasktree-title]");
    const desc = it.querySelector("[class*=tasktree-desc]");
    const btn = it.querySelector("[class*=tasktree-btn]");
    return { cls: (typeof it.className === "string" ? it.className : "").slice(0, 60),
             title: txt(title), desc: txt(desc), btn: txt(btn) };
  });
  const groups = Array.from(document.querySelectorAll("[class*=tasktree]")).map(el => txt(el).slice(0, 40))
      .filter(t => /任务/.test(t)).slice(0, 10);
  return { url: location.href, title: document.title, items: items, groups: groups,
           body: ((document.body.innerText || "") || "").slice(0, 900) };
}
"""

CAMPAIGN = "https://www.zhihu.com/parker/campaign/2083977679355889635?zh_hide_nav_bar=true"


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {"entries": [], "campaign": {}}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        for url in ENTRY_URLS:
            try:
                b.page.goto(url, wait_until="domcontentloaded", timeout=40000)
                time.sleep(5)
                info = b._safe_evaluate(LINKS_JS) or {}
            except Exception as exc:
                info = {"url": url, "error": str(exc)[:120]}
            out["entries"].append(info)
            print("--- 入口", url)
            print("    final:", info.get("url"), "|", info.get("title"))
            for l in info.get("links") or []:
                print("      link:", l.get("text"), "->", l.get("href")[:90])
        b.page.goto(CAMPAIGN, wait_until="domcontentloaded", timeout=40000)
        time.sleep(8)
        out["campaign"] = b._safe_evaluate(TASKS_JS) or {}
        c = out["campaign"]
        print("=== 今日任务解析原型 ===")
        for it in c.get("items") or []:
            print("    [%s] %s | %s | 按钮=%s" % (it.get("cls")[:28], it.get("title"), it.get("desc"), it.get("btn")))
        print("=== 分组 ===", c.get("groups"))
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "checkin_entry_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("原始结构已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())