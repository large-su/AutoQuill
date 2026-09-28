# -*- coding: utf-8 -*-
"""只读探针：把「已发布回答」接口的第一条完整结构 dump 出来（确定字段路径）。

只读。为进度校核模块确定：created_time / question.id / id 各在哪个层级。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

PROBE = r"""
async () => {
  const now = Math.floor(Date.now() / 1000);
  const midnight = Math.floor(new Date(new Date().setHours(0,0,0,0)).getTime() / 1000);
  const r = await fetch('/api/v4/creators/creations/v2/answer'
      + '?start=' + midnight + '&end=' + now
      + '&limit=20&offset=0&need_co_creation=1&sort_type=created',
      {credentials: 'include'});
  const d = await r.json();
  const rows = d.data || [];
  return {totals: (d.paging || {}).totals, count: rows.length,
          first: rows[0] || null,
          inner_keys: rows[0] && rows[0].data ? Object.keys(rows[0].data) : [],
          inner: rows[0] && rows[0].data ? rows[0].data : null};
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
        b.page.goto("https://www.zhihu.com/creator/manage/creation/answer",
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        got = b._safe_evaluate(PROBE) or {}
        print("totals=%s count=%s" % (got.get("totals"), got.get("count")))
        print("\n=== 外层字段 ===")
        print(json.dumps(sorted((got.get("first") or {}).keys()), ensure_ascii=False))
        print("\n=== 内层 data 字段 ===")
        print(json.dumps(sorted(got.get("inner_keys") or []), ensure_ascii=False))
        inner = got.get("inner") or {}
        keep = {}
        for k in ("id", "created", "created_time", "updated", "updated_time",
                  "title", "question", "url", "type", "status", "excerpt"):
            if k in inner:
                keep[k] = inner[k]
        print("\n=== 关心的字段（内层） ===")
        print(json.dumps(keep, ensure_ascii=False, indent=2)[:1500])
        outer = got.get("first") or {}
        print("\n=== 外层 data 字段内容（截断） ===")
        print(json.dumps(outer.get("data"), ensure_ascii=False)[:600])
        print("\n=== 外层 status/tags ===")
        print(json.dumps({k: outer.get(k) for k in ("status", "tags", "type")},
                         ensure_ascii=False)[:400])
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
