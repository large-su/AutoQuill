# -*- coding: utf-8 -*-
"""只读探针：把「我」最近发布的回答列出来（核对今天到底发出去了几篇）。"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

ME_JS = r"""
async () => {
  const r = await fetch('/api/v4/members/me?include=url_token,name',
                        {credentials: 'include'});
  if (!r.ok) return {ok: false, status: r.status};
  const d = await r.json();
  return {ok: true, url_token: d.url_token, name: d.name, id: d.id};
}
"""

ANSWERS_JS = r"""
async (token) => {
  const out = [];
  let url = '/api/v4/members/' + token
      + '/answers?include=data%5B*%5D.created_time%2Cupdated_time'
      + '%2Cquestion%2Cvoteup_count%2Ccomment_count%2Cexcerpt&limit=20&offset=0';
  for (let page = 0; page < 3; page++) {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return {ok: false, status: r.status, out: out};
    const d = await r.json();
    for (const a of (d.data || [])) {
      out.push({id: a.id, created: a.created_time, updated: a.updated_time,
                q: (a.question || {}).title || '',
                qid: (a.question || {}).id || '',
                voteup: a.voteup_count, comment: a.comment_count,
                url: a.url || ('https://www.zhihu.com/answer/' + a.id)});
    }
    if (!d.paging || d.paging.is_end) break;
    url = d.paging.next.replace('https://www.zhihu.com', '');
    await new Promise(r => setTimeout(r, 800));
  }
  return {ok: true, out: out};
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/", wait_until="domcontentloaded",
                    timeout=45000)
        time.sleep(4)
        me = b._safe_evaluate(ME_JS) or {}
        print("我：%s" % json.dumps(me, ensure_ascii=False))
        if not me.get("ok"):
            return 1
        got = b._safe_evaluate(ANSWERS_JS, me.get("url_token")) or {}
        rows = got.get("out") or []
        print("拿到 %d 条回答（按发布时间倒序）" % len(rows))
        import datetime as _dt
        for a in rows:
            ts = a.get("created") or 0
            try:
                t = _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
            except Exception:                 # noqa: BLE001
                t = str(ts)
            print("  %s  id=%s 赞=%-5s 评=%-4s 《%s》"
                  % (t, a.get("id"), a.get("voteup"), a.get("comment"),
                     (a.get("q") or "")[:38]))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
