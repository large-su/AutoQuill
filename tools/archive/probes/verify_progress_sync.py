# -*- coding: utf-8 -*-
"""真机验证：进度校核读到的数字，是否等于创作中心的事实。

只读：只调两个只读接口 + 落盘快照，不做任何写操作。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--ledger-units", type=int, default=-1,
                    help="台账口径的已发布数（用于记差异）；-1 = 自动取")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from webui import site_progress

    ledger = None
    if args.ledger_units >= 0:
        ledger = args.ledger_units
    else:
        from automation import store
        day = store.now_str()[:10]
        ledger = store.done_counts(day).get("publish_drafts", 0)
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        snap = site_progress.refresh(b, ledger_units=ledger, note="人工校核验证",
                                     force=True)
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    if snap is None:
        print("✗ 没读到线上状态（保持本地计数，不猜）")
        return 1
    print("✓ 线上事实：")
    print("   今日已发布 %d 篇 · 待发草稿 %d 篇（%s）"
          % (snap.published_today, snap.drafts_pending, snap.at))
    print("   台账口径 publish_drafts = %s" % ledger)
    print("   取 max 后的计数 = %s"
          % max(int(ledger or 0), snap.published_today))
    print("   今日明细（前 5 条）：")
    for it in list(snap.published)[:5]:
        print("     %s  qid=%s  《%s》"
              % (it.get("created_time"), it.get("qid"), it.get("title")[:34]))
    st = site_progress.status_payload(snap.day)
    print("\n   界面将显示：%s" % site_progress.summary_line(snap.day))
    print("   差异记录：%s"
          % json.dumps(st.get("reconcile") or [], ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
