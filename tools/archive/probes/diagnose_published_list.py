# -*- coding: utf-8 -*-
"""只读探针：创作中心「已发布回答」列表 —— 核对今天到底发出去了哪几篇。"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

URL = "https://www.zhihu.com/creator/manage/creation/answer"

CARDS_JS = r"""() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/[ \t]+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.CreationManage-CreationCard'))
      .map(c => ({
        title: clean((c.innerText || '').split('\n')[0] || ''),
        text: clean(c.innerText).slice(0, 200),
        href: (function () {
          const a = c.querySelector('a[href*="/question/"],a[href*="/answer/"]');
          return a ? a.href : '';
        })(),
      }));
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        for _ in range(10):
            b._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.2)
        cards = b._safe_evaluate(CARDS_JS) or []
        print("已发布回答卡片：%d 张" % len(cards))
        for i, c in enumerate(cards[:30]):
            print("[%2d] 《%s》" % (i, (c.get("title") or "")[:44]))
            print("      %s" % (c.get("text") or "")[:150])
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
