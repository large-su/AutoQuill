# -*- coding: utf-8 -*-
"""诊断：知乎登录页到底给了什么（排查「登不上/登完没反应」）。

用法：python tools/archive/probes/probe_signin_page.py [--profile]
  默认用**临时干净上下文**打开 https://www.zhihu.com/signin，只读地把页面
  状态打出来（URL/标题/是否出现二维码/按钮文案/正文开头/命中风控词）。
  --profile 改为用 AutoQuill 的持久化 profile 打开（会占用 profile 锁，
  先确认没有其它 AutoQuill 浏览器在跑）。

2026-09-23 事故背景：用户报告「重新登录后没自动保存并关闭」。窗口在登录页
停留 5 分钟、历史里没有任何「登录后跳首页」的访问记录，最终实测仍是登出态
——需要看清登录页本身是不是可用（二维码有没有渲染、是否被风控拦截）。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.getcwd())

RISK_WORDS = ("异常", "验证", "安全", "风险", "captcha", "被限制", "操作频繁", "登录已过期")


def dump(page, tag):
    time.sleep(6)          # 等 SPA 渲染出二维码/表单
    info = page.evaluate(r"""() => {
        const q = (sel) => document.querySelectorAll(sel).length;
        const txt = (document.body ? document.body.innerText : "") || "";
        return {
          url: location.href,
          title: document.title || "",
          qr: q("img[src*=qr], canvas, .Qrcode, [class*=Qrcode], [class*=qrcode]"),
          inputs: q("input"),
          buttons: Array.from(document.querySelectorAll("button"))
              .map(b => (b.innerText || "").replace(/\s+/g, " ").trim())
              .filter(Boolean).slice(0, 12),
          body: txt.replace(/\s+/g, " ").slice(0, 260)
        };
    }""")
    hits = [w for w in RISK_WORDS if w in (info.get("body") or "")]
    print("[%s] url=%s" % (tag, info.get("url")))
    print("[%s] title=%s" % (tag, info.get("title")))
    print("[%s] 二维码候选节点=%s 输入框=%s" % (tag, info.get("qr"), info.get("inputs")))
    print("[%s] buttons=%s" % (tag, info.get("buttons")))
    print("[%s] 正文开头=%s" % (tag, info.get("body")))
    print("[%s] 风控/异常词命中=%s" % (tag, hits or "无"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="store_true",
                    help="用 AutoQuill 持久化 profile（会占 profile 锁）")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    from applications.zhihu_story.browser_utils import EDGE_PATH, USER_DATA_DIR
    print("edge =", EDGE_PATH)
    with sync_playwright() as p:
        if args.profile:
            print("profile =", USER_DATA_DIR)
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR, executable_path=EDGE_PATH,
                headless=True, locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"])
        else:
            b = p.chromium.launch(executable_path=EDGE_PATH, headless=True)
            ctx = b.new_context(locale="zh-CN")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.zhihu.com/signin",
                      wait_until="domcontentloaded", timeout=35000)
            dump(page, "signin")
            page.goto("https://www.zhihu.com/",
                      wait_until="domcontentloaded", timeout=35000)
            dump(page, "home")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()