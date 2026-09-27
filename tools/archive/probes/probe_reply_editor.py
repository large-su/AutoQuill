# -*- coding: utf-8 -*-
# ============================================================
# probe_reply_editor.py - 回答页「回复某条评论」的编辑器流程（只输入，不发送）
#
# 目的：把「点回复 → 编辑器出现 → 输入 → 发送按钮可用 → 清空关闭」摸清，
#       避免评论回复链路凭空写代码。全程不点发送、不产生任何真实评论。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_reply_editor.py [回答URL] [评论序号]
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

DEFAULT_URL = 'https://www.zhihu.com/answer/2087106680509166319'

OPEN_COMMENTS_JS = '''
() => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const zwRe = new RegExp('[' + ZW + ']', 'g');
  const clean = s => (s || '').replace(zwRe, '').trim();
  const btns = Array.from(document.querySelectorAll('button,.ContentItem-action'));
  const hit = btns.find(b => /条评论/.test(clean(b.innerText)));
  if (hit) { hit.click(); return clean(hit.innerText); }
  return '';
}
'''

# 找到第 idx 条评论的「回复」按钮并点开编辑器
CLICK_REPLY_JS = '''
(idx) => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const zwRe = new RegExp('[' + ZW + ']', 'g');
  const clean = s => (s || '').replace(zwRe, ' ').replace(/[ ]+/g, ' ').trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || '');
  const contents = Array.from(document.querySelectorAll('.CommentContent'));
  const cc = contents[idx];
  if (!cc) return { ok: false, reason: '没有第 ' + idx + ' 条评论' };
  let cur = cc.parentElement;
  let holder = null;
  for (let k = 0; k < 5 && cur; k++) {
    if (Array.from(cur.querySelectorAll('button')).some(b => txt(b) === '回复')) { holder = cur; break; }
    cur = cur.parentElement;
  }
  if (!holder) return { ok: false, reason: '找不到该评论的容器' };
  const btn = Array.from(holder.querySelectorAll('button')).find(b => txt(b) === '回复');
  if (!btn) return { ok: false, reason: '找不到回复按钮' };
  const nestedBefore = holder.querySelectorAll('.CommentContent').length;
  btn.click();
  return { ok: true, text: txt(cc).slice(0, 60), nestedBefore: nestedBefore };
}
'''

EDITOR_JS = '''
() => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const zwRe = new RegExp('[' + ZW + ']', 'g');
  const clean = s => (s || '').replace(zwRe, ' ').trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || '');
  const cls = el => (el && typeof el.className === 'string' ? el.className : '');
  const editables = Array.from(document.querySelectorAll('[contenteditable=true],textarea'))
      .map(el => ({ tag: el.tagName, cls: cls(el).slice(0, 80),
                    placeholder: el.getAttribute('placeholder') || '',
                    focused: document.activeElement === el,
                    visible: !!el.offsetParent, text: txt(el).slice(0, 40) }));
  const buttons = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent)
      .map(b => ({ text: txt(b).slice(0, 12), cls: cls(b).slice(0, 60), disabled: !!b.disabled }))
      .filter(b => b.text).slice(-12);
  return { editables: editables, buttons: buttons };
}
'''


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    out = {'url': url, 'idx': idx}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        out['open'] = b._safe_evaluate(OPEN_COMMENTS_JS)
        print('1) 展开评论：', out['open'])
        time.sleep(5)
        for _ in range(3):
            b._safe_evaluate('() => { window.scrollBy(0, 800); return true; }')
            time.sleep(1.2)
        out['before'] = b._safe_evaluate(EDITOR_JS)
        print('2) 点回复前编辑器数：', len(out['before']['editables']))
        out['click'] = b._safe_evaluate(CLICK_REPLY_JS, idx)
        print('3) 点回复：', out['click'])
        time.sleep(3)
        out['after'] = b._safe_evaluate(EDITOR_JS)
        print('4) 点回复后：')
        for e in out['after']['editables']:
            print('     editor:', e)
        print('     按钮:', [(x['text'], x['disabled']) for x in out['after']['buttons']])
        # 只输入，不发送：输入后看「发布」是否变可用，再清空
        try:
            b.page.keyboard.type('测试回复不发送')
        except Exception as exc:
            print('   （输入失败：%s）' % str(exc)[:80])
        time.sleep(2)
        out['typed'] = b._safe_evaluate(EDITOR_JS)
        print('5) 输入后：', [(x['text'], x['disabled']) for x in out['typed']['buttons']])
        print('     编辑器内容：', [e['text'] for e in out['typed']['editables']])
        # 清空（全选删除），绝不按 Enter
        b.page.keyboard.press('Control+A')
        b.page.keyboard.press('Delete')
        time.sleep(1)
        out['cleared'] = b._safe_evaluate(EDITOR_JS)
        print('6) 清空后：', [(x['text'], x['disabled']) for x in out['cleared']['buttons']])
    finally:
        b.close()
    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'reply_editor.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
