# -*- coding: utf-8 -*-
# ============================================================
# verify_reply_send.py — 真实发送一条评论回复（一次性验证「发送路径」）
#
# 背景：回复链路的采集/筛选/生成/校验都在真机验证过，唯独「点发布 + 落地确认」
# 没跑过（演练模式刻意不点）。本脚本用用户过目过的那一条回复补上这一环。
#
# 用法（默认真发；--dry 只演练）：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="%APPDATA%/AutoQuill" \
#       .venv/Scripts/python tools/archive/probes/verify_reply_send.py [--dry]
# 产出：控制台全过程 + data/cleanup/verify_reply_send.json
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

COMMENT = '你这篇文到底啥意思'
REPLY = '没啥深意，就一狗血爽文，看个热闹。'
ANSWER_URL = 'https://www.zhihu.com/answer/2033320828461396569'
KEY = 'ae5d9c2b4a07e4bf'          # 与演练记录同一个去重键
AUTHOR = '三人合'


def main():
    from applications.zhihu_story import browser_adapter  # noqa: F401  注册浏览器工厂
    from core import checkin
    from web_drivers import reset_driver
    from web_drivers.browser_pool import close_shared_browser, get_browser

    dry = '--dry' in sys.argv
    out = {'dry': dry, 'comment': COMMENT, 'reply': REPLY, 'answer': ANSWER_URL}
    b = get_browser()
    try:
        print('登录态：%s' % b.is_logged_in())
        print('1) 打开回答页并展开评论区…')
        b.page.goto(ANSWER_URL, wait_until='domcontentloaded', timeout=45000)
        time.sleep(8)
        thread = b.read_answer_comments() or {}
        # 评论区默认按「热度」只渲染前 N 条（这条回答有 76 条评论）；
        # 切到「最新」才可能读到较近的评论（真机踩过：不切排序找不到目标）
        for label in ('最新',):
            try:
                loc = b.page.get_by_text(label, exact=True)
                if loc.count() > 0:
                    loc.first.click(timeout=5000)
                    print('   已切换评论排序：%s' % label)
                    time.sleep(3)
                    thread = b.read_answer_comments() or {}
                    break
            except Exception as exc:            # noqa: BLE001
                print('   切排序失败：%s' % str(exc)[:60])
        comments = [(c.get('text') or '') for c in (thread.get('comments') or [])]
        out['comments'] = comments
        print('   评论区读到 %d 条：' % len(comments))
        for c in comments[:8]:
            print('     -', c[:44])
        hit = [c for c in comments if COMMENT in c]
        if not hit:
            print('!! 目标评论不在评论区（可能被删或未加载）——已终止，未发送')
            out['error'] = 'comment-not-found'
            return 1
        our = [r.get('reply') for r in checkin.load_replies() if r.get('reply')]
        before = b.comment_replied(COMMENT, our_texts=our)
        out['replied_before'] = before
        print('2) 发送前「是否已回复」：%s' % before)
        if before:
            print('!! 这条评论下已有我们的回复——已终止，绝不重复打扰')
            out['error'] = 'already-replied'
            return 1
        print('3) %s：%s' % ('演练（不点发布）' if dry else '发送', REPLY))
        r = b.send_reply(COMMENT, REPLY, dry_run=dry)
        out['send'] = r
        print('4) 结果：%s' % json.dumps(r, ensure_ascii=False))
        checkin.append_reply({
            'key': KEY, 'author': AUTHOR, 'comment': COMMENT, 'reply': REPLY,
            'answer_url': ANSWER_URL, 'sent': bool(r.get('sent')),
            'attempted': not dry, 'source': 'manual-verify',
            'detail': r.get('detail') or '',
        })
        print('5) 已写台账（同一条评论不会再被回复）')
        if not dry:
            time.sleep(3)
            after = b.comment_replied(COMMENT, our_texts=[REPLY])
            out['replied_after'] = after
            print('6) 发送后「是否已回复」：%s' % after)
        try:
            st = checkin.load_state()
            found = b.discover_campaign_url()
            if found.get('ok'):
                info = b.read_checkin_tasks(found['url'])
                if info.get('tasks'):
                    checkin.update_tasks(st, info['tasks'])
                    if info.get('title'):
                        st['campaign_title'] = info['title']
                    checkin.save_state(st)
                    line = checkin.summary(st)['line']
                    out['checkin_line'] = line
                    print('7) 打卡状态：%s' % line)
        except Exception as exc:            # noqa: BLE001
            print('   （打卡状态刷新失败，不影响发送：%s）' % exc)
    finally:
        try:
            reset_driver(delete_session=True)
        except Exception:                   # noqa: BLE001
            pass
        try:
            close_shared_browser()
        except Exception:                   # noqa: BLE001
            pass
    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'verify_reply_send.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
