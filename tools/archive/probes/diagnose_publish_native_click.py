# -*- coding: utf-8 -*-
"""一次性真机诊断 3：JS .click() vs Playwright 真实鼠标点击——哪个能触发发布请求。

★ 会真的尝试发布一次（不可逆）。目的：定位「点发布没任何反应」是
   「程序化 click 不被受理」还是「内容/账号被服务端前置拒绝」。
   全程抓包（Playwright response 事件），只看到底有没有 POST 出去。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--qid", default="2063380350257063810")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    net = []

    def on_resp(resp):
        try:
            u = resp.url
            if not re.search(r"publish|answer|draft", u, re.I):
                return
            body = ""
            try:
                body = resp.text()[:300]
            except Exception:                 # noqa: BLE001
                body = "<no body>"
            net.append({"status": resp.status, "method": resp.request.method,
                        "url": u[:140], "body": body})
            print("    NET [%s] %s %s" % (resp.status, resp.request.method, u[:110]))
        except Exception:                     # noqa: BLE001
            pass

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.on("response", on_resp)
        b.page.goto("https://www.zhihu.com/question/%s#write" % args.qid,
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        loc = b.page.get_by_role("button", name=re.compile("^发布回答$"))
        print("按钮数量=%d" % loc.count())
        if loc.count() == 0:
            print("找不到「发布回答」按钮，退出")
            return 1
        target = loc.first
        print("按钮可见=%s enabled=%s" % (target.is_visible(), target.is_enabled()))
        print("\n>>> 用 Playwright 真实鼠标点击（trusted event）")
        try:
            target.click(timeout=8000)
            print("    click() 返回正常")
        except Exception as exc:              # noqa: BLE001
            print("    click() 抛异常：%s" % exc)
        for i in range(1, 21):
            time.sleep(1)
            url = b.page.url or ""
            if "/answer/" in url:
                print("    [%2ds] URL 已跳到回答页：%s" % (i, url))
                break
            if i % 5 == 0:
                dlen = len(b.get_draft_content(args.qid) or "")
                print("    [%2ds] url=%s 草稿长度=%d" % (i, url[:70], dlen))
                if dlen == 0:
                    print("    → 草稿已清空：发布成功")
                    break
        print("\n抓到的请求：%d 条" % len(net))
        for n in net:
            print("  [%s] %s %s" % (n["status"], n["method"], n["url"][:100]))
            print("      %s" % (n["body"] or "")[:200])
    finally:
        try:
            b.close()
        except Exception:                      # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
