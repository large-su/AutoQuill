# -*- coding: utf-8 -*-
"""只读/演练验证：改写后的 publish_draft 主流程还能不能正常定位与判定。

dry_run=True：只走「打开草稿箱 → 打开编辑页 → 确认发布按钮在」，
**绝不点击发布**（发布不可逆）。用来确认这次重构没有把正常路径改坏。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        logs = []
        r = b.publish_draft(dry_run=True,
                            progress=lambda t: (logs.append(t), print("   · %s" % t)))
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    print("\n结果：%s" % json.dumps(r, ensure_ascii=False, indent=2))
    print("监听器残留：%s" % (getattr(b, "page", None) is None))
    return 0 if r.get("reason") == "dry_run" else 1


if __name__ == "__main__":
    sys.exit(main())
