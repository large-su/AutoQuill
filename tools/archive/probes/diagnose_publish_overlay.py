# -*- coding: utf-8 -*-
"""一次性真机诊断 2：点「发布回答」后**每秒截一张图**，看清到底弹了什么。

★ 会真的点一次「发布回答」（不可逆动作；同 diagnose_publish_click）。
   本探针只回答一个问题：点下去之后页面上到底出现了什么（弹窗/报错/盖层）。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

CLICK_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, '').trim();
  const hit = Array.from(document.querySelectorAll('button'))
      .find(b => clean(b.innerText) === '发布回答' && b.offsetParent !== null);
  if (!hit) return { ok: false, reason: 'no-button' };
  hit.click();
  return { ok: true };
}
"""

# 盖层/弹窗探测：viewport 中心点上最顶层元素是谁；有哪些高 z-index 的固定层
OVERLAY_JS = r"""
() => {
  const vis = e => e.offsetParent !== null;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cx = Math.round(window.innerWidth / 2);
  const cy = Math.round(window.innerHeight / 2);
  const top = document.elementFromPoint(cx, cy);
  const desc = el => {
    if (!el) return '';
    const cs = getComputedStyle(el);
    return el.tagName + '.' + String(el.className || '').slice(0, 60)
        + ' z=' + cs.zIndex + ' pos=' + cs.position;
  };
  // 高 z-index 的固定/绝对定位层（弹窗、遮罩、抽屉都在这儿）
  const layers = Array.from(document.querySelectorAll('body *'))
      .filter(e => vis(e))
      .map(e => ({ e: e, cs: getComputedStyle(e) }))
      .filter(x => (x.cs.position === 'fixed' || x.cs.position === 'absolute')
                   && parseInt(x.cs.zIndex || '0', 10) >= 100)
      .slice(0, 12)
      .map(x => ({ cls: String(x.e.className || '').slice(0, 70),
                   z: x.cs.zIndex,
                   text: clean(x.e.innerText).slice(0, 150),
                   rect: (function (r) { return [Math.round(r.width),
                                                 Math.round(r.height)]; })(
                             x.e.getBoundingClientRect()) }));
  const dialogs = Array.from(document.querySelectorAll(
      '[role=dialog],[aria-modal=true],[class*=Modal],[class*=Dialog]'))
      .filter(vis)
      .map(e => ({ cls: String(e.className || '').slice(0, 70),
                   text: clean(e.innerText).slice(0, 200) }));
  return { center: desc(top), layers: layers, dialogs: dialogs.slice(0, 6),
           body_scroll_h: document.body.scrollHeight };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"C:\Users\10162\AppData\Roaming\AutoQuill")
    ap.add_argument("--qid", default="2063380350257063810")
    ap.add_argument("--seconds", type=int, default=12)
    args = ap.parse_args()
    os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    outdir = os.path.join("data", "cleanup", "click_frames")
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {"at": stamp}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto("https://www.zhihu.com/question/%s#write" % args.qid,
                    wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        report["before"] = b._safe_evaluate(OVERLAY_JS) or {}
        b.page.screenshot(path=os.path.join(outdir, "%s_t00_before.png" % stamp))
        print("点击前中心元素：%s" % report["before"].get("center"))
        print("点击前 layers=%s" % json.dumps(report["before"].get("layers"),
                                              ensure_ascii=False)[:400])
        clicked = b._safe_evaluate(CLICK_JS) or {}
        print("点击：%s" % json.dumps(clicked, ensure_ascii=False))
        frames = []
        for i in range(1, max(2, args.seconds) + 1):
            time.sleep(1)
            snap = b._safe_evaluate(OVERLAY_JS) or {}
            png = os.path.join(outdir, "%s_t%02d.png" % (stamp, i))
            try:
                b.page.screenshot(path=png)
            except Exception:                 # noqa: BLE001
                png = ""
            frames.append({"t": i, "center": snap.get("center"),
                           "layers": snap.get("layers"),
                           "dialogs": snap.get("dialogs"), "png": png})
            print("[%2ds] center=%s" % (i, snap.get("center")))
            for L in (snap.get("layers") or [])[:4]:
                print("      layer z=%s %s rect=%s text=%r"
                      % (L.get("z"), (L.get("cls") or "")[:50], L.get("rect"),
                         (L.get("text") or "")[:80]))
            for D in (snap.get("dialogs") or [])[:3]:
                print("      dialog %s :: %r" % ((D.get("cls") or "")[:50],
                                                 (D.get("text") or "")[:120]))
        report["frames"] = frames
    finally:
        try:
            b.close()
        except Exception:                      # noqa: BLE001
            pass
    path = os.path.join("data", "cleanup", "click_frames_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("report:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
