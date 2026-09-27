# -*- coding: utf-8 -*-
# 管理页能加载到多少条评论？目标评论在第几屏？（决定回复该从哪个页面发起）
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

NEEDLE = sys.argv[1] if len(sys.argv) > 1 else '你这篇文到底啥意思'

COUNT_JS = "() => document.querySelectorAll('.CommentManage-CommentCard').length"
SCROLL_JS = "() => { window.scrollTo(0, document.body.scrollHeight); return true; }"


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from applications.zhihu_story.browser_interact import COMMENT_MANAGE_URL
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(COMMENT_MANAGE_URL, wait_until='domcontentloaded', timeout=45000)
        time.sleep(7)
        for i in range(12):
            b._safe_evaluate(SCROLL_JS)
            time.sleep(1.6)
            n = b._safe_evaluate(COUNT_JS)
            print('  第 %2d 轮滚动后卡片数：%s' % (i + 1, n))
        got = b.collect_manage_comments(limit=200)
        cards = got.get('comments') or []
        print('最终采集 %d 条' % len(cards))
        hit = [c for c in cards if NEEDLE in (c.get('text') or '')]
        if hit:
            for c in hit:
                print('★ 找到目标：', c.get('author'), '|', c.get('time_text'), '|', c.get('text')[:30])
                print('   回答：', c.get('answer_url'))
        else:
            print('!! 前 %d 条里仍没有这条评论' % len(cards))
            print('   最老一条的时间：', (cards[-1].get('time_text') if cards else '-'))
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
