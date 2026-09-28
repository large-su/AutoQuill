# -*- coding: utf-8 -*-
"""只读探针 3：看清「送礼物设置」那一行的 DOM（开启/关闭怎么选）。"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

OPEN_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  const btn = Array.from(document.querySelectorAll('button,div,span,a'))
      .filter(vis).find(e => clean(e.innerText) === '发布设置');
  if (!btn) return {ok: false};
  btn.click();
  return {ok: true};
}
"""

GIFT_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  // 「送礼物设置」所在的整行容器
  let row = null;
  for (const e of Array.from(document.querySelectorAll('body *')).filter(vis)) {
    const t = clean(e.innerText);
    if (/^送礼物设置/.test(t) && t.length < 40) { row = e.parentElement || e; break; }
  }
  if (!row) return {found: false};
  // 该行往上两层，取到包含「开启送礼物/关闭送礼物」的容器
  let box = row;
  for (let k = 0; k < 3; k++) {
    if (box.parentElement && /开启送礼物/.test(
            clean(box.parentElement.innerText))) box = box.parentElement;
  }
  const opts = Array.from(box.querySelectorAll('*')).filter(vis)
      .filter(e => {
        const t = clean(e.innerText);
        return (t === '开启送礼物' || t === '关闭送礼物') && e.children.length <= 2;
      })
      .map(e => ({ tag: e.tagName, cls: String(e.className || '').slice(0, 80),
                   text: clean(e.innerText),
                   html: e.outerHTML.slice(0, 400) }));
  const inputs = Array.from(box.querySelectorAll('input')).map(i => ({
    type: i.type, checked: i.checked, value: i.value,
    cls: String(i.className || '').slice(0, 60) }));
  return {found: true, row_text: clean(box.innerText).slice(0, 200),
          options: opts, inputs: inputs,
          box_html: box.outerHTML.slice(0, 1500)};
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--qid", default="2063380350257063810")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/question/%s#write" % args.qid,
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        print("打开发布设置：%s" % json.dumps(
            b._safe_evaluate(OPEN_JS) or {}, ensure_ascii=False))
        time.sleep(4)
        info = b._safe_evaluate(GIFT_JS) or {}
        print(json.dumps(info, ensure_ascii=False, indent=2)[:4000])
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
