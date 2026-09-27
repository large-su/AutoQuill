# -*- coding: utf-8 -*-
# 发送后核实：把页面重新加载，切「最新」，找我们的回复是否真的在评论区
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ANSWER = 'https://www.zhihu.com/answer/2033320828461396569'
COMMENT = '你这篇文到底啥意思'
REPLY = '没啥深意，就一狗血爽文，看个热闹。'


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        # 用「我的」标签页可能直接列出我发过的评论；先试主页面
        b.page.goto(ANSWER + '/comments', wait_until='domcontentloaded', timeout=45000)
        time.sleep(6)
        body = b.page.inner_text('body') or ''
        print('URL:', b.page.url)
        print('标题:', b.page.title())
        print('页面是否含我们的回复：', REPLY in body)
        print('页面是否含目标评论：', COMMENT in body)
        if REPLY in body:
            i = body.find(REPLY)
            print('上下文：', body[max(0, i - 120):i + 60].replace(chr(10), ' / '))
            return 0
        # 回到回答页，展开 + 切最新 + 展开嵌套回复后再找
        b.page.goto(ANSWER, wait_until='domcontentloaded', timeout=45000)
        time.sleep(7)
        b.read_answer_comments()
        time.sleep(2)
        try:
            loc = b.page.get_by_text('最新', exact=True)
            if loc.count() > 0:
                loc.first.click(timeout=5000)
                time.sleep(3)
        except Exception as exc:                # noqa: BLE001
            print('切最新失败：', str(exc)[:60])
        b.read_answer_comments()
        time.sleep(1)
        # 展开所有「展开其他 N 条回复」
        for _ in range(3):
            try:
                loc = b.page.get_by_text('展开其他', exact=False)
                n = loc.count()
                for i in range(n):
                    try:
                        loc.nth(i).click(timeout=3000)
                    except Exception:           # noqa: BLE001
                        pass
                if not n:
                    break
                time.sleep(1.5)
            except Exception:                   # noqa: BLE001
                break
        body = b.page.inner_text('body') or ''
        print('回答页含我们的回复：', REPLY in body)
        if REPLY in body:
            i = body.find(REPLY)
            print('上下文：', body[max(0, i - 140):i + 60].replace(chr(10), ' / '))
        else:
            j = body.find(COMMENT)
            print('目标评论附近：', body[max(0, j - 40):j + 200].replace(chr(10), ' / ') if j >= 0 else '(未找到目标评论)')
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
