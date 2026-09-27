# -*- coding: utf-8 -*-
# ============================================================
# probe_answer_comments.py - 回答页评论区的「已回复」真相 + 回复编辑器 DOM
#
# 目的：
#   1) 回答页评论区能不能看出「这条评论我回复过没有」（防重复回复的真相来源）；
#   2) 评论列表的 DOM（作者/正文/嵌套回复/回复按钮）；
#   3) 点「回复」后的编辑器与发送按钮（只输入测试文字，不发送）。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_answer_comments.py [回答URL]
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

# 零宽字符在知乎按钮文本里很常见（"\u200b\n回复"），统一清理
ZW = "String.fromCharCode(8203,8204,8205,65279)"
CLICK_COMMENT_TAB_JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, "").trim();
  const btns = Array.from(document.querySelectorAll("button,.ContentItem-action"));
  const hit = btns.find(b => /条评论/.test(clean(b.innerText)) || /^评论$/.test(clean(b.innerText))) || btns.find(b => /评论/.test(clean(b.innerText)) && !/评论管理/.test(clean(b.innerText)));
  if (hit) { hit.click(); return clean(hit.innerText).slice(0, 20); }
  return "";
}
"""

SCROLL_COMMENTS_JS = """
() => {
  const box = document.querySelector(".Comments-container,.Comments--action,.CommentList,.Modal-content");
  if (box) { box.scrollTop = box.scrollHeight; }
  window.scrollBy(0, 1200);
  return true;
}
"""

DUMP_COMMENTS_JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const items = Array.from(document.querySelectorAll(".CommentItem,.CommentItemV2,[class*=CommentItem]"));
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const t = txt(it);
    if (!t || t.length < 2) continue;
    const key = t.slice(0, 60);
    if (seen.has(key)) continue;
    seen.add(key);
    const author = it.querySelector("a[href*='/people/']");
    out.push({
      cls: cls(it).slice(0, 70),
      author: author ? txt(author).slice(0, 20) : "",
      authorHref: author ? author.getAttribute("href") : "",
      childComments: it.querySelectorAll(".CommentItem,.CommentItemV2").length,
      hasReplyBtn: Array.from(it.querySelectorAll("button")).some(b => txt(b) === "回复"),
      text: t.slice(0, 300)
    });
  }
  const counts = {};
  document.querySelectorAll("[class*=Comment]").forEach(el => {
    cls(el).split(" ").forEach(c => { if (c) counts[c] = (counts[c] || 0) + 1; });
  });
  return { url: location.href, title: document.title, items: out.slice(0, 25),
           classCounts: Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 25),
           body: txt(document.body).slice(0, 800) };
}
"""

CLICK_REPLY_JS = """
(idx) => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, "").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const items = Array.from(document.querySelectorAll(".CommentItem,.CommentItemV2,[class*=CommentItem]"));
  const it = items[idx];
  if (!it) return { ok: false, reason: "没有这条评论" };
  const btn = Array.from(it.querySelectorAll("button")).find(b => txt(b) === "回复");
  if (!btn) return { ok: false, reason: "没有回复按钮" };
  btn.click();
  return { ok: true, item: txt(it).slice(0, 60) };
}
"""

EDITOR_JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  return {
    editables: Array.from(document.querySelectorAll("[contenteditable=true],textarea")).map(el => ({
      tag: el.tagName, cls: cls(el).slice(0, 80), placeholder: el.getAttribute("placeholder") || "",
      visible: !!el.offsetParent, text: txt(el).slice(0, 60)
    })),
    buttons: Array.from(document.querySelectorAll("button")).filter(b => b.offsetParent)
        .map(b => ({ text: txt(b).slice(0, 14), cls: cls(b).slice(0, 60), disabled: !!b.disabled }))
        .filter(b => b.text).slice(-10)
  };
}
"""


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    out = {"url": url}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        out["click_tab"] = b._safe_evaluate(CLICK_COMMENT_TAB_JS)
        time.sleep(5)
        for _ in range(4):
            b._safe_evaluate(SCROLL_COMMENTS_JS)
            time.sleep(1.5)
        out["comments"] = b._safe_evaluate(DUMP_COMMENTS_JS) or {}
        print("=== 点击评论按钮 ===", out["click_tab"])
        print("=== Comment 类名统计 ===", out["comments"].get("classCounts"))
        print("=== 评论条目 ===")
        for it in out["comments"].get("items") or []:
            print("   [%s] author=%s nested=%s replyBtn=%s | %s" % (it.get("cls")[:26], it.get("author"), it.get("childComments"), it.get("hasReplyBtn"), it.get("text")[:120]))
        print("=== 页面正文尾部 ===")
        print((out["comments"].get("body") or "")[-500:])
        click = b._safe_evaluate(CLICK_REPLY_JS, 0) or {}
        time.sleep(3)
        out["click_reply"] = click
        out["editor"] = b._safe_evaluate(EDITOR_JS) or {}
        print("=== 点回复 ===", click)
        print("    editables:", out["editor"].get("editables"))
        print("    buttons:", out["editor"].get("buttons"))
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "answer_comments_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("原始结构已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())