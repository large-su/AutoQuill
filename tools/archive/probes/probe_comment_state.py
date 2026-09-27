# -*- coding: utf-8 -*-
# ============================================================
# probe_comment_state.py - 评论管理页「已回复 / 未回复」状态 + 回复编辑器 DOM
#
# 目的：
#   1) 页面上有没有「未回复 / 已回复」筛选标签（Tabs）？
#   2) 卡片上能不能看出这条评论我们回没回过（嵌套回复 / 标记）？
#   3) 点「回复」后出现什么编辑器（contenteditable / textarea / 按钮）？
# 只读为主：点「回复」只为看编辑器，不输入、不发送。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_comment_state.py
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

COMMENT_URL = "https://www.zhihu.com/creator/manage/comment/answer"

TABS_JS = """
() => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const tabs = Array.from(document.querySelectorAll("[class*=Tabs-item],[role=tab],a[href*=comment]"))
      .map(el => ({ tag: el.tagName, text: txt(el).slice(0, 20), cls: cls(el).slice(0, 70), href: el.getAttribute("href") || "" }));
  return { url: location.href, title: document.title, tabs: tabs };
}
"""

CARDS_JS = """
() => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const cards = Array.from(document.querySelectorAll("[class*=CommentManage-CommentCard]"));
  const info = cards.map((c, i) => {
    const rich = c.querySelectorAll(".CommentRichText").length;
    const mine = Array.from(c.querySelectorAll("*")).filter(el => /已回复|我的回复|作者回复|你的回复/.test(txt(el)) && txt(el).length < 12).map(el => txt(el));
    return { i: i, richTextCount: rich, replyMarks: mine.slice(0, 5),
             text: txt(c).split(String.fromCharCode(10)).join(" / ").slice(0, 260) };
  });
  return { count: cards.length, cards: info };
}
"""

CLICK_REPLY_JS = """
(idx) => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const cards = Array.from(document.querySelectorAll("[class*=CommentManage-CommentCard]"));
  const card = cards[idx];
  if (!card) return { ok: false, reason: "卡片不存在" };
  const btns = Array.from(card.querySelectorAll("button,[role=button]")).filter(b => txt(b) === "回复");
  if (!btns.length) return { ok: false, reason: "没找到回复按钮" };
  btns[0].click();
  return { ok: true };
}
"""

EDITOR_JS = """
() => {
  const txt = el => ((el && (el.innerText || el.textContent)) || "").trim();
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const editables = Array.from(document.querySelectorAll("[contenteditable=true],textarea")).map(el => ({
    tag: el.tagName, cls: cls(el).slice(0, 90), placeholder: el.getAttribute("placeholder") || "",
    text: txt(el).slice(0, 60), visible: !!el.offsetParent
  }));
  const buttons = Array.from(document.querySelectorAll("button")).filter(b => b.offsetParent)
      .map(b => ({ text: txt(b).slice(0, 12), cls: cls(b).slice(0, 70), disabled: !!b.disabled }))
      .filter(b => b.text);
  return { editables: editables, buttons: buttons.slice(-12),
           ballot: Array.from(document.querySelectorAll("[class*=Modal],[class*=Editor],[class*=CommentEditor]")).map(el => ({ cls: cls(el).slice(0, 80), text: txt(el).slice(0, 120) })).slice(0, 8) };
}
"""


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        for _ in range(3):
            b._safe_evaluate("() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.5)
        out["tabs"] = b._safe_evaluate(TABS_JS) or {}
        out["cards_before"] = b._safe_evaluate(CARDS_JS) or {}
        print("=== TABS ===")
        for t in out["tabs"].get("tabs") or []:
            print("   ", t.get("tag"), "|", t.get("text"), "|", t.get("cls")[:40], "|", t.get("href")[:60])
        print("=== 卡片数 ===", out["cards_before"].get("count"))
        for c in (out["cards_before"].get("cards") or [])[:20]:
            print("   #%s richText=%s marks=%s | %s" % (c.get("i"), c.get("richTextCount"), c.get("replyMarks"), c.get("text")[:150]))
        # 点第 2 张卡片的「回复」，只看编辑器（不输入、不发送）
        click = b._safe_evaluate(CLICK_REPLY_JS, 1) or {}
        time.sleep(3)
        out["click"] = click
        out["editor"] = b._safe_evaluate(EDITOR_JS) or {}
        print("=== 点回复 ===", click)
        print("=== 编辑器 ===")
        for e in out["editor"].get("editables") or []:
            print("    edit:", e)
        print("    buttons:", out["editor"].get("buttons"))
        print("    ballot:", out["editor"].get("ballot"))
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "comment_state_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("原始结构已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())