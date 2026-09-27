# -*- coding: utf-8 -*-
# ============================================================
# probe_interact_dom.py - 回答页「关注 / 赞同」按钮 DOM 只读探测
#
# 目标（只读，不点任何按钮）：回答页上
#   1) 参考回答作者的「关注」按钮：类名 / 文本（关注 / 已关注 / 互相关注）；
#   2) 「赞同」按钮：类名 / 文本 / 是否已赞同（VoteButton--active 之类）；
#   3) 作者链接（/people/xxx）→ 用于判重与后续关注。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_interact_dom.py [问题URL]
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

DEFAULT_URL = "https://www.zhihu.com/question/1988024921025168911"

JS = """
() => {
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const vis = el => !!(el && el.offsetParent !== null);
  const items = Array.from(document.querySelectorAll("[class*=AnswerItem],.List-item")).slice(0, 6).map(it => {
    const author = it.querySelector("a[href*='/people/']");
    const follows = Array.from(it.querySelectorAll("[class*=Follow],[class*=follow]")).filter(vis)
        .map(el => ({ tag: el.tagName, text: txt(el).slice(0, 20), cls: cls(el).slice(0, 90) }));
    const votes = Array.from(it.querySelectorAll("[class*=VoteButton],[class*=Vote]")).filter(vis)
        .map(el => ({ tag: el.tagName, text: txt(el).slice(0, 20), cls: cls(el).slice(0, 120) }));
    return { author: author ? { name: txt(author), href: author.getAttribute("href") } : null,
             follows: follows, votes: votes };
  });
  const peopleAll = Array.from(document.querySelectorAll("a[href*='/people/']")).filter(vis).slice(0, 8)
      .map(a => ({ name: txt(a).slice(0, 20), href: a.getAttribute("href") }));
  return { url: location.href, title: document.title, items: items, people: peopleAll,
           body: ((document.body.innerText || "") || "").slice(0, 1200) };
}
"""


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    b = ZhihuBrowser(headless=True)
    out = {}
    try:
        b.start()
        b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        out = b._safe_evaluate(JS) or {}
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "interact_dom_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("URL:", out.get("url"))
    print("TITLE:", out.get("title"))
    for i, it in enumerate(out.get("items") or []):
        print("--- answer", i, "author=", it.get("author"))
        print("    follows:", it.get("follows"))
        print("    votes:", it.get("votes"))
    print("people:", out.get("people"))
    print("原始结构已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())