# -*- coding: utf-8 -*-
"""只读探针：有没有「已发布回答」的接口（带发布时间），以便不靠 DOM 抠数。

目标：为「进度校核」找一个**廉价、稳定**的线上数据源——
  · 今天的已发布篇数（按 created_time 判断，不用解析「2 小时前」这种相对时间）
  · 是否已发布过某 qid（去重）
  · 草稿箱当前篇数
全程只读，不做任何写操作。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    import datetime as _dt
    from applications.zhihu_story.browser_adapter import ZhihuBrowser

    # 候选接口：创作中心的「已发布回答」列表（带 created/updated）
    PROBE = r"""
async () => {
  const out = {};
  const tryGet = async (name, url) => {
    try {
      const r = await fetch(url, {credentials: 'include'});
      const t = await r.text();
      out[name] = {status: r.status, len: t.length, head: t.slice(0, 700)};
    } catch (e) { out[name] = {status: -1, err: String(e).slice(0, 120)}; }
  };
  await tryGet('creation_answer_v4',
    '/api/v4/members/me/creation/answer?limit=5&offset=0&type=answer');
  await tryGet('creator_answers_v3',
    '/api/v3/creator/answers?limit=5&offset=0');
  await tryGet('me_answers',
    '/api/v4/members/me/answers?limit=5&offset=0&include=data%5B*%5D.created_time%2Cquestion');
  await tryGet('self_answers',
    '/api/v4/members/self/answers?limit=5&offset=0');
  // 草稿箱列表接口（找得到就能不靠滚动）
  await tryGet('draft_list',
    '/api/v4/creator/answers/draft?limit=20&offset=0');
  return out;
}
"""

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/creator/manage/creation/answer",
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        got = b._safe_evaluate(PROBE) or {}
        for name, info in got.items():
            print("\n=== %s ===" % name)
            print("  status=%s len=%s" % (info.get("status"), info.get("len")))
            head = info.get("head") or info.get("err") or ""
            print("  head=%s" % head[:600].replace("\n", " "))
            if info.get("status") == 200 and (info.get("head") or "").startswith("{"):
                try:
                    data = json.loads(info["head"] + ("" if info["head"].rstrip().endswith("}") else ""))
                except Exception:
                    data = None
                if isinstance(data, dict):
                    rows = data.get("data") or []
                    if isinstance(rows, list) and rows:
                        r0 = rows[0]
                        print("  首条字段=%s" % sorted(r0.keys())[:18])
                        ct = r0.get("created_time") or r0.get("created") or 0
                        if ct:
                            print("  首条发布时间=%s" % _dt.datetime.fromtimestamp(int(ct)))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
