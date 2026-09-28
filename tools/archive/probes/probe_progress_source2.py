# -*- coding: utf-8 -*-
"""只读探针：创作中心「已发布回答」页的数据到底从哪来（接口 or DOM）。

为进度校核找**最稳的数据源**：优先接口（结构化、带 created_time），
退而求其次才是 DOM（相对时间「2 小时前」需要解析）。
只读，且只抓取页面自身已经发出的请求。
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.getcwd())

URL = "https://www.zhihu.com/creator/manage/creation/answer"

SHAPE_JS = r"""
() => {
  const out = {init_state_keys: [], scripts: [], globals: []};
  for (const k of Object.keys(window)) {
    if (/INITIAL|__NEXT|preload|STATE/i.test(k)) out.globals.push(k);
  }
  if (window.__INITIAL_STATE__) {
    out.init_state_keys = Object.keys(window.__INITIAL_STATE__).slice(0, 20);
  }
  out.scripts = Array.from(document.querySelectorAll('script[src]'))
      .map(s => s.src).slice(0, 12);
  const root = document.querySelector('#root, #app, .CreatorHome, [class*=Creation]');
  out.root_cls = root ? String(root.className || '').slice(0, 80) : '';
  out.card_count = document.querySelectorAll(
      '.CreationManage-CreationCard').length;
  return out;
}
"""

BUNDLE_JS = r"""
async () => {
  const srcs = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
  const found = new Set();
  const re = /\/api\/v[0-9]+\/[A-Za-z0-9_\/\-]*(creation|creator|answer|draft)[A-Za-z0-9_\/\-]*/g;
  for (const src of srcs.slice(0, 25)) {
    try {
      const t = await (await fetch(src)).text();
      let m;
      while ((m = re.exec(t)) !== null) { found.add(m[0]); if (found.size > 40) break; }
    } catch (e) { /* 忽略 */ }
  }
  return Array.from(found).slice(0, 40);
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    seen = []

    def on_resp(resp):
        try:
            u = resp.url or ""
            if "api" in u and re.search(r"creation|creator|answer|draft", u, re.I):
                if not any(x["url"] == u for x in seen):
                    seen.append({"url": u[:160], "status": resp.status})
        except Exception:                     # noqa: BLE001
            pass

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.on("response", on_resp)
        b.page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        for _ in range(3):
            b._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.5)
        print("=== 页面形状 ===")
        print(" ", b._safe_evaluate(SHAPE_JS))
        print("\n=== 页面自身发出的相关请求 ===")
        for x in seen:
            print("  [%s] %s" % (x["status"], x["url"]))
        print("\n=== 脚本里出现的候选接口 ===")
        for p in (b._safe_evaluate(BUNDLE_JS) or []):
            print("  %s" % p)
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
