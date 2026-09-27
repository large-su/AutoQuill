# -*- coding: utf-8 -*-
# ============================================================
# probe_follow_verify.py - 验证关注/赞同是否真落到服务端（重新加载 + 换页看）
#
# 上一轮探针（probe_follow_vote.py）做了真实动作：关注→取关→关注、
# 赞同→取消赞同→赞同。这一轮只读验证：重新打开页面，看状态是否保持；
# 并顺手摸清「作者名/主页链接」的可靠取法（去重台账要用）。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_follow_verify.py [问题URL]
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

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.zhihu.com/question/8692943577"

JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").replace(/[ ]+/g, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  const out = items.slice(0, 3).map((it, i) => {
    const links = Array.from(it.querySelectorAll("a[href*='/people/']"));
    const named = links.filter(a => txt(a).length > 0);
    const f = it.querySelector("button.FollowButton");
    const v = it.querySelector("button.VoteButton");
    const ans = it.querySelector("a[href*='/answer/']");
    return { i: i, author: named.length ? txt(named[0]).slice(0, 20) : "",
             authorHref: named.length ? named[0].getAttribute("href") : "",
             follow: f ? txt(f) : "(无)", vote: v ? txt(v) : "(无)",
             answerHref: ans ? ans.getAttribute("href") : "" };
  });
  const who = Array.from(document.querySelectorAll("a[href*='/people/']"))
      .filter(a => a.closest("[class*=AppHeader],[class*=AppHeader-profile],header"))
      .map(a => ({ text: txt(a).slice(0, 20), href: a.getAttribute("href") })).slice(0, 5);
  return { url: location.href, title: document.title, items: out, headerPeople: who };
}
"""


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {"url": URL}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(9)
        out["first_load"] = b._safe_evaluate(JS) or {}
        b.page.reload(wait_until="domcontentloaded", timeout=45000)
        time.sleep(9)
        out["after_reload"] = b._safe_evaluate(JS) or {}
    finally:
        b.close()
    for key in ("first_load", "after_reload"):
        d = out[key]
        print("=== %s === %s" % (key, (d.get("title") or "")[:50]))
        for it in d.get("items") or []:
            print("    #%s author=%s (%s) | 关注=%s | 赞同=%s | answer=%s" % (
                it.get("i"), it.get("author"), (it.get("authorHref") or "")[-24:], it.get("follow"), it.get("vote"), (it.get("answerHref") or "")[-24:]))
        print("    顶栏账号：", d.get("headerPeople"))
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "follow_verify_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())