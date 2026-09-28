# -*- coding: utf-8 -*-
"""只读探针 2：「发布设置」面板 + 发布请求体（找「送礼物」这个被拒的字段）。

★ 只点开「发布设置」（不点发布）；**不发任何 POST**（只读取已经发生的请求体）。
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.getcwd())

OPEN_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  const cands = Array.from(document.querySelectorAll('button,div,span,a'))
      .filter(vis).filter(e => clean(e.innerText) === '发布设置');
  if (!cands.length) return {ok: false, reason: 'no-button'};
  cands[0].click();
  return {ok: true, tag: cands[0].tagName,
          cls: String(cands[0].className || '').slice(0, 60)};
}
"""

PANEL_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  const all = Array.from(document.querySelectorAll('body *')).filter(vis);
  // 所有出现「礼物/赞赏/收益/声明/匿名/评论」的可见叶子节点
  const hits = [];
  for (const e of all) {
    const t = clean(e.innerText);
    if (!t || t.length > 60) continue;
    if (!/礼物|赞赏|收益|声明|匿名|评论|同步|生成|话题|地区/.test(t)) continue;
    hits.push({ tag: e.tagName, cls: String(e.className || '').slice(0, 70),
                text: t,
                parent_html: (e.parentElement ? e.parentElement.outerHTML : '')
                    .slice(0, 300) });
    if (hits.length >= 25) break;
  }
  // 开关类元素（含 aria-checked / role=switch / class 带 switch）
  const switches = all.filter(e => /switch|Switch|toggle|Toggle/.test(
        String(e.className || '') + (e.getAttribute('role') || '')))
      .slice(0, 20)
      .map(e => ({ tag: e.tagName, cls: String(e.className || '').slice(0, 70),
                   role: e.getAttribute('role'),
                   checked: e.getAttribute('aria-checked'),
                   text: clean(e.innerText).slice(0, 40),
                   html: e.outerHTML.slice(0, 240) }));
  return {hits: hits, switches: switches};
}
"""

REQ_JS = r"""() => (window.__aq_reqs || [])"""

HOOK_JS = r"""
() => {
  if (window.__aq_req_hook) return 'already';
  window.__aq_reqs = [];
  const of = window.fetch;
  window.fetch = function (a, b) {
    try {
      const url = String((a && a.url) || a || '');
      if (/publish|answer-settings|draft/i.test(url)) {
        window.__aq_reqs.push({ url: url.slice(0, 140),
                                method: (b && b.method) || (a && a.method) || 'GET',
                                body: b && b.body ? String(b.body).slice(0, 1200) : '' });
      }
    } catch (e) { /* 忽略 */ }
    return of.apply(this, arguments);
  };
  window.__aq_req_hook = true;
  return 'installed';
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--qid", default="2063380350257063810")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    payload = {}

    def on_req(req):
        try:
            if re.search(r"/api/v4/content/publish", req.url, re.I):
                payload["url"] = req.url
                payload["post"] = (req.post_data or "")[:2000]
        except Exception:                     # noqa: BLE001
            pass

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.on("request", on_req)
        b.page.goto("https://www.zhihu.com/question/%s#write" % args.qid,
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        print("hook:", b._safe_evaluate(HOOK_JS))
        print("打开发布设置：%s" % json.dumps(
            b._safe_evaluate(OPEN_JS) or {}, ensure_ascii=False))
        time.sleep(4)
        panel = b._safe_evaluate(PANEL_JS) or {}
        print("\n=== 面板里与「礼物/赞赏/声明」相关的可见文本 ===")
        for h in panel.get("hits") or []:
            print("  <%s class=%s> %r" % (h.get("tag"),
                                          (h.get("cls") or "")[:40],
                                          h.get("text")))
        print("\n=== 开关元素 ===")
        for s in panel.get("switches") or []:
            print("  %s" % json.dumps(s, ensure_ascii=False)[:260])
        print("\n=== 抓到的请求体（我们这一侧发出的） ===")
        for r in (b._safe_evaluate(REQ_JS) or [])[:10]:
            print("  %s %s" % (r.get("method"), r.get("url")))
            if r.get("body"):
                print("      body=%s" % r.get("body")[:600])
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
