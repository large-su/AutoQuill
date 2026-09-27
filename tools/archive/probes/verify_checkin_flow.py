# -*- coding: utf-8 -*-
# ============================================================
# verify_checkin_flow.py - 打卡链路真机串联验证（只读，不动账号）
#
# 验四件事：
#   1) 从创作中心首页能不能自动发现当期打卡页；
#   2) 打卡页今日任务解析 + 状态摘要；
#   3) 回答页能不能列出「作者 + 关注/赞同按钮状态」并挑出目标；
#   4) run_checkin_job / run_interaction 全链路跑通但不动作
#      （今天打卡页显示关注/赞同已完成 → 决策为 done → 不发任何请求）。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/verify_checkin_flow.py
# ============================================================

import json
import os
import sys

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from applications.zhihu_story import checkin_task
    from core import checkin

    out = {}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        print('登录态：%s' % b.is_logged_in())

        found = b.discover_campaign_url()
        print('1) 当期入口：', found.get('ok'), found.get('url'), '|', found.get('text'))
        out['campaign'] = found

        if found.get('ok'):
            info = b.read_checkin_tasks(found['url'])
            out['tasks'] = info
            print('2) 打卡页标题：', info.get('title'))
            for key, t in (info.get('tasks') or {}).items():
                print('     %-13s %-14s %s' % (key, t.get('action'), t.get('title')))
            state = checkin.load_state()
            checkin.update_tasks(state, info.get('tasks') or {})
            print('   摘要：', checkin.summary(state)['line'])
            checkin.save_state(state)

        b.open_recommend_page()
        qs = b.get_recommend_questions(max_cards=3) or []
        print('3) 推荐问题候选：', [q.get('title', '')[:24] for q in qs][:3])
        if qs:
            b.open_question(qs[0]['href'])
            targets = b.list_interact_targets() or {}
            out['targets'] = targets
            for it in targets.get('items') or []:
                print('     条目#%s 作者=%s 关注=%s 赞同=%s disabled=%s' % (
                    it.get('index'), it.get('author'), it.get('follow_text'),
                    it.get('vote_text'), it.get('vote_disabled')))
            tgt = checkin_task.pick_target(targets.get('items') or [])
            print('     选中目标：', (tgt or {}).get('author'), 'index=', (tgt or {}).get('index'))

        print('4) run_checkin_job 全链路（今天应无动作）：')
        r = checkin_task.run_checkin_job(b, url=found.get('url') or '',
                                         progress=lambda t: print('     ·', t))
        out['job'] = r
        print('     →', json.dumps(r, ensure_ascii=False)[:300])
    finally:
        b.close()

    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'verify_checkin_flow.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
