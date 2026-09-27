# -*- coding: utf-8 -*-
# ============================================================
# probe_answer_comments2.py - 回答页评论区结构（第二轮：定位自己的回答 + 评论列表）
#
# 第一轮发现：点「N 条评论」会展开一个 Draft 编辑器与「发布」按钮，
# 但评论条目的真实类名没抓到（不是 CommentItem）。这一轮把结构摊开看。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_answer_comments2.py [回答URL]
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

DEFAULT_URL = "https://www.zhihu.com/answer/2087106680509166319"

WHOAMI_JS = """
() => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const links = Array.from(document.querySelectorAll("a[href*='/people/']"));
  const cands = links.slice(0, 12).map(a => ({ text: txt(a).slice(0, 20), href: a.getAttribute("href"),
      inHeader: !!a.closest("header,.AppHeader,[class*=AppHeader],[class*=AppHeader-profile]") }));
  return { title: document.title, people: cands };
}
"""

ANSWERS_JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  return items.slice(0, 5).map(it => {
    const author = it.querySelector("a[href*='/people/']");
    const vote = it.querySelector(".VoteButton");
    const commentBtn = Array.from(it.querySelectorAll("button")).find(b => /条评论/.test(txt(b)));
    const answerLink = it.querySelector("a[href*='/answer/']");
    return { author: author ? txt(author).slice(0, 20) : "",
             authorHref: author ? author.getAttribute("href") : "",
             vote: vote ? { text: txt(vote).slice(0, 20), cls: vote.className.slice(0, 90), disabled: !!vote.disabled } : null,
             commentBtn: commentBtn ? txt(commentBtn).slice(0, 20) : "",
             answerHref: answerLink ? answerLink.getAttribute("href") : "" };
  });
}
"""

CLICK_COMMENT_JS = """
(idx) => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  const it = items[idx];
  if (!it) return { ok: false, reason: "没有该回答" };
  const btn = Array.from(it.querySelectorAll("button")).find(b => /条评论/.test(txt(b)));
  if (!btn) return { ok: false, reason: "没有评论按钮" };
  btn.click();
  return { ok: true, label: txt(btn).slice(0, 20) };
}
"""

DUMP_COMMENT_AREA_JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const nodes = Array.from(document.querySelectorAll("[class*=Comment]")).map(el => ({
    tag: el.tagName, cls: cls(el).slice(0, 80), text: txt(el).slice(0, 90),
    kids: el.children.length
  })).filter(n => n.text || n.cls);
  const cc = document.querySelector(".CommentContent");
  let chain = [];
  let cur = cc;
  while (cur && chain.length < 7) { chain.push({ tag: cur.tagName, cls: cls(cur).slice(0, 80), text: txt(cur).slice(0, 80) }); cur = cur.parentElement; }
  const editables = Array.from(document.querySelectorAll("[contenteditable=true],textarea")).map(el => ({
    cls: cls(el).slice(0, 80), placeholder: el.getAttribute("placeholder") || "", visible: !!el.offsetParent }));
  const buttons = Array.from(document.querySelectorAll("button")).filter(b => b.offsetParent)
      .map(b => ({ text: txt(b).slice(0, 14), cls: cls(b).slice(0, 60), disabled: !!b.disabled }))
      .filter(b => b.text).slice(-14);
  return { nodes: nodes.slice(0, 30), commentContentChain: chain, editables: editables, buttons: buttons,
           html: cc ? cc.parentElement.parentElement.outerHTML.slice(0, 4000) : "" };
}
"""


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        out["whoami"] = b._safe_evaluate(WHOAMI_JS) or {}
        out["answers"] = b._safe_evaluate(ANSWERS_JS) or []
        print("=== 页面标题 ===", out["whoami"].get("title"))
        print("=== 页面上的人 ===")
        for p in out["whoami"].get("people") or []:
            print("   ", p.get("text"), "|", p.get("href"), "| header=", p.get("inHeader"))
        print("=== 回答条目 ===")
        for i, a in enumerate(out["answers"]):
            print("   #%s author=%s vote=%s commentBtn=%s" % (i, a.get("author"), a.get("vote"), a.get("commentBtn")))
        idx = 0
        out["click"] = b._safe_evaluate(CLICK_COMMENT_JS, idx) or {}
        print("=== 点评论按钮 ===", out["click"])
        time.sleep(6)
        for _ in range(3):
            b._safe_evaluate("() => { window.scrollBy(0, 900); return true; }")
            time.sleep(1.5)
        out["area"] = b._safe_evaluate(DUMP_COMMENT_AREA_JS) or {}
        print("=== Comment 相关元素 ===")
        for n in out["area"].get("nodes") or []:
            print("   [%s] %s | kids=%s | %s" % (n.get("tag"), n.get("cls")[:50], n.get("kids"), n.get("text")[:70]))
        print("=== CommentContent 祖先链 ===")
        for c in out["area"].get("commentContentChain") or []:
            print("   [%s] %s | %s" % (c.get("tag"), c.get("cls")[:50], c.get("text")[:60]))
        print("=== editables ===", out["area"].get("editables"))
        print("=== buttons ===", out["area"].get("buttons"))
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "answer_comments2_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("原始结构已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())