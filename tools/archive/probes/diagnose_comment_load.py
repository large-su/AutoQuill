# -*- coding: utf-8 -*-
# 诊断：回答页评论区到底加载了多少、有没有「查看更多」控件
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

URL = sys.argv[1] if len(sys.argv) > 1 else 'https://www.zhihu.com/answer/2033320828461396569'

JS = '''
() => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const zwRe = new RegExp('[' + ZW + ']', 'g');
  const clean = s => (s || '').replace(zwRe, ' ').replace(/[ ]+/g, ' ').trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || '');
  const btns = Array.from(document.querySelectorAll('button,a,div,span'))
      .filter(el => el.offsetParent)
      .map(el => ({ tag: el.tagName, text: txt(el).slice(0, 24), cls: (typeof el.className === 'string' ? el.className : '').slice(0, 40) }))
      .filter(x => /条评论|更多|全部|展开|收起/.test(x.text));
  const seen = new Set();
  const uniq = btns.filter(b => { const k = b.tag + b.text; if (seen.has(k)) return false; seen.add(k); return true; });
  const contents = Array.from(document.querySelectorAll('.CommentContent')).map(x => txt(x).slice(0, 40));
  return { url: location.href, title: document.title,
           comment_contents: contents, count: contents.length,
           controls: uniq.slice(0, 15) };
}'
'''


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(URL, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        print('展开前：', json.dumps(b._safe_evaluate(JS), ensure_ascii=False)[:300])
        info = b.read_answer_comments()
        print('read_answer_comments 读到 %s 条' % len(info.get('comments') or []))
        out = b._safe_evaluate(JS) or {}
        print('展开后条数：%s' % out.get('count'))
        print('评论内容：')
        for c in out.get('comment_contents') or []:
            print('   -', c)
        print('控件：')
        for c in out.get('controls') or []:
            print('   ', c)
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
