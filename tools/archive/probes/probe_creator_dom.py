# -*- coding: utf-8 -*-
# ============================================================
# probe_creator_dom.py — 知乎创作中心（已发布 / 草稿箱）页面结构探测
#
# 用途：看板/草稿箱「刷新失败」（抓取 0 条）时，先跑本脚本看真实 DOM：
#   - 页面最终 URL 与标题（是否被重定向到登录页/新版页面）
#   - 候选卡片类名统计（旧选择器 .CreationManage-CreationCard 还在不在）
#   - 首张卡片的 outerHTML 片段（据此改 _EXTRACT_JS）
#
# 用法（源码态；复用安装版登录态）：
#   AQ_DATA_DIR="%APPDATA%/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_creator_dom.py
# 输出：控制台摘要 + data/cleanup/creator_dom_<ts>.json（原始结构）
# ============================================================

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

CHECKS = [
    ".CreationManage-CreationCard",
    "[class*=CreationManage]",
    "[class*=CreationCard]",
    "[class*=CreationManage] a[href*='/answer/']",
    "a[href*='/answer/']",
    "a[href*='#write']",
]

DUMP_JS = r"""
(checks) => {
  const counts = {};
  checks.forEach(s => { try { counts[s] = document.querySelectorAll(s).length; } catch (e) { counts[s] = -1; } });
  const classCount = {};
  document.querySelectorAll("*").forEach(el => {
    (el.className && typeof el.className === "string" ? el.className : "").split(/\s+/).forEach(c => {
      if (c) classCount[c] = (classCount[c] || 0) + 1;
    });
  });
  const top = Object.entries(classCount).sort((a, b) => b[1] - a[1]).slice(0, 40);
  const cand = Object.entries(classCount)
      .filter(([c]) => /Card|Creation|Manage|Draft|List|Item/i.test(c))
      .sort((a, b) => b[1] - a[1]).slice(0, 40);
  const first = document.querySelector("[class*=Card]");
  return {
    url: location.href,
    title: document.title,
    counts: counts,
    top_classes: top,
    card_like_classes: cand,
    body_text: (document.body.innerText || "").slice(0, 900),
    first_card_html: first ? first.outerHTML.slice(0, 3000) : "",
  };
}
"""

URLS = {
    "published": "https://www.zhihu.com/creator/manage/creation/answer",
    "drafts": "https://www.zhihu.com/creator/manage/creation/draft?type=answer",
}


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        for name, url in URLS.items():
            b.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            b._safe_evaluate("() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(3)
            info = b._safe_evaluate(DUMP_JS, CHECKS) or {}
            out[name] = info
            print("=" * 60)
            print("[%s] url=%s" % (name, info.get("url")))
            print("       title=%s" % info.get("title"))
            print("       counts=%s" % info.get("counts"))
            print("       card-like classes=%s" % info.get("card_like_classes")[:12])
            print("       body[:200]=%s" % (info.get("body_text") or "")[:200].replace(chr(10), " / "))
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "creator_dom_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("=" * 60)
    print("原始结构已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
