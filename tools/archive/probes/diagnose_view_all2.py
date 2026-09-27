# -*- coding: utf-8 -*-
# 用 page.inner_text 找评论区的「查看全部」入口（不写自定义 JS）
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
        b.read_answer_comments()          # 生产代码：展开 + 有界加载
        time.sleep(2)
        body = b.page.inner_text('body') or ''
        for kw in ('查看全部', '全部评论', '更多评论', '展开更多', '条评论'):
            idx = body.find(kw)
            print('%-6s -> %s' % (kw, ('…%s…' % body[max(0, idx - 30):idx + 24].replace(chr(10), ' / ')) if idx >= 0 else '未出现'))
        print('--- 评论区附近文本（从「条评论」起 200 字）---')
        i = body.find('条评论')
        print(body[i:i + 200].replace(chr(10), ' / ') if i >= 0 else '(未找到)')
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
