# -*- coding: utf-8 -*-
# 打印当日排班与计划（排查「时间轴上这一条是什么」用）
import json
import os
import sys
from datetime import date

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

base = os.path.join(os.environ.get('APPDATA', ''), 'AutoQuill', 'data', 'state', 'automation')
plan_path = os.path.join(base, 'plan.json')
day_path = os.path.join(base, 'day_%s.json' % date.today().isoformat())

print('plan:', plan_path)
if os.path.exists(plan_path):
    plan = json.load(open(plan_path, encoding='utf-8'))
    print('  enabled=%s window=%s' % (plan.get('enabled'), plan.get('window')))
    for t, cfg in (plan.get('tasks') or {}).items():
        print('   %-14s enabled=%-5s cap=%-3s window=%s params=%s'
              % (t, cfg.get('enabled'), cfg.get('daily_cap'), cfg.get('window'), cfg.get('params')))

print('day:', day_path)
if os.path.exists(day_path):
    day = json.load(open(day_path, encoding='utf-8'))
    for job in day.get('schedule') or []:
        print('   %-19s %-14s %-9s units=%-2s %s'
              % (job.get('planned_at'), job.get('type'), job.get('status'),
                 job.get('units'), (job.get('note') or '')[:44]))
else:
    print('   （今天还没有排班文件）')
