# -*- coding: utf-8 -*-
# ============================================================
# verify_reply_dryrun.py - 评论回复链路演练验证（只填不发，零真实评论）
#
# 验四件事：
#   1) 能按评论正文定位到目标评论；
#   2) 能点开回复编辑器（Draft.js 自动聚焦）；
#   3) 能键盘输入中文并等到「发布」按钮变可用；
#   4) 演练结束会清空编辑器，评论区嵌套数不变（证明没发出去）。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/verify_reply_dryrun.py [回答URL]
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


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    out = {'url': url}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        thread = b.read_answer_comments()
        comments = [(c.get('text') or '') for c in (thread.get('comments') or [])]
        print('1) 评论区条数：', len(comments))
        for c in comments[:6]:
            print('     -', c[:50])
        target = next((c for c in comments if len(c) >= 6), '')
        if not target:
            print('!! 没有可用的候选评论，退出')
            return 1
        out['target'] = target
        print('2) 选定目标评论：', target[:40])
        before = b.comment_replied(target)
        out['replied_before'] = before
        print('3) 是否已回复（发送前）：', before)
        draft = '谢谢您看完，我下次写细一点。'
        r = b.send_reply(target, draft, dry_run=True)
        out['dry_run'] = r
        print('4) 演练发送：', json.dumps(r, ensure_ascii=False))
        after = b.comment_replied(target)
        out['replied_after'] = after
        print('5) 是否已回复（演练后）：', after, '（应仍为 False，证明没发出去）')
        state = b._safe_evaluate(
            "() => { const e = document.querySelector('div.public-DraftEditor-content');"
            " return e ? (e.innerText || '').trim() : '(没有编辑器)'; }")
        print('6) 编辑器残留内容：', repr(state))
        out['editor_leftover'] = state
    finally:
        b.close()
    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'verify_reply_dryrun.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
