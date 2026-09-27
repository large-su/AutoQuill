# -*- coding: utf-8 -*-
# ============================================================
# shot_single_axis.py - 「单次任务轴 + 详情卡」视觉检查（独立数据目录，不碰真实数据）
#
# 造一份带打卡互动 / 回复评论的当日排班与状态 → 起临时服务 → 截图。
# 运行：.venv/Scripts/python tools/archive/probes/shot_single_axis.py
# 输出：data/cleanup/auto_single_axis.png
# ============================================================

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8797)
    ap.add_argument('--width', type=int, default=1400)
    ap.add_argument('--height', type=int, default=900)
    args = ap.parse_args()
    data_dir = tempfile.mkdtemp(prefix='aq_single_axis_')
    os.makedirs(os.path.join(data_dir, 'config'), exist_ok=True)
    for name in ('llm_providers.json', 'webui_model.json'):
        src = ROOT / 'config' / name
        if src.exists():
            with open(src, 'rb') as f:
                blob = f.read()
            with open(os.path.join(data_dir, 'config', name), 'wb') as f:
                f.write(blob)
    env = dict(os.environ, AQ_DATA_DIR=data_dir, PYTHONIOENCODING='utf-8')
    os.environ['AQ_DATA_DIR'] = data_dir

    from datetime import datetime
    from automation import store, planner
    from automation.model import normalize_plan
    from core import checkin

    plan = normalize_plan({
        'enabled': True,
        'window': {'start': '08:00', 'end': '23:30'},
        'tasks': {'full_chain': {'enabled': True, 'daily_cap': 3},
                  'publish_drafts': {'enabled': True, 'daily_cap': 3},
                  'checkin': {'enabled': True, 'daily_cap': 1},
                  'reply_comment': {'enabled': True, 'daily_cap': 3}}},
    )
    store.save_plan(plan)
    now = datetime.now().replace(hour=17, minute=5, second=0, microsecond=0)
    day = now.strftime('%Y-%m-%d')
    data = planner.materialize_day(now, plan, {}, {})
    for j in data['schedule']:
        if j['type'] == 'reply_comment':
            j['status'] = 'done'
            j['dry_run'] = True
            j['units'] = 2
            j['note'] = '演练 2 条：演练：被抢走的提成得上百万→想过，提成都是口头画的饼…'
        elif j['type'] == 'checkin':
            j['status'] = 'planned'
            j['note'] = '一次跑完（最多 1 项）'
    store.save_day(day, data)

    state = checkin.load_state()
    checkin.set_campaign(state, 'https://www.zhihu.com/parker/campaign/2083977679355889635',
                         '创作打卡挑战赛第五十三期')
    checkin.update_tasks(state, {
        'answer': {'title': '发布 1 篇回答', 'desc': '回答需至少 100 字以上', 'action': '已完成', 'done': True},
        'follow': {'title': '关注 1 位知友', 'desc': '关注 1 位志同道合的知友', 'action': '已完成', 'done': True},
        'vote': {'title': '送出 1 个赞同', 'desc': '打卡当天送出 1 个赞同', 'action': '已完成', 'done': True},
        'comment': {'title': '发布 1 条评论', 'desc': '发布 1 条 10 字以上有效评论', 'action': '去评论', 'done': False},
    })
    checkin.mark_done(state, 'follow', detail='关注 Seasee Youl')
    checkin.mark_done(state, 'vote', detail='赞同 Seasee Youl')
    checkin.mark_tried(state, 'follow', author='绯意所思', reason='已关注，换下一个')
    checkin.save_state(state)

    checkin.append_reply({'key': 'k1', 'author': '伊蜻蜓',
                          'comment': '被抢走的提成得上百万了吧，不走劳动仲裁？[为难]',
                          'reply': '想过，提成都是口头画的饼，聊天记录也删了，仲裁没证据。',
                          'question': '追妻火葬场的女主不回头了怎么办？',
                          'dry_run': True, 'sent': False})
    checkin.append_reply({'key': 'k2', 'author': '蹲',
                          'comment': '[蹲][哇]', 'reply': '谢谢蹲，我尽快更下一章。',
                          'question': '有什么好看双男主文？', 'dry_run': True, 'sent': False})
    store.append_reply_run({'collected': 20, 'candidates': 10,
                            'dropped': {'hostile': 3, 'no-content': 4, 'replied': 2, 'spam': 1},
                            'picked': 2, 'units': 2, 'dry_run': True,
                            'detail': '演练 2 条'})

    boot = tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8')
    root_esc = str(ROOT).replace(chr(92), chr(92) * 2)
    boot.write("import sys" + chr(10))
    boot.write("sys.path.insert(0, r'%s')" % root_esc + chr(10))
    boot.write('import webui.server as srv' + chr(10))
    boot.write("srv._ALLOWED_HOSTS.update({'127.0.0.1:%d','localhost:%d'})" % (args.port, args.port) + chr(10))
    boot.write("srv._ALLOWED_ORIGINS.update({'http://127.0.0.1:%d','http://localhost:%d'})" % (args.port, args.port) + chr(10))
    boot.write("srv.run(host='127.0.0.1', port=%d)" % args.port + chr(10))
    boot.close()
    log = tempfile.NamedTemporaryFile('wb', suffix='.log', delete=False)
    proc = subprocess.Popen([sys.executable, boot.name], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        import urllib.request
        for _ in range(60):
            try:
                if urllib.request.urlopen('http://127.0.0.1:%d/' % args.port, timeout=1).status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        from playwright.sync_api import sync_playwright
        outdir = ROOT / 'data' / 'cleanup'
        outdir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            pg = browser.new_page(viewport={'width': args.width, 'height': args.height},
                                  device_scale_factor=2)
            errors = []
            pg.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            pg.goto('http://127.0.0.1:%d/' % args.port, wait_until='networkidle', timeout=30000)
            pg.evaluate("() => { const m = document.getElementById('setupMask'); if (m) m.classList.remove('show'); }")
            pg.select_option('#leftModeSel', 'automation')
            pg.wait_for_timeout(2500)
            marks = pg.evaluate("() => document.querySelectorAll('#autoSingleAxis .sa-pill').length")
            cards = pg.evaluate("() => document.querySelectorAll('#autoSingleCards .sa-card').length")
            lanes = pg.evaluate("() => document.querySelectorAll('#autoTimeline .tl-lane').length")
            texts = pg.evaluate("() => Array.from(document.querySelectorAll('#autoSingleAxis .tl-lane-label')).map(e => e.textContent)")
            dbg = pg.evaluate("() => JSON.stringify({keys: Object.keys(autoData || {}), single: (autoData || {}).single || null, sched: ((autoData || {}).schedule || []).map(j => j.type + ':' + j.status)})")
            print('DEBUG:', dbg[:600])
            png = outdir / 'auto_single_axis.png'
            pg.screenshot(path=str(png), full_page=False)
            print('POINTS: marks=%d cards=%d main_lanes=%d labels=%s'
                  % (marks, cards, lanes, texts))
            print('console_errors=%d' % len(errors))
            for e in errors[:5]:
                print('  console error:', e[:160])
            print('screenshot:', png)
            browser.close()
    finally:
        proc.terminate()
    return 0


if __name__ == '__main__':
    sys.exit(main())
