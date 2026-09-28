# -*- coding: utf-8 -*-
"""一次性真机诊断：把「点发布 → 等待确认」这一段完整插桩跑一遍。

★ 这一步**会真的点「发布回答」**（不可逆）：只在用户已授权发布草稿的前提下使用。
   目的：拿到线上一直失败的那篇草稿的真实反馈（弹窗文案 / 接口响应 / URL 变化），
   据此把 publish_draft 的确认逻辑修对。

运行：.venv/Scripts/python tools/archive/probes/diagnose_publish_click.py [--data-dir 路径]
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

# 点击前：把按钮与周边容器拍下来（确认点的是主按钮，不是「发布设置」）
PRE_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, '').trim();
  const vis = e => e.offsetParent !== null;
  const all = Array.from(document.querySelectorAll('button')).filter(vis);
  return {
    url: location.href,
    buttons: all.map(b => ({ text: clean(b.innerText).slice(0, 16),
                             disabled: !!b.disabled,
                             cls: String(b.className || '').slice(0, 60) })),
    draft_bar: (function () {
      const el = Array.from(document.querySelectorAll('div,span'))
          .filter(e => vis(e) && /草稿|字数/.test(clean(e.innerText))
                       && clean(e.innerText).length < 40);
      return el.length ? clean(el[0].innerText).slice(0, 60) : '';
    })(),
  };
}
"""

# 点击后：可见弹窗 / 提示 / 按钮变化 / 是否有错误文案
POST_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, ' ').trim();
  const vis = e => e.offsetParent !== null;
  const modals = Array.from(document.querySelectorAll(
      '[class*=Modal],[class*=modal],[role=dialog],[class*=Popover],[class*=Tooltip]'))
      .filter(vis)
      .map(m => ({ cls: String(m.className || '').slice(0, 70),
                   text: clean(m.innerText).slice(0, 200) }))
      .filter(m => m.text);
  const toasts = Array.from(document.querySelectorAll(
      '[class*=Toast],[class*=Notification],[class*=Message],[class*=Alert],[class*=Banner]'))
      .filter(vis).map(m => clean(m.innerText).slice(0, 200)).filter(Boolean);
  const btns = Array.from(document.querySelectorAll('button')).filter(vis)
      .map(b => clean(b.innerText).slice(0, 16)).filter(Boolean);
  const err = Array.from(document.querySelectorAll('div,span,p'))
      .filter(vis)
      .filter(e => /失败|错误|异常|违规|不能|无法|请先|审核/.test(clean(e.innerText))
                   && clean(e.innerText).length < 120)
      .slice(0, 6).map(e => clean(e.innerText));
  return { url: location.href, modals: modals.slice(0, 6),
           toasts: toasts.slice(0, 6), buttons: btns.slice(0, 25),
           err_hits: err };
}
"""

CLICK_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, '').trim();
  const hit = Array.from(document.querySelectorAll('button'))
      .find(b => clean(b.innerText) === '发布回答' && b.offsetParent !== null);
  if (!hit) return { ok: false, reason: 'no-button' };
  hit.click();
  return { ok: true, cls: String(hit.className || '').slice(0, 80),
           disabled: !!hit.disabled };
}
"""

# 安装一个 fetch 记录器：把发布相关的 XHR 响应截下来（页面内，只读）
HOOK_JS = r"""
() => {
  if (window.__aq_hook) return 'already';
  window.__aq_log = [];
  const of = window.fetch;
  window.fetch = function () {
    const url = String(arguments[0] && arguments[0].url || arguments[0] || '');
    const p = of.apply(this, arguments);
    if (/publish|answer|draft/i.test(url)) {
      p.then(r => {
        const c = r.clone();
        c.text().then(t => window.__aq_log.push(
            { url: url.slice(0, 120), status: r.status, body: String(t).slice(0, 400) }))
         .catch(() => {});
      }).catch(() => {});
    }
    return p;
  };
  const ox = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__aq_url = String(u || '');
    return ox.apply(this, arguments);
  };
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', () => {
      if (/publish|answer|draft/i.test(this.__aq_url || '')) {
        window.__aq_log.push({ url: String(this.__aq_url).slice(0, 120),
                               status: this.status,
                               body: String(this.responseText || '').slice(0, 400) });
      }
    });
    return os.apply(this, arguments);
  };
  window.__aq_hook = true;
  return 'installed';
}
"""

