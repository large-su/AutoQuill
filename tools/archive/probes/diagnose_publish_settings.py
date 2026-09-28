# -*- coding: utf-8 -*-
"""只读探针：「发布设置」面板里有什么（找「送礼物 / 赞赏 / 收益」开关）。

★ 只点开「发布设置」看内容，**不点「发布回答」**（发布不可逆）。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

OPEN_SETTINGS_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  const btn = Array.from(document.querySelectorAll('button,div,span'))
      .filter(vis)
      .find(e => clean(e.innerText) === '发布设置');
  if (!btn) return {ok: false, reason: 'no-settings-button'};
  btn.click();
  return {ok: true, tag: btn.tagName, cls: String(btn.className || '').slice(0, 80)};
}
"""

PANEL_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  // 面板 = 含「发布设置/评论权限/送礼物」等关键词、且层级较高的容器
  const cands = Array.from(document.querySelectorAll('body *'))
      .filter(vis)
      .filter(e => parseInt(getComputedStyle(e).zIndex || '0', 10) >= 10
                   || /Modal|Drawer|Popover|Panel|Dialog/.test(String(e.className || '')))
      .map(e => ({ e: e, t: clean(e.innerText) }))
      .filter(x => /送礼物|赞赏|收益|评论权限|匿名|同步|声明|地区|话题/.test(x.t)
                   && x.t.length < 600);
  const pick = cands[cands.length - 1];
  if (!pick) {
    return {found: false,
            body_hits: Array.from(document.querySelectorAll('div,span,label'))
              .filter(vis)
              .map(e => clean(e.innerText))
              .filter(t => /送礼物|赞赏|收益/.test(t) && t.length < 60)
              .slice(0, 8)};
  }
  const switches = Array.from(pick.e.querySelectorAll(
      'button,[role=switch],input,label,div'))
      .filter(vis)
      .filter(e => /switch|Switch|toggle|Toggle|checkbox|Checkbox/.test(
          String(e.className || '') + (e.type || '')))
      .slice(0, 20)
      .map(e => ({ tag: e.tagName, cls: String(e.className || '').slice(0, 60),
                   checked: e.checked === undefined ? null : !!e.checked,
                   aria: e.getAttribute('aria-checked'),
                   html: e.outerHTML.slice(0, 200) }));
  return {found: true, cls: String(pick.e.className || '').slice(0, 80),
          text: pick.t.slice(0, 500), switches: switches,
          rows: Array.from(pick.e.querySelectorAll('div,label'))
            .filter(vis)
            .map(e => clean(e.innerText))
            .filter(t => t && t.length < 40)
            .slice(0, 30)};
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--qid", default="2063380350257063810")
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/question/%s#write" % args.qid,
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        print("打开发布设置：%s" % json.dumps(
            b._safe_evaluate(OPEN_SETTINGS_JS) or {}, ensure_ascii=False))
        time.sleep(4)
        panel = b._safe_evaluate(PANEL_JS) or {}
        print(json.dumps(panel, ensure_ascii=False, indent=2)[:3000])
        png = os.path.join("data", "cleanup", "publish_settings.png")
        os.makedirs(os.path.dirname(png), exist_ok=True)
        b.page.screenshot(path=png)
        print("截图：%s" % png)
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
