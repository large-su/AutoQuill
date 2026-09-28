# -*- coding: utf-8 -*-
"""修复验证（真机）：走生产代码 publish_draft，只发草稿箱里最旧的一篇。

用途：验证「送礼物被拒 → 关掉 → 重试」这条修复在真机上真的能发布成功。
★ 会真的发布一篇公开回答（不可逆）；只发草稿箱里**最旧**的一篇（与自动化同口径）。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--qid", default="2063380350257063810",
                    help="指定要发布的草稿 qid（空 = 最旧的一篇）")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    net = []

    def on_resp(resp):
        try:
            if re.search(r"/api/v4/content/publish", resp.url or ""):
                body = ""
                try:
                    body = (resp.text() or "")[:200]
                except Exception:                 # noqa: BLE001
                    pass
                net.append((resp.status, body))
                print("   NET 发布回执 [%s] %s" % (resp.status, body))
        except Exception:                         # noqa: BLE001
            pass

    logs = []
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.on("response", on_resp)
        r = b.publish_draft(qid=args.qid,
                            progress=lambda t: (logs.append(t),
                                                print("   · %s" % t)))
    finally:
        try:
            b.close()
        except Exception:                         # noqa: BLE001
            pass
    print("\n结果：%s" % json.dumps(r, ensure_ascii=False, indent=2))
    print("回执条数：%d" % len(net))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
