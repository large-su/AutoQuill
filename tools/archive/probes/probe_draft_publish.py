# -*- coding: utf-8 -*-
"""只读探针：草稿箱 → 草稿编辑页的发布链路 DOM 结构（M2 发布草稿接入前置）。

★ 全程只导航与读取，不点「发布」、不改任何内容。
   探明：草稿列表卡片结构 / 编辑页「发布」按钮与容器 / 可能的确认弹窗容器 /
   页面脚本里出现过的 publish 相关接口路径（据此决定走 DOM 还是接口通道）。

运行：.venv/Scripts/python tools/archive/probes/probe_draft_publish.py [--data-dir 路径]
输出：控制台摘要 + data/cleanup/draft_publish_probe_<ts>.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

LIST_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const cards = Array.from(document.querySelectorAll(".CreationManage-CreationCard"));
  return {
    url: location.href,
    card_count: cards.length,
    cards: cards.slice(0, 5).map(c => {
      const a = c.querySelector("a[href*=\"#write\"]");
      const t = c.querySelector(".CreationCardTitle-wrapper");
      return {
        title: t ? clean(t.innerText) : "",
        edit_href: a ? a.href : "",
        buttons: Array.from(c.querySelectorAll("button")).map(b => clean(b.innerText)),
        text: clean(c.innerText).slice(0, 180),
      };
    }),
  };
}
"""

EDITOR_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const vis = e => e.offsetParent !== null;
  const buttons = Array.from(document.querySelectorAll("button,[role=button],a"))
      .filter(vis).map(b => ({ tag: b.tagName, cls: String(b.className || "").slice(0, 60),
                               text: clean(b.innerText).slice(0, 24) }))
      .filter(b => b.text);
  const hits = Array.from(document.querySelectorAll("*"))
      .filter(e => vis(e) && /发布|提交回答/.test(clean(e.innerText)) && e.children.length <= 2)
      .slice(0, 8)
      .map(e => ({ tag: e.tagName, cls: String(e.className || "").slice(0, 90),
                   id: e.id || "", text: clean(e.innerText).slice(0, 20),
                   html: e.outerHTML.slice(0, 240) }));
  const modals = Array.from(document.querySelectorAll("[class*=Modal],[class*=modal],[role=dialog]"))
      .slice(0, 6).map(m => ({ cls: String(m.className || "").slice(0, 70),
                               shown: vis(m), text: clean(m.innerText).slice(0, 120) }));
  return { url: location.href, title: document.title, buttons: buttons.slice(0, 40),
           publish_hits: hits, modals: modals,
           has_editor: !!document.querySelector(".public-DraftEditor-content,[contenteditable=true]") };
}
"""

SCRIPT_JS = r"""
async () => {
  const srcs = Array.from(document.querySelectorAll("script[src]")).map(s => s.src);
  const re = /\/api\/v[0-9]+\/[A-Za-z0-9_\/\-?=&.%]{0,90}/g;
  const found = new Set();
  for (const src of srcs.slice(0, 25)) {
    try {
      const r = await fetch(src);
      const t = await r.text();
      let m;
      while ((m = re.exec(t)) !== null) {
        if (/publish|answer|draft|creation/i.test(m[0])) found.add(m[0]);
      }
    } catch (e) { /* 忽略失败 */ }
  }
  return { scripts: srcs.length, api_paths: Array.from(found).slice(0, 60) };
}
"""


TOOLBAR_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").replace(/[\u200b-\u200d\ufeff]/g, "").trim();
  const box = document.querySelector("#AnswerFormPortalContainer") || document;
  const btns = Array.from(box.querySelectorAll("button,[role=button],a,div"))
      .filter(e => e.offsetParent !== null && clean(e.innerText) && e.children.length <= 1)
      .map(e => ({ tag: e.tagName, cls: String(e.className || "").slice(0, 70), id: e.id || "",
                   text: clean(e.innerText).slice(0, 16),
                   html: e.outerHTML.slice(0, 200) }));
  const pub = Array.from(box.querySelectorAll("*"))
      .filter(e => e.offsetParent !== null && /^发布/.test(clean(e.innerText)) && e.children.length <= 2)
      .slice(0, 6)
      .map(e => ({ tag: e.tagName, cls: String(e.className || "").slice(0, 90), id: e.id || "",
                   text: clean(e.innerText).slice(0, 16), html: e.outerHTML.slice(0, 260) }));
  return { in_editor: !!document.querySelector("#AnswerFormPortalContainer"),
           buttons: btns.slice(0, 40), publish_candidates: pub,
           editor_text_head: clean((document.querySelector(".public-DraftEditor-content") || {}).innerText || "").slice(0, 80) };
}
"""

BUNDLE_JS = r"""
async () => {
  const srcs = Array.from(document.querySelectorAll("script[src]")).map(s => s.src);
  const out = { publish_sites: [], answer_api_sites: [] };
  for (const src of srcs.slice(0, 30)) {
    try {
      const t = await (await fetch(src)).text();
      let i = t.indexOf("/api/v4/content/publish");
      let n = 0;
      while (i >= 0 && n < 3) {
        out.publish_sites.push(t.slice(Math.max(0, i - 800), i + 260));
        i = t.indexOf("/api/v4/content/publish", i + 1);
        n += 1;
      }
      let j = t.indexOf("empty_answer_draft_web");
      if (j >= 0 && out.answer_api_sites.length < 3) {
        out.answer_api_sites.push(t.slice(Math.max(0, j - 500), j + 200));
      }
    } catch (e) { /* 忽略 */ }
  }
  return out;
}
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="", help="指定数据目录（默认源码态项目 data）")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    report = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        for _ in range(3):
            b._safe_evaluate("() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.5)
        report["list"] = b._safe_evaluate(LIST_JS) or {}
        first = (report["list"].get("cards") or [{}])[0]
        edit_url = first.get("edit_href") or ""
        report["edit_url"] = edit_url
        if edit_url:
            b.page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(7)
            report["editor"] = b._safe_evaluate(EDITOR_JS) or {}
            report["toolbar"] = b._safe_evaluate(TOOLBAR_JS) or {}
            report["scripts"] = b._safe_evaluate(SCRIPT_JS) or {}
            report["bundles"] = b._safe_evaluate(BUNDLE_JS) or {}
    finally:
        b.close()
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "draft_publish_probe_%s.json"
                        % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    lst = report.get("list", {})
    ed = report.get("editor", {})
    print("LIST cards=%s url=%s" % (lst.get("card_count"), lst.get("url")))
    for c in (lst.get("cards") or [])[:3]:
        print("  card:", json.dumps(c, ensure_ascii=False)[:220])
    print("EDITOR url=%s has_editor=%s title=%s" % (ed.get("url"), ed.get("has_editor"), ed.get("title")))
    print("  publish_hits:", json.dumps(ed.get("publish_hits"), ensure_ascii=False)[:700])
    print("  modals:", json.dumps(ed.get("modals"), ensure_ascii=False)[:300])
    print("  buttons:", json.dumps([x.get("text") for x in (ed.get("buttons") or [])][:24], ensure_ascii=False))
    print("  api_paths:", json.dumps((report.get("scripts") or {}).get("api_paths"), ensure_ascii=False)[:1000])
    print("report:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
