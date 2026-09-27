# -*- coding: utf-8 -*-
# 切到「最新」排序后，能不能读到目标评论？（回答页评论很多时不切排序读不全）
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

NEEDLE = sys.argv[2] if len(sys.argv) > 2 else '你这篇文到底啥意思'


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.zhihu.com/answer/2033320828461396569'
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        b.read_answer_comments()          # 展开评论区（生产代码）
        time.sleep(2)
        # 用 Playwright 自带的文本定位点「最新」（不写自定义 JS）
        clicked = False
        for label in ('最新', '按时间'):
            try:
                loc = b.page.get_by_text(label, exact=True)
                if loc.count() > 0:
                    loc.first.click(timeout=5000)
                    clicked = True
                    print('已点排序：%s' % label)
                    break
            except Exception as exc:      # noqa: BLE001
                print('点 %s 失败：%s' % (label, str(exc)[:60]))
        time.sleep(3)
        info = b.read_answer_comments()
        cs = [c.get('text') or '' for c in (info.get('comments') or [])]
        print('排序后读到 %d 条' % len(cs))
        for c in cs[:14]:
            print('   -', c[:36])
        hit = [c for c in cs if NEEDLE in c]
        print('★ 目标评论是否读到：%s' % ('是' if hit else '否'))
        if not clicked:
            print('（没有找到「最新」排序控件）')
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
