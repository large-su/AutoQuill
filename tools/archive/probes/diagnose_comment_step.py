# -*- coding: utf-8 -*-
"""单步诊断：点开评论框 → 找编辑器 → 聚焦 → 输入 → 读回。每步都打印。

只读性质：最后会清空编辑器，不点发布、不发评论。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

COUNT_JS = r"""
() => {
  const vis = e => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const draft = Array.from(document.querySelectorAll('div.public-DraftEditor-content'));
  const ce = Array.from(document.querySelectorAll('[contenteditable=true]'));
  const ta = Array.from(document.querySelectorAll('textarea'));
  return {
    draft_total: draft.length, draft_vis: draft.filter(vis).length,
    ce_total: ce.length, ce_vis: ce.filter(vis).length,
    ta_total: ta.length, ta_vis: ta.filter(vis).length,
    draft_vis_info: draft.filter(vis).map(e => ({
      cls: String(e.className || '').slice(0, 50),
      rect: (r => [Math.round(r.width), Math.round(r.height)])(e.getBoundingClientRect()),
      focused: document.activeElement === e || e.contains(document.activeElement),
      text: (e.innerText || '').replace(/\s+/g, ' ').slice(0, 30),
    })),
    active: document.activeElement ? (document.activeElement.tagName + '.' +
            String(document.activeElement.className || '').slice(0, 40)) : '',
    publish: Array.from(document.querySelectorAll('button'))
      .filter(vis).map(b => ({ t: (b.innerText || '').replace(/\s+/g, '').slice(0, 8),
                               dis: !!b.disabled }))
      .filter(x => /发布|评论|发送/.test(x.t)),
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--url", required=True)
    ap.add_argument("--text", default="验证输入链路的临时内容，稍后清空")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from applications.zhihu_story import browser_interact as bi

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        print("=== 0. 打字前 ===")
        print(json.dumps(b._safe_evaluate(COUNT_JS), ensure_ascii=False))

        print("\n=== 1. 生产 JS 点「添加评论」 ===")
        r = b._safe_evaluate(bi._OPEN_ANSWER_COMMENT_JS)
        print(json.dumps(r, ensure_ascii=False))
        time.sleep(3)
        print(json.dumps(b._safe_evaluate(COUNT_JS), ensure_ascii=False))

        print("\n=== 2. 生产 _EDITOR_STATE_JS ===")
        print(json.dumps(b._safe_evaluate(bi._EDITOR_STATE_JS), ensure_ascii=False))

        print("\n=== 3. 生产焦点函数 ===")
        print("focus ok =", b._focus_comment_editor())
        print(json.dumps(b._safe_evaluate(COUNT_JS), ensure_ascii=False))

        print("\n=== 4. keyboard.type('%s') ===" % args.text)
        b.page.keyboard.type(args.text)
        time.sleep(2)
        print(json.dumps(b._safe_evaluate(COUNT_JS), ensure_ascii=False))
        print("生产状态：", json.dumps(b._safe_evaluate(bi._EDITOR_STATE_JS),
                                      ensure_ascii=False))

        print("\n=== 5. 清空 ===")
        b.page.keyboard.press("Control+A")
        b.page.keyboard.press("Delete")
        time.sleep(1)
        print(json.dumps(b._safe_evaluate(COUNT_JS), ensure_ascii=False))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
