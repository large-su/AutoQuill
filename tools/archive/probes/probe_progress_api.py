# -*- coding: utf-8 -*-
"""只读探针：确认进度校核要用的两个接口的字段与「按时间过滤」能力。

1) /api/v4/creators/creations/v2/answer  —— 已发布回答（要 created_time 与 qid）
2) /api/v4/answer-drafts/count           —— 草稿数（比滚动草稿箱便宜得多）
只读，不写任何东西。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

PROBE = r"""
async () => {
  const out = {};
  const get = async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    const t = await r.text();
    return {status: r.status, body: t};
  };
  out.count = await get('/api/v4/answer-drafts/count');
  out.list = await get('/api/v4/creators/creations/v2/answer'
      + '?start=0&end=0&limit=5&offset=0&need_co_creation=1&sort_type=created');
  // 试着按时间区间过滤（今天 00:00 到现在）
  const now = Math.floor(Date.now() / 1000);
  const midnight = Math.floor(new Date(new Date().setHours(0,0,0,0)).getTime() / 1000);
  out.range = await get('/api/v4/creators/creations/v2/answer'
      + '?start=' + midnight + '&end=' + now
      + '&limit=20&offset=0&need_co_creation=1&sort_type=created');
  out.window = {midnight: midnight, now: now};
  return out;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    import datetime as _dt
    from applications.zhihu_story.browser_adapter import ZhihuBrowser

    def show(name, info, dump_rows=3):
        print("\n=== %s ===" % name)
        if not info:
            print("  （无返回）")
            return
        print("  status=%s len=%s" % (info.get("status"), len(info.get("body") or "")))
        try:
            data = json.loads(info["body"])
        except Exception:
            print("  非 JSON：%s" % (info.get("body") or "")[:200])
            return
        if isinstance(data, dict):
            print("  顶层字段=%s" % sorted(data.keys())[:12])
            paging = data.get("paging") or {}
            if paging:
                print("  paging.totals=%s is_end=%s" % (paging.get("totals"),
                                                         paging.get("is_end")))
            rows = data.get("data")
            if isinstance(rows, list):
                print("  条数=%d" % len(rows))
                for r in rows[:dump_rows]:
                    if not isinstance(r, dict):
                        continue
                    ct = r.get("created_time") or r.get("created") or 0
                    try:
                        when = _dt.datetime.fromtimestamp(int(ct)).strftime("%m-%d %H:%M")
                    except Exception:
                        when = str(ct)
                    q = r.get("question") or {}
                    title = q.get("title") if isinstance(q, dict) else ""
                    print("    id=%s created=%s qid=%s 《%s》"
                          % (r.get("id"), when, q.get("id") if isinstance(q, dict) else "",
                             str(title)[:34]))
                    if r is rows[0]:
                        print("    字段=%s" % sorted(r.keys())[:22])

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/creator/manage/creation/answer",
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        got = b._safe_evaluate(PROBE) or {}
        show("草稿数 /api/v4/answer-drafts/count", got.get("count"))
        show("已发布（默认区间）", got.get("list"))
        print("\n  时间窗口=%s" % got.get("window"))
        show("已发布（今天 00:00 → 现在）", got.get("range"), dump_rows=20)
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
