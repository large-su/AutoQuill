# -*- coding: utf-8 -*-
# ============================================================
# probe_manage_reply.py — 评论管理页「卡片内回复」DOM 探测（只输入不发送）
#
# 目的：把回复从「回答页」搬到「管理页卡片」（评论就是从那儿挑的、必然在场）。
#   1) 卡片上的「回复」按钮点开后是什么编辑器？发布按钮在哪、判据是什么？
#   2) 已经回复过的评论，在管理页卡片上长什么样（防重复回复的判据）？
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_manage_reply.py [要探测的评论关键字]
# ============================================================

import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

NEEDLE = sys.argv[1] if len(sys.argv) > 1 else ''
REPLY_TEXT = '（演练）测试回复不发送'

CLICK_REPLY_JS = '''
(arg) => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const clean = s => (s || '').split(ZW).join(' ').replace(/[ ]+/g, ' ').trim();
  const cards = Array.from(document.querySelectorAll('.CommentManage-CommentCard'));
  const want = clean(arg.needle);
  let idx = -1;
  for (let i = 0; i < cards.length; i++) {
    if (want && clean(cards[i].innerText).indexOf(want) >= 0) { idx = i; break; }
  }
  if (idx < 0) { if (arg.fallback_first && cards.length) idx = 0; else return { ok: false, reason: 'no-card' }; }
  const card = cards[idx];
  const btn = Array.from(card.querySelectorAll('button')).find(b => clean(b.innerText) === '回复');
  if (!btn) return { ok: false, reason: 'no-reply-button', idx: idx };
  btn.click();
  return { ok: true, idx: idx, card: clean(card.innerText).slice(0, 60) };
}
'''

EDITOR_JS = '''
() => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const clean = s => (s || '').split(ZW).join(' ').replace(/[ ]+/g, ' ').trim();
  const eds = Array.from(document.querySelectorAll('div.public-DraftEditor-content,[contenteditable=true],textarea'))
      .filter(e => e.offsetParent);
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetParent && clean(b.innerText) === '发布');
  return {
    editors: eds.map(e => ({ tag: e.tagName, cls: String(e.className).slice(0, 60),
                            focused: document.activeElement === e || e.contains(document.activeElement),
                            text: clean(e.innerText).slice(0, 40) })),
    publish_buttons: pubs.map(b => ({ disabled: !!b.disabled, cls: String(b.className).slice(0, 50) })),
    card_count: document.querySelectorAll('.CommentManage-CommentCard').length
  };
}
'''


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        got = b.collect_manage_comments(limit=20)
        cards = got.get('comments') or []
        print('管理页卡片 %d 张' % len(cards))
        target = None
        if NEEDLE:
            target = next((c for c in cards if NEEDLE in (c.get('text') or '')), None)
        if target is None:
            target = cards[0] if cards else None
            print('（未指定关键字或没找到，改用第一张卡）')
        if target is None:
            print('!! 没有卡片可用，退出')
            return 1
        print('目标评论：%s | %s' % (target.get('author'), (target.get('text') or '')[:40]))
        out['target'] = {k: target.get(k) for k in ('author', 'text', 'answer_url', 'time_text', 'key')}

        r = b._safe_evaluate(CLICK_REPLY_JS,
                             {'needle': (target.get('text') or '')[:24], 'fallback_first': True})
        out['click'] = r
        print('点「回复」：', json.dumps(r, ensure_ascii=False)[:160])
        time.sleep(3)
        st = b._safe_evaluate(EDITOR_JS) or {}
        out['editor'] = st
        print('编辑器：', json.dumps(st, ensure_ascii=False)[:300])
        # 输入测试文字（不发送），确认「发布」是否变可用
        try:
            b.page.keyboard.type(REPLY_TEXT)
        except Exception as exc:            # noqa: BLE001
            print('输入失败：', str(exc)[:80])
        time.sleep(2)
        st2 = b._safe_evaluate(EDITOR_JS) or {}
        out['after_type'] = st2
        print('输入后：', json.dumps(st2, ensure_ascii=False)[:300])
        # 清空，绝不发送
        try:
            b.page.keyboard.press('Control+A')
            b.page.keyboard.press('Delete')
            time.sleep(1)
        except Exception:                   # noqa: BLE001
            pass
        st3 = b._safe_evaluate(EDITOR_JS) or {}
        out['after_clear'] = st3
        print('清空后：', json.dumps(st3, ensure_ascii=False)[:200])
    finally:
        b.close()
    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'probe_manage_reply.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
