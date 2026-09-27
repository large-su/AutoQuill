# -*- coding: utf-8 -*-
# 管理页有没有翻页控件？（决定回复能否从管理页发起、以及能不能翻到第 2 页）
import os
import re
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from applications.zhihu_story.browser_interact import COMMENT_MANAGE_URL
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(COMMENT_MANAGE_URL, wait_until='domcontentloaded', timeout=45000)
        time.sleep(7)
        body = b.page.inner_text('body') or ''
        print('=== 页面正文尾部 400 字 ===')
        print(body[-400:].replace(chr(10) * 2, chr(10)))
        print('=== 关键词命中 ===')
        for kw in ('下一页', '上一页', '共 ', '更多', '加载', '页'):
            idx = body.find(kw)
            print('  %-4s -> %s' % (kw, ('位置 %d：%s' % (idx, body[max(0, idx - 20):idx + 30].replace(chr(10), ' '))) if idx >= 0 else '未出现'))
        print('=== 底部链接/按钮 ===')
        items = b._safe_evaluate('''() => {
          const out = [];
          const all = document.querySelectorAll('button,a');
          for (let i = 0; i < all.length; i++) {
            const el = all[i];
            const t = (el.innerText || '').trim();
            if (el.offsetParent && t && t.length < 12) out.push(t + ' | ' + el.tagName + ' | ' + (el.getAttribute('href') || ''));
          }
          return out.slice(-25);
        }''')
        for it in items or []:
            print('   ', it)
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
