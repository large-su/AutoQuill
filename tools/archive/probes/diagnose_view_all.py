# -*- coding: utf-8 -*-
# 展开评论后，页面上有没有「查看全部/更多评论」入口？点了会不会加载更多？
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

JS = '''
() => {
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const out = [];
  const all = document.querySelectorAll('button,a,div,span');
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!el.offsetParent) continue;
    const t = String(el.innerText || '').split(ZW).join('').trim();
    if (!t || t.length > 30) continue;
    if (t.indexOf('查看') >= 0 || t.indexOf('全部') >= 0 || t.indexOf('更多') >= 0 || t.indexOf('条评论') >= 0) {
      out.push(t + ' <' + el.tagName + '>');
      if (out.length >= 20) break;
    }
  }
  return out;
}'
'''


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.zhihu.com/answer/2033320828461396569'
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        b._safe_evaluate('''() => {
          const ZW = String.fromCharCode(8203,8204,8205,65279);
          const clean = s => (s || '').split(ZW).join('').trim();
          const btns = Array.from(document.querySelectorAll('button'));
          const hit = btns.find(x => /条评论/.test(clean(x.innerText)));
          if (hit) { hit.click(); return true; }
          return false;
        }''')
        time.sleep(5)
        print('=== 展开后可见的「查看/全部/更多/条评论」控件 ===')
        for x in (b._safe_evaluate(JS) or []):
            print('   ', x)
        n1 = len((b.read_answer_comments() or {}).get('comments') or [])
        print('当前读到的评论条数：%d' % n1)
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
