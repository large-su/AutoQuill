# -*- coding: utf-8 -*-
# 这条回答到底有多少条评论？页面渲染了几条？（判断目标评论是否真的属于它）
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
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.zhihu.com/answer/2033320828461396569'
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        body = b.page.inner_text('body') or ''
        m = re.findall(r'(\d+)\s*条评论', body)
        print('页面上的「N 条评论」：', m)
        print('标题：', b.page.title())
        info = b.read_answer_comments()
        cs = [c.get('text') or '' for c in (info.get('comments') or [])]
        print('读完评论区共 %d 条：' % len(cs))
        for c in cs:
            print('   -', c[:38])
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
