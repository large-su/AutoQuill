# -*- coding: utf-8 -*-
# ============================================================
# core/checkin.py — 打卡挑战：当日快照 + 台账 + 决策（纯逻辑）
#
# 分层：本模块只做「状态与判断」，不碰浏览器、不碰 DOM：
#   - 页面读写 → applications/zhihu_story/browser_interact.py
#   - 调度执行 → automation/
# 这样「今天还差不差关注/赞同/评论」「该不该做取关再关注」这些判断
# 可以脱机单测（tests/test_checkin.py）。
#
# 两份数据：
#   data/state/checkin.json           当日快照（跨天自动重置）
#   data/state/comment_replies.jsonl  评论回复台账（同一条永不回第二次）
#
# 设计口径（2026-09-27 用户拍板）：
#   - 打卡是否达成以**打卡页状态**为权威（用户手动点过的也算），
#     本地台账只用于「今天我们自己做过没有」，两者取并集；
#   - 当天最后一个写草稿仍未达成 → 允许翻转（取关再关注 / 取消赞同再赞同）；
#   - 评论回复每天 N 条、一次跑完，N 由前端设置（automation 的任务配额）。
# ============================================================

import json
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)

# 我们关心的三项互动（其余两项「提问 / 收听 Morning Call」刻意不做）
TRACKED = ('follow', 'vote', 'comment')

KIND_LABELS = {'follow': '关注知友', 'vote': '送出赞同', 'comment': '发布评论'}

# 哪些项可以从本地台账追溯「其实已经做成了」（见 heal_from_ledger）。
# 只放**有台账可查、且不可重复做**的项：评论发出去了就不可能再发一次，
# 所以必须能从台账补记，不能干等打卡页。
HEAL_RULES = {
    'comment': {'flag': 'sent', 'exclude_dry': True,
                'detail': '回复读者评论 %d 条（打卡页统计有延迟）'},
}


def _state_dir():
    from core import paths
    d = paths.data('data', 'state')
    os.makedirs(d, exist_ok=True)
    return d


def state_path():
    return os.path.join(_state_dir(), 'checkin.json')


def replies_path():
    return os.path.join(_state_dir(), 'comment_replies.jsonl')


def _atomic_write(path, text):
    '''原子写：先写 .tmp 再替换（断电/中途退出不留半截文件）。'''
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return True
        except OSError as exc:
            if attempt == 2:
                log.warning('打卡状态写入失败（%s）：%s', os.path.basename(path), exc)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
            import time as _t
            _t.sleep(0.15 * (attempt + 1))
    return False


def today_key(now=None):
    return (now or datetime.now()).strftime('%Y-%m-%d')


def now_str(now=None):
    return (now or datetime.now()).strftime('%Y-%m-%dT%H:%M:%S')


def fresh_state(now=None, campaign_url=''):
    '''当天全新快照。'''
    return {
        'date': today_key(now),
        'campaign_url': campaign_url or '',
        'campaign_title': '',
        'checked_at': '',
        'tasks': {},                 # key -> {title, desc, action, done}
        'done': {k: False for k in TRACKED},    # 本地台账：今天成功做过没有
        'tried': [],                 # 今天试过但跳过/失败的目标
        'result': {},                # {ok, detail} 今日打卡结论
    }


def load_state(now=None, campaign_url=''):
    '''读当日快照；文件缺失/损坏/跨天 → 返回全新快照（永不抛错）。'''
    day = today_key(now)
    path = state_path()
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f) or {}
        except Exception as exc:            # noqa: BLE001
            log.warning('打卡状态读取失败，按新的一天处理：%s', exc)
            raw = {}
    if raw.get('date') != day:
        state = fresh_state(now, campaign_url)
        if raw.get('campaign_url') and not campaign_url:
            state['campaign_url'] = raw['campaign_url']      # 沿用上次发现的当期链接
            state['campaign_title'] = raw.get('campaign_title') or ''
        return state
    state = fresh_state(now, campaign_url)
    state.update(raw)
    state['date'] = day
    done = state.get('done') or {}
    state['done'] = {k: bool(done.get(k)) for k in TRACKED}
    state.setdefault('tasks', {})
    state.setdefault('tried', [])
    state.setdefault('result', {})
    return state


def save_state(state):
    return _atomic_write(state_path(), json.dumps(state, ensure_ascii=False, indent=2))


def set_campaign(state, url, title=''):
    '''记录当期打卡页（每期 campaign id 会变，由创作中心首页自动发现）。'''
    if url:
        state['campaign_url'] = url
    if title:
        state['campaign_title'] = title
    return state


def update_tasks(state, tasks_by_key, now=None):
    '''用打卡页解析结果刷新任务状态。

    ★ 已达成**只升不降**（2026-10-01 用户反馈的显示 bug）：
      打卡页有自己的统计延迟——刚发完评论就读，页面往往还是「去评论」。
      以前这里直接覆盖，于是「本地明明做成了」被页面的旧状态抹掉，
      界面上永远显示「发布评论✗」。现在只要页面本次读到 done=True 就置位，
      而**已置位的绝不因为一次页面读回 False 而降级**（当天的达成事实不可否认）。
    '''
    new_tasks = dict(tasks_by_key or {})
    old_map = {k: bool((v or {}).get('done'))
               for k, v in (state.get('tasks') or {}).items()}
    for key, task in new_tasks.items():
        if not isinstance(task, dict):
            continue
        if old_map.get(key) and not task.get('done'):
            task = dict(task)
            task['done'] = True
            task['stale_page'] = True      # 标记：这一项是本地记账的事实，页面还没跟上
            new_tasks[key] = task
    state['tasks'] = new_tasks
    state['checked_at'] = now_str(now)
    for key in TRACKED:
        if (state.get('done') or {}).get(key):
            task = state['tasks'].get(key)
            if isinstance(task, dict) and not task.get('done'):
                task['done'] = True
                task['stale_page'] = True
    return state


def task_done(state, kind):
    '''打卡页上这一项是不是已完成（None = 页面上没这项 → 未知）。'''
    task = (state.get('tasks') or {}).get(kind)
    if task is None:
        return None
    return bool(task.get('done'))


def needs(state, kind):
    '''今天这一项还要不要做：打卡页没完成 且 本地今天也没做成。'''
    if kind not in TRACKED:
        return False
    page = task_done(state, kind)
    if page is True:
        return False
    if (state.get('done') or {}).get(kind):
        return False
    return True


def pending_kinds(state):
    return [k for k in TRACKED if needs(state, k)]


def mark_done(state, kind, detail=''):
    '''本地记账：今天这一项成功了。'''
    if kind not in TRACKED:
        return state
    state.setdefault('done', {})[kind] = True
    if detail:
        state.setdefault('notes', []).append(
            {'at': now_str(), 'kind': kind, 'detail': detail})
    return state


def mark_tried(state, kind, author='', reason='', now=None):
    '''本地记账：今天试过这个目标但跳过了（已关注 / 已赞同 …）。'''
    state.setdefault('tried', []).append({
        'at': now_str(now), 'kind': kind, 'author': author or '', 'reason': reason or ''})
    return state


def tried_authors(state, kind=''):
    '''今天试过的作者集合（换候选时避免反复看同一批）。'''
    out = set()
    for row in state.get('tried') or []:
        if kind and row.get('kind') != kind:
            continue
        if row.get('author'):
            out.add(row['author'])
    return out


def decide(state, kind, is_last=False, enabled=True):
    '''今天对某一项该做什么：

      done    已经达成（打卡页已完成，或本地今天做成了）→ 什么都不做
      skip    这项不做（没启用 / 页面上没有这项）
      do      直接做（关注 / 赞同）
      toggle  翻转（取关再关注 / 取消赞同再赞同）——只在当天最后一个写草稿时
    '''
    if not enabled or kind not in TRACKED:
        return 'skip'
    if not needs(state, kind):
        return 'done'
    return 'toggle' if is_last else 'do'


def target_action(kind, item, is_last=False):
    '''给定页面上某个回答条目，决定在它身上做什么：

      do      直接关注 / 赞同
      toggle  取关再关注 / 取消赞同再赞同（仅最后一个草稿）
      skip    换下一个候选（已关注且不是最后一班 / 按钮不可用 / 状态读不出来）
    '''
    from applications.zhihu_story.browser_interact import (
        follow_state, vote_state)
    it = item or {}
    if kind == 'follow':
        if not it.get('has_follow'):
            return 'skip'
        st = follow_state(it.get('follow_text'))
        if st == 'none':
            return 'do'
        if st == 'followed' and is_last:
            return 'toggle'
        return 'skip'
    if kind == 'vote':
        if not it.get('has_vote') or it.get('vote_disabled'):
            return 'skip'
        st = vote_state(it.get('vote_text'))
        if st == 'none':
            return 'do'
        if st == 'voted' and is_last:
            return 'toggle'
        return 'skip'
    return 'skip'


def summary(state, enabled_kinds=None):
    '''给 UI / 通知用的一句话摘要。'''
    kinds = list(enabled_kinds or TRACKED)
    rows = []
    pending = 0
    unknown = 0
    for k in kinds:
        page = task_done(state, k)
        local = bool((state.get('done') or {}).get(k))
        if page is True or local:
            rows.append('%s✓' % KIND_LABELS.get(k, k))
        elif page is None:
            rows.append('%s?' % KIND_LABELS.get(k, k))
            unknown += 1
        else:
            rows.append('%s✗' % KIND_LABELS.get(k, k))
            pending += 1
    ok = pending == 0 and unknown == 0
    return {'ok': ok, 'pending': pending, 'unknown': unknown,
            'line': '打卡：' + ' / '.join(rows), 'campaign': state.get('campaign_title') or ''}


def set_result(state, ok, detail=''):
    state['result'] = {'ok': bool(ok), 'detail': detail or '', 'at': now_str()}
    return state


def heal_from_ledger(state, now=None):
    '''用**本地台账**把「已经做成但没记账」的项补上（返回补了哪几项）。

    ★ 为什么需要（2026-10-01 用户反馈）：评论发出去了、打卡页却还没跟上，
      而当时的代码没有本地记账，于是「发布评论」一直显示 ✗。
      已经发出去的评论不可能再发一次，所以这里按台账**追溯**补记：
      今天只要有一条真的发出去的评论（sent=True 且非演练），这一项就是达成的。

    只读台账、只补不删；台账里没有就什么都不做。
    '''
    healed = []
    try:
        rows = replies_today(now=now)
    except Exception:                       # noqa: BLE001 台账读不到不该影响主流程
        return healed
    for kind, rule in HEAL_RULES.items():
        if (state.get('done') or {}).get(kind):
            continue
        hits = [r for r in rows
                if r.get(rule['flag'])
                and not (rule['exclude_dry'] and r.get('dry_run'))]
        if not hits:
            continue
        mark_done(state, kind,
                  detail='台账追溯：%s' % (rule['detail'] % len(hits)))
        healed.append(kind)
    return healed


# ------------------------------------------------------------
# 评论回复台账（同一条评论永不回第二次）
# ------------------------------------------------------------
def load_replies(limit=0):
    path = replies_path()
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:           # noqa: BLE001
                    continue
    except Exception as exc:                # noqa: BLE001
        log.warning('评论回复台账读取失败：%s', exc)
        return []
    return rows[-limit:] if limit and limit > 0 else rows


def replied_keys():
    '''已经**真的回出去**的评论（演练草稿不算——那条我们从没发过）。'''
    out = set()
    for r in load_replies():
        if not r.get('key'):
            continue
        # attempted：真的点过发送（哪怕没确认到落地）——也算消耗掉，
        # 绝不因为「没确认」就换个机会再发一次（那是重复打扰读者）
        if r.get('sent') or r.get('attempted') or r.get('failed'):
            out.add(r['key'])
    return out


def dryrun_keys():
    '''演练阶段「生成过但没发」的评论：演练期间跳过它们换样本，

    切到自动发送后这些评论**重新变成可回复**（用户 2026-09-27：先演练两天，
    那两天挑过的评论不该因为演练而被永久占用）。
    '''
    out = set()
    for r in load_replies():
        if not r.get('key'):
            continue
        if r.get('dry_run') and not r.get('sent') and not r.get('failed'):
            out.add(r['key'])
    return out


def is_replied(key):
    return bool(key) and key in replied_keys()


def append_reply(record):
    '''记一条回复（草稿/已发都记，sent 字段区分）。'''
    row = dict(record or {})
    row.setdefault('at', now_str())
    try:
        with open(replies_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + chr(10))
        return True
    except Exception as exc:                # noqa: BLE001
        log.warning('评论回复台账写入失败（不影响发送）：%s', exc)
        return False


def replies_today(now=None):
    day = today_key(now)
    return [r for r in load_replies() if str(r.get('at') or '').startswith(day)]

# ------------------------------------------------------------
# 评论回复「每次运行」的概览（UI 的「今天抓了什么」看这里）
# ------------------------------------------------------------
def reply_runs_path():
    return os.path.join(_state_dir(), 'reply_runs.jsonl')


def append_reply_run(record):
    '''记一次评论回复运行的概览：抓了多少条 / 过滤掉什么 / 回了几个。'''
    row = dict(record or {})
    row.setdefault('at', now_str())
    try:
        with open(reply_runs_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + chr(10))
        return True
    except Exception as exc:                # noqa: BLE001
        log.warning('回复运行统计写入失败（不影响回复）：%s', exc)
        return False


def load_reply_runs(day='', limit=0):
    '''读回复运行统计；给了 day 只返回那一天的。'''
    path = reply_runs_path()
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:           # noqa: BLE001
                    continue
                if day and not str(row.get('at', '')).startswith(day):
                    continue
                rows.append(row)
    except Exception as exc:                # noqa: BLE001
        log.warning('回复运行统计读取失败：%s', exc)
        return []
    return rows[-limit:] if limit and limit > 0 else rows


# ------------------------------------------------------------
# 写草稿顺带互动的上下文（进程内单例，不做跨进程传递）
# ------------------------------------------------------------
# **只有自动化调度器派出的 full_chain 作业会写这个上下文**
# （automation/executor._full_chain）；用户在控制台手点的经典/纯净/批量
# 链路走 run_manager 自己的入口，从不写上下文 → 钩子第一行就返回，
# 连浏览器都不会碰（用户 2026-09-27 明确要求：手动写作绝不被干扰）。
#
# 三道保险，防止上下文「泄漏」到手动作业上：
#   1) 执行器用 try/finally 包住整个作业，无论成功失败都清理；
#   2) 上下文带时间戳，超过 TTL（默认 2 小时）自动视为失效；
#   3) 每次读取都做时间校验，不依赖调用方记得清理。
_context = None
_context_at = None
CONTEXT_TTL_MINUTES = 120


def set_context(ctx, now=None):
    '''设置当前写草稿作业的打卡上下文（空值 = 不做互动）。'''
    global _context, _context_at
    if ctx:
        _context = dict(ctx)
        _context_at = now or datetime.now()
    else:
        _context = None
        _context_at = None
    return dict(_context) if _context else None


def get_context(now=None):
    '''读当前上下文；缺失或已过期返回 {}（过期即视为没有，绝不冒险动手）。'''
    global _context, _context_at
    if not _context:
        return {}
    ts = _context_at or datetime.now()
    age = ((now or datetime.now()) - ts).total_seconds() / 60.0
    if age > CONTEXT_TTL_MINUTES:
        log.info('打卡上下文已过期（%.0f 分钟），忽略', age)
        _context = None
        _context_at = None
        return {}
    return dict(_context)


def context_age_minutes(now=None):
    '''当前上下文的年龄（分钟）；没有上下文返回 None。'''
    if not _context or not _context_at:
        return None
    return ((now or datetime.now()) - _context_at).total_seconds() / 60.0


def clear_context():
    global _context, _context_at
    _context = None
    _context_at = None

