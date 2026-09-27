# -*- coding: utf-8 -*-
# 查一条评论现在还在不在（用生产代码的采集，不写自定义 JS）
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

NEEDLE = sys.argv[1] if len(sys.argv) > 1 else '你这篇文到底啥意思'


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        got = b.collect_manage_comments(limit=20)
        cards = got.get('comments') or []
        print('评论管理页共 %d 条（卡片总数 %s）' % (len(cards), got.get('count')))
        hit = [c for c in cards if NEEDLE in (c.get('text') or '')]
        if not hit:
            print('!! 管理页里已经没有这条评论了（可能已被作者/平台删除）')
            for c in cards[:8]:
                print('   - [%s] %s' % (c.get('time_text'), (c.get('text') or '')[:30]))
            return 1
        for c in hit:
            print('找到：')
            print('   作者  :', c.get('author'))
            print('   时间  :', c.get('time_text'))
            print('   正文  :', c.get('text'))
            print('   回答  :', c.get('answer_url'))
            print('   标题  :', c.get('answer_title'))
            print('   去重键:', c.get('key'))
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
