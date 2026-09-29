# -*- coding: utf-8 -*-
"""只读诊断：点开「添加评论」后，编辑器焦点与状态到底在哪。

不发送任何内容；只在编辑器里打字并读回状态，最后清空。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

STATE_JS = r"""
() => {
  const vis = e => e.offsetParent !== null;
  const eds = Array.from(document.querySelectorAll(
      'div.public-DraftEditor-content, [contenteditable=true], textarea'))
      .filter(vis)
      .map((e, i) => ({
        i: i,
        tag: e.tagName,
        cls: String(e.className || '').slice(0, 60),
        hasFocus: document.activeElement === e || e.contains(document.activeElement),
        text: ((e.innerText || e.value || '')).replace(/\s+/g, ' ').slice(0, 40),
      }));
  const pubs = Array.from(document.querySelectorAll('button')).filter(vis)
      .map((b, i) => ({ i: i, text: (b.innerText || '').replace(/\s+/g, '').slice(0, 10),
                        disabled: !!b.disabled,
                        cls: String(b.className || '').slice(0, 50) }))
      .filter(b => /发布|评论|发送/.test(b.text));
  return { editors: eds, publish_buttons: pubs,
           active_tag: document.activeElement
               ? (document.activeElement.tagName + '.' +
                  String(document.activeElement.className || '').slice(0, 40)) : '' };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--url", required=True)
    ap.add_argument("--text", default="这是一条用于验证输入链路的临时评论内容，稍后会清空")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        print("=== 打字前 ===")
        print(json.dumps(b._safe_evaluate(STATE_JS), ensure_ascii=False, indent=2))

        r = b._safe_evaluate(
            "() => { const ZW = String.fromCharCode(8203,8204,8205,65279);"
            " const clean = s => (s||'').split('').filter(c=>ZW.indexOf(c)<0)"
            ".join('').replace(/\\s+/g,'');"
            " const scopes = Array.from(document.querySelectorAll("
            "'.AnswerItem .ContentItem-actions, .QuestionAnswer-content .ContentItem-actions, .ContentItem-actions'))"
            ".filter(e => e.offsetParent);"
            " for (const s of scopes) {"
            "   const hit = Array.from(s.querySelectorAll('button')).filter(x=>x.offsetParent)"
            "     .find(x => clean(x.innerText) === '添加评论');"
            "   if (hit) { hit.click(); return {ok:true}; } }"
            " return {ok:false}; }")
        print("\n=== 点「添加评论」：%s ===" % json.dumps(r, ensure_ascii=False))
        time.sleep(3)
        print(json.dumps(b._safe_evaluate(STATE_JS), ensure_ascii=False, indent=2))

        print("\n=== 尝试 keyboard.type ===")
        b.page.keyboard.type(args.text)
        time.sleep(2)
        print(json.dumps(b._safe_evaluate(STATE_JS), ensure_ascii=False, indent=2))

        print("\n=== 清空（Ctrl+A / Delete）===")
        b.page.keyboard.press("Control+A")
        b.page.keyboard.press("Delete")
        time.sleep(1)
        print(json.dumps(b._safe_evaluate(STATE_JS), ensure_ascii=False, indent=2))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