READ_LOG_JS = r"""() => (window.__aq_log || []).slice(-25)"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="", help="指定数据目录")
    ap.add_argument("--qid", default="2063380350257063810", help="目标草稿的 qid")
    ap.add_argument("--wait", type=int, default=120, help="点完后的观察秒数")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    report = {"at": datetime.now().isoformat(timespec="seconds"), "qid": args.qid}
    b = ZhihuBrowser(headless=True)
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    net = []

    def _on_response(resp):
        """Playwright 层抓包（页面内 hook fetch 可能被应用层缓存的引用绕过）。"""
        try:
            u = resp.url
            if not re.search(r"publish|answer|draft|comment", u, re.I):
                return
            body = ""
            try:
                body = resp.text()[:400]
            except Exception:                 # noqa: BLE001
                body = "<body unavailable>"
            net.append({"status": resp.status, "method": resp.request.method,
                        "url": u[:150], "body": body})
        except Exception:                     # noqa: BLE001
            pass

    try:
        b.start()
        b.page.on("response", _on_response)
        url = "https://www.zhihu.com/question/%s#write" % args.qid
        b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        for _ in range(10):
            if b._safe_evaluate(
                    "() => !!document.querySelector('.public-DraftEditor-content')"):
                break
            time.sleep(1.5)
        print("hook:", b._safe_evaluate(HOOK_JS))
        pre = b._safe_evaluate(PRE_JS) or {}
        report["pre"] = pre
        print("点击前 url=%s" % pre.get("url"))
        print("  按钮：%s" % json.dumps(pre.get("buttons"), ensure_ascii=False)[:600])
        print("  草稿状态条：%r" % pre.get("draft_bar"))
        draft_before = b.get_draft_content(args.qid) or ""
        report["draft_len_before"] = len(draft_before)
        print("  服务端草稿长度：%d" % len(draft_before))

        clicked = b._safe_evaluate(CLICK_JS) or {}
        report["click"] = clicked
        print("点击结果：%s" % json.dumps(clicked, ensure_ascii=False))

        steps = []
        deadline = time.time() + max(10, args.wait)
        step = 0
        while time.time() < deadline:
            time.sleep(3)
            step += 1
            snap = b._safe_evaluate(POST_JS) or {}
            dlen = len(b.get_draft_content(args.qid) or "")
            row = {"t": step * 3, "url": snap.get("url"), "draft_len": dlen,
                   "modals": snap.get("modals"), "toasts": snap.get("toasts"),
                   "err_hits": snap.get("err_hits"),
                   "buttons": snap.get("buttons")}
            steps.append(row)
            print("[%3ds] url=%s draft=%d modals=%s toasts=%s err=%s"
                  % (row["t"], (row["url"] or "")[:70], dlen,
                     json.dumps(row["modals"], ensure_ascii=False)[:160],
                     json.dumps(row["toasts"], ensure_ascii=False)[:120],
                     json.dumps(row["err_hits"], ensure_ascii=False)[:120]))
            if "/answer/" in (row["url"] or ""):
                print("  → URL 已跳到回答页：发布成功")
                break
            if dlen == 0 and step > 2:
                print("  → 服务端草稿已清空：发布成功")
                break
        report["steps"] = steps
        report["net"] = b._safe_evaluate(READ_LOG_JS) or []
        report["net_playwright"] = net
        print("\nPlaywright 层抓包（publish/answer/draft/comment 相关）：")
        for n in net:
            print("  [%s] %s %s" % (n.get("status"), n.get("method"),
                                    (n.get("url") or "")[:100]))
            print("        %s" % (n.get("body") or "")[:240])
        try:
            png = os.path.join(outdir, "publish_click_%s.png" % stamp)
            b.page.screenshot(path=png, full_page=False)
            report["screenshot"] = png
            print("截图：%s" % png)
        except Exception as exc:                  # noqa: BLE001
            print("截图失败：%s" % exc)
    finally:
        try:
            b.close()
        except Exception:                          # noqa: BLE001
            pass
    path = os.path.join(outdir, "publish_click_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("report:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
