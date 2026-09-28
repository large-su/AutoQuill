# -*- coding: utf-8 -*-
"""只读诊断：草稿箱里「点了发布但 90s 未确认」的那篇，到底卡在哪。

★ 全程只导航与读取，**绝不点「发布回答」**（发布不可逆）。
   探明：
     1. 草稿箱现有几篇、最旧的是哪篇；
     2. 那篇的编辑页：URL / 「发布回答」按钮 / 可见弹窗 / 是否已是「编辑回答」（= 该问题下已有我们的回答）
     3. 服务端草稿 API 对每篇草稿返回什么（能对上本地 md 哪一篇）；
     4. 页面截图 + DOM 摘要，供人工核对。

运行：.venv/Scripts/python tools/archive/probes/diagnose_publish_stuck.py [--data-dir 路径]
输出：控制台摘要 + data/cleanup/publish_stuck_<ts>.json / .png
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())

DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

# 编辑页只读快照：按钮文案 + 可见弹窗 + 是否已有「编辑回答」入口
EDITOR_JS = r"""
() => {
  const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
  const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
      .join('').replace(/\s+/g, '').trim();
  const vis = e => e.offsetParent !== null;
  const btns = Array.from(document.querySelectorAll('button')).filter(vis)
      .map(b => ({ text: clean(b.innerText).slice(0, 20),
                   cls: String(b.className || '').slice(0, 80),
                   disabled: !!b.disabled }));
  const modals = Array.from(document.querySelectorAll(
      '[class*=Modal],[class*=modal],[role=dialog]'))
      .filter(m => vis(m))
      .map(m => ({ cls: String(m.className || '').slice(0, 80),
                   text: clean(m.innerText).slice(0, 120) }));
  const toasts = Array.from(document.querySelectorAll(
      '[class*=Toast],[class*=Notification],[class*=Message],[class*=alert]'))
      .filter(m => vis(m))
      .map(m => clean(m.innerText).slice(0, 120)).filter(Boolean);
  const ed = document.querySelector('.public-DraftEditor-content,[contenteditable=true]');
  return {
    url: location.href, title: document.title,
    publish_btn: btns.filter(b => b.text.indexOf('发布') >= 0),
    edit_btn: btns.filter(b => b.text.indexOf('编辑回答') >= 0
                              || b.text.indexOf('修改回答') >= 0),
    write_btn: btns.filter(b => b.text.indexOf('写回答') >= 0),
    all_buttons: btns.map(b => b.text).filter(Boolean).slice(0, 30),
    modals: modals.slice(0, 5), toasts: toasts.slice(0, 5),
    has_editor: !!ed,
    editor_text_head: ed ? clean(ed.innerText).slice(0, 100) : '',
    body_head: clean((document.body ? document.body.innerText : '')).slice(0, 200),
  };
}
"""

CARDS_JS = r"""() => Array.from(
    document.querySelectorAll('.CreationManage-CreationCard')).map((c, i) => {
  const a = c.querySelector('a[href*="/question/"][href*="#write"]');
  const t = c.querySelector('.CreationCardTitle-wrapper');
  const m = a ? a.href.match(/question\/(\d+)/) : null;
  return {index: i, qid: m ? m[1] : '', href: a ? a.href : '',
          title: t ? (t.innerText || '').trim() : '',
          text: (c.innerText || '').replace(/\s+/g, ' ').slice(0, 140)};
})"""

DRAFT_CONTENT_JS = r"""(qid) => fetch('/api/v4/questions/' + qid + '/draft',
        {credentials: 'include'})
    .then(r => r.ok ? r.json() : {__status: r.status})
    .then(d => ({ok: true, keys: Object.keys(d || {}),
                 content_len: ((d || {}).content || '').length,
                 content_head: ((d || {}).content || '').slice(0, 120),
                 raw_keys_status: (d || {}).__status || 0}))
    .catch(e => ({ok: false, err: String(e)}))"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="", help="指定数据目录（默认源码态项目 data）")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    report = {"at": datetime.now().isoformat(timespec="seconds")}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        for _ in range(5):
            b._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.2)
        cards = b._safe_evaluate(CARDS_JS) or []
        report["url"] = b.page.url
        report["needs_login"] = "/signin" in (b.page.url or "")
        report["cards"] = cards
        print("草稿箱 url=%s 需要登录=%s 卡片=%d 篇"
              % (report["url"], report["needs_login"], len(cards)))
        for c in cards:
            print("  [%d] qid=%s 《%s》 %s"
                  % (c.get("index"), c.get("qid"), (c.get("title") or "")[:40],
                     c.get("href")))
        # 每篇草稿的服务端容器状态
        report["drafts_api"] = {}
        for c in cards:
            qid = str(c.get("qid") or "")
            if not qid:
                continue
            info = b._safe_evaluate(DRAFT_CONTENT_JS, qid) or {}
            report["drafts_api"][qid] = info
            print("  草稿 API qid=%s -> len=%s head=%r"
                  % (qid, info.get("content_len"), (info.get("content_head") or "")[:60]))
        if not cards:
            print("！草稿箱没有卡片（可能已全部发布 / 登录态失效）")
        else:
            oldest = cards[-1]
            print("\n最旧的一篇（发布任务会挑它）：《%s》" % oldest.get("title"))
            b.page.goto(oldest["href"], wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)
            report["editor"] = b._safe_evaluate(EDITOR_JS) or {}
            ed = report["editor"]
            print("  编辑页 url=%s" % ed.get("url"))
            print("  has_editor=%s 发布类按钮=%s"
                  % (ed.get("has_editor"), json.dumps(ed.get("publish_btn"),
                                                      ensure_ascii=False)))
            print("  「编辑回答/修改回答」按钮=%s"
                  % json.dumps(ed.get("edit_btn"), ensure_ascii=False))
            print("  可见弹窗=%s" % json.dumps(ed.get("modals"), ensure_ascii=False))
            print("  可见提示=%s" % json.dumps(ed.get("toasts"), ensure_ascii=False))
            print("  正文开头=%r" % (ed.get("editor_text_head") or "")[:80])
            outdir = os.path.join("data", "cleanup")
            os.makedirs(outdir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            png = os.path.join(outdir, "publish_stuck_%s.png" % stamp)
            try:
                b.page.screenshot(path=png, full_page=False)
                report["screenshot"] = png
                print("  截图：%s" % png)
            except Exception as exc:              # noqa: BLE001
                print("  截图失败：%s" % exc)
    finally:
        try:
            b.close()
        except Exception:                          # noqa: BLE001
            pass
    outdir = os.path.join("data", "cleanup")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "publish_stuck_%s.json"
                        % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("report:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
