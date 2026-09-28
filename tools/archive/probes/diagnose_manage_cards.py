# -*- coding: utf-8 -*-
"""只读探针：评论管理页首屏「卡片文本」到底包含什么（核对回复可见性判据）。

修复背景（2026-09-28）：发送后的核实是「卡片正文里有没有我们的回复」，
但没人确认过回复渲染在卡片的哪个位置。这里把前 N 张卡片的完整文本打出来，
确认：① 卡片文本里能看到该评论；② 若某条评论下已有我们的回复，它是否也在
同一张卡片的文本里（决定 `manage_reply_visible` 这条判据是否成立）。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

CARDS_JS = r"""() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/[ \t]+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.CommentManage-CommentCard'))
      .slice(0, 6)
      .map((c, i) => {
        const contents = Array.from(c.querySelectorAll('.CommentContent'))
            .map(x => clean(x.innerText).slice(0, 120));
        return {index: i, text: clean(c.innerText).slice(0, 400),
                comment_contents: contents,
                buttons: Array.from(c.querySelectorAll('button'))
                    .map(b => clean(b.innerText)).filter(Boolean).slice(0, 8)};
      });
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from applications.zhihu_story.browser_interact import COMMENT_MANAGE_URL
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(COMMENT_MANAGE_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        cards = b._safe_evaluate(CARDS_JS) or []
        print("首屏卡片：%d 张" % len(cards))
        for c in cards:
            print("\n--- 卡片 %d ---" % c.get("index"))
            print("  文本: %s" % (c.get("text") or "")[:300])
            print("  CommentContent(%d): %s"
                  % (len(c.get("comment_contents") or []),
                     c.get("comment_contents")))
            print("  按钮: %s" % c.get("buttons"))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
