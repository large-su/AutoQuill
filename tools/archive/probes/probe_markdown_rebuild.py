# -*- coding: utf-8 -*-
# ============================================================
# probe_markdown_rebuild.py — 逐块重建 walker 的真机验证（合成 DOM）
#
# 背景：网页端把 markdown 渲染成 DOM（## **N** 变成 h2），只读 innerText 的
# 通道会把章节标题丢成裸章节号 → 格式校验「章节 0 个」扣 4 分判废。
# base.MARKDOWN_REBUILD_JS 是 DeepSeek / 豆包共用的逐块重建实现。
#
# 本脚本不登录、不联网：用合成 DOM 在真实浏览器引擎里跑同一段 JS，
# 验证「h2 -> ## **N**」与段落拼接确实成立（字符串断言之外的语义验证）。
#
# 运行：.venv/Scripts/python tools/archive/probes/probe_markdown_rebuild.py
# ============================================================

import os
import sys

sys.path.insert(0, os.getcwd())

from web_drivers.base import MARKDOWN_REBUILD_JS

HTML = (
    '<div class="ds-message">'
    '  <div class="ds-assistant-message-main-content">'
    '    <p>引言第一段。</p>'
    '    <h2>1</h2><p>第一章正文。</p>'
    '    <h3>2</h3><p>第二章正文。</p>'
    '    <div><p>嵌套块也要读到。</p></div>'
    '  </div>'
    '</div>'
)

JS = (
    '() => {'
    + MARKDOWN_REBUILD_JS
    + '  const el = document.querySelector('
    + '      "div[class*=ds-assistant-message-main-content]");'
    + '  return el ? (toMarkdown(el) || el.innerText || "") : "";'
    + '}'
)


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        last = None
        browser = None
        for kwargs in ({}, {"channel": "msedge"}):
            try:
                browser = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:
                last = exc
        if browser is None:
            print("无法启动浏览器：%s" % last)
            return 2
        try:
            page = browser.new_page()
            page.set_content(HTML)
            out = page.evaluate(JS)
        finally:
            browser.close()
    print("---- 重建结果 ----")
    print(out)
    print("------------------")
    ok = ("## **1**" in out and "### **2**" in out
          and "第一章正文。" in out and "嵌套块也要读到。" in out)
    print("逐块重建：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())