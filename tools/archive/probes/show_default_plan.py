# -*- coding: utf-8 -*-
# 新装默认计划长什么样？四类任务是否都排上班、时间是否合理
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, '.')
tmp = tempfile.mkdtemp(prefix='aq_defaults_')
os.environ['AQ_DATA_DIR'] = tmp

from automation.model import normalize_plan  # noqa: E402
from automation import planner  # noqa: E402

plan = normalize_plan({})                      # 全新计划（模拟首次安装）
print('默认开关：')
for t, cfg in sorted(plan['tasks'].items()):
    print('   %-14s enabled=%-5s 每日=%-3s 时段=%s'
          % (t, cfg['enabled'], cfg['daily_cap'], cfg.get('window') or '全局'))

now = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
day = planner.materialize_day(now, plan, {}, {})
print(
'今天排班：')
for j in day['schedule']:
    meta = planner.TASK_TYPES[j['type']]
    print('   %s  %-14s %-8s %s' % ((j.get('planned_at') or '—')[11:16], j['type'],
                                    j['status'], (j.get('note') or '')[:34]))
