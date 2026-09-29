# -*- coding: utf-8 -*-
"""只读探针：回答页上「给这条回答发表评论」的入口与编辑器长什么样。

现有代码只覆盖「回复已有评论」（点某条评论的『回复』）。给**别人的回答**发一条
新评论是另一条路径：需要先找到并点开评论输入框（可能是内联编辑器，也可能是弹层）。
全程只读：只读取结构与按钮文案，不输入、不提交。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

# 只读：找评论入口与可能出现的编辑器容器
PROBE_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;

  // 1) 回答容器的操作栏：点赞/评论/收藏 按钮
  const scope = document.querySelector('.AnswerItem .ContentItem-actions')
      || document.querySelector('.QuestionAnswer-content .ContentItem-actions')
      || document.querySelector('.ContentItem-actions');
  const actionBtns = scope ? Array.from(scope.querySelectorAll('button'))
      .filter(vis).map(b => ({ text: clean(b.innerText).slice(0, 24),
                               cls: String(b.className || '').slice(0, 70),
                               aria: b.getAttribute('aria-label') || '' })) : [];

  // 2) 页面上所有「看起来像评论输入」的元素
  const inputs = Array.from(document.querySelectorAll(
      'textarea, input[type=text], [contenteditable=true], .public-DraftEditor-content, ' +
      '[class*=CommentEditor], [class*=CommentInput], [class*=CommentBox], [class*=Editor]'))
      .filter(vis)
      .slice(0, 20)
      .map(e => ({ tag: e.tagName, cls: String(e.className || '').slice(0, 80),
                   ph: e.getAttribute('placeholder') || e.getAttribute('data-placeholder') || '',
                   ce: e.getAttribute('contenteditable') || '',
                   text: clean(e.innerText).slice(0, 30) }));

  // 3) 含「评论」字样且可点的元素（入口候选）
  const entry = Array.from(document.querySelectorAll('button,a,div,span'))
      .filter(vis)
      .filter(e => {
        const t = clean(e.innerText) || clean(e.getAttribute('aria-label') || '');
        return /条评论|添加评论|写评论|发表评论|评论/.test(t) && t.length <= 16
               && e.children.length <= 2;
      })
      .slice(0, 12)
      .map(e => ({ tag: e.tagName, cls: String(e.className || '').slice(0, 70),
                   text: clean(e.innerText).slice(0, 20),
                   aria: e.getAttribute('aria-label') || '' }));

  return { url: location.href, has_scope: !!scope,
           action_buttons: actionBtns.slice(0, 10),
           inputs: inputs, comment_entries: entry };
}
"""

# 点开评论入口后再看：编辑器是否出现、容器是谁
AFTER_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  const eds = Array.from(document.querySelectorAll(
      '.public-DraftEditor-content, [contenteditable=true], textarea, ' +
      '[class*=CommentEditor] [contenteditable=true]')).filter(vis)
      .map(e => ({ tag: e.tagName, cls: String(e.className || '').slice(0, 80),
                   ph: e.getAttribute('placeholder') || e.getAttribute('data-placeholder') || '',
                   focused: document.activeElement === e || e.contains(document.activeElement) }));
  const pubs = Array.from(document.querySelectorAll('button')).filter(vis)
      .map(b => clean(b.innerText))
      .filter(t => /^(发布|评论|发送|提交)/.test(t)).slice(0, 8);
  return { editors: eds, publish_like_buttons: pubs,
           url: location.href };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--url", default="", help="目标回答/问题页；默认取一个已发布回答")
    ap.add_argument("--click", action="store_true",
                    help="是否点开评论入口（只点「评论」入口，不输入不提交）")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser

    url = args.url
    if not url:
        # 从今天的进度快照里取一个已发布回答所在的页
        try:
            import json as _json
            from pathlib import Path
            from core import paths
            snap = _json.loads(Path(paths.data("data", "state",
                                               "progress_2026-09-29.json"))
                               .read_text(encoding="utf-8"))
            aid = (snap.get("published") or [{}])[0].get("aid") or ""
            url = "https://www.zhihu.com/answer/%s" % aid if aid else ""
        except Exception as exc:                  # noqa: BLE001
            print("取默认 URL 失败：%s" % exc)
    if not url:
        print("需要 --url")
        return 1

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        for _ in range(3):
            b._safe_evaluate(
                "() => { window.scrollBy(0, 700); return true; }")
            time.sleep(1.0)
        info = b._safe_evaluate(PROBE_JS) or {}
        print("URL: %s" % info.get("url"))
        print("有操作栏: %s" % info.get("has_scope"))
        print("\n--- 操作栏按钮 ---")
        for x in info.get("action_buttons") or []:
            print("  %s | aria=%s | class=%s" % (x["text"], x["aria"], x["cls"][:46]))
        print("\n--- 评论入口候选 ---")
        for x in info.get("comment_entries") or []:
            print("  <%s> %r aria=%s class=%s"
                  % (x["tag"], x["text"], x["aria"], x["cls"][:46]))
        print("\n--- 现存输入类元素 ---")
        for x in info.get("inputs") or []:
            print("  <%s> ce=%s ph=%r text=%r class=%s"
                  % (x["tag"], x["ce"], x["ph"], x["text"], x["cls"][:50]))

        if args.click:
            print("\n=== 点开「评论」入口（不输入、不提交）===")
            r = b._safe_evaluate(r"""() => {
              const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
              const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
                  .join('').replace(/\s+/g, '').trim();
              const vis = e => e.offsetParent !== null;
              const scope = document.querySelector('.AnswerItem .ContentItem-actions')
                  || document.querySelector('.QuestionAnswer-content .ContentItem-actions');
              const all = scope ? Array.from(scope.querySelectorAll('button'))
                  .filter(vis) : [];
              const hit = all.find(b => /条评论|评论/.test(clean(b.innerText))
                                        || /评论/.test(clean(b.getAttribute('aria-label') || '')));
              if (!hit) return {ok: false, reason: 'no-entry'};
              hit.click();
              return {ok: true, text: clean(hit.innerText).slice(0, 20)};
            }""")
            print("  点击结果：%s" % json.dumps(r, ensure_ascii=False))
            time.sleep(3)
            after = b._safe_evaluate(AFTER_JS) or {}
            print("  编辑器：%s" % json.dumps(after.get("editors"),
                                             ensure_ascii=False)[:400])
            print("  发布类按钮：%s" % after.get("publish_like_buttons"))
        png = os.path.join("data", "cleanup", "answer_comment_entry.png")
        os.makedirs(os.path.dirname(png), exist_ok=True)
        try:
            b.page.screenshot(path=png)
            print("\n截图：%s" % png)
        except Exception:                          # noqa: BLE001
            pass
    finally:
        try:
            b.close()
        except Exception:                          # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
