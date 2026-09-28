# -*- coding: utf-8 -*-
"""只读探针：核对某条回答是否真的已发布 + 草稿箱里还有哪几篇。

用途：界面报「发布失败」但账号里有新回答时，用它判断到底谁对。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

ANSWER_JS = r"""(aid) => fetch('/api/v4/answers/' + aid + '?include=created_time,question,author,voteup_count',
        {credentials: 'include'})
    .then(r => r.ok ? r.json() : {__status: r.status})
    .then(d => ({status: (d || {}).__status || 200,
                 id: (d || {}).id || '',
                 created: (d || {}).created_time || 0,
                 question: ((d || {}).question || {}).title || '',
                 author: ((d || {}).author || {}).name || '',
                 excerpt: ((d || {}).excerpt || '').slice(0, 60)}))
    .catch(e => ({status: -1, err: String(e)}))"""

CARDS_JS = r"""() => Array.from(
    document.querySelectorAll('.CreationManage-CreationCard')).map((c, i) => {
  const a = c.querySelector('a[href*="/question/"][href*="#write"]');
  const t = c.querySelector('.CreationCardTitle-wrapper');
  const m = a ? a.href.match(/question\/(\d+)/) : null;
  return {index: i, qid: m ? m[1] : '', title: t ? (t.innerText || '').trim() : ''};
})"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--aid", default="2088018397955094230")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    import datetime as _dt
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/", wait_until="domcontentloaded",
                    timeout=45000)
        time.sleep(3)
        info = b._safe_evaluate(ANSWER_JS, str(args.aid)) or {}
        print("回答 %s：" % args.aid)
        c = info.get("created") or 0
        try:
            when = _dt.datetime.fromtimestamp(int(c)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:                     # noqa: BLE001
            when = str(c)
        print("  状态=%s 作者=%s 发布=%s" % (info.get("status"), info.get("author"), when))
        print("  问题=%s" % info.get("question"))
        print("  摘要=%s" % info.get("excerpt"))
        b.page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        for _ in range(4):
            b._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.2)
        cards = b._safe_evaluate(CARDS_JS) or []
        print("\n草稿箱还剩 %d 篇：" % len(cards))
        for x in cards:
            print("  [%d] qid=%s 《%s》" % (x.get("index"), x.get("qid"),
                                           (x.get("title") or "")[:44]))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
