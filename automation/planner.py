# -*- coding: utf-8 -*-
"""把计划展开成「今天的时间轴」。

用户口径对应到实现：
  - 配额可自由设置 → 每天按 daily_cap 生成作业；当天改配置会重排未执行部分；
  - **时间在时段内「铺开」**（2026-09-19 按用户口径重做）：把时段等分成 N 份，每份里
    随机取一点，再统一修复到「两两间隔 ≥ min_gap」；不是「间隔 1 小时随机一下、再累加
    随机一下」的随机游走——那样作业会挤在一天前段、后半天全空；
  - **数量上限是算出来的**：N 个作业之间只有 N-1 个间隔，所以时段 W 分钟内最多
    `floor(W / min_gap) + 1` 个（如 08:00–23:30 = 930 分钟、间隔 60 分钟 → 上限 16）；
    设多了不会硬排，而是按上限排 + 在时间轴上留一条「排不下」的说明；
  - 「当日完成即可」→ 不固定时间点，只在运行时段内铺开；
  - 随时可停、再开始时先看今天已做多少 → 排班按「已完成数量」扣减后生成（幂等键去重）；
  - 错过怎么办 → catch_up：none / same_day（默认，顺延到当天剩余时间）/ next_window。

★ 排班必须可复现：随机数种子 = (日期, 计划指纹, 任务类型)，所以重启后同一份配置
  得到同一条时间轴（用户看到的时间轴不会每次刷新都变），同时也让单测可断言。
"""

import hashlib
import json
import random
from datetime import datetime, timedelta

from automation.model import (
    DECOLLISION_MINUTES, TASK_PRIORITY, TASK_TYPES, job_key, parse_hhmm,
)

STATUS_PLANNED = "planned"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NEEDS_HUMAN = "needs_human"

# 「错过」的宽限期（分钟）：刚过点的作业属于正常到期，直接执行；
# 只有超过这个时长还没执行（说明当时程序没在跑）才按 catch_up 策略处理。
CATCH_UP_GRACE_MINUTES = 10

# 视为「已消耗配额」的状态（失败不算消耗，会重试/顺延）
_CONSUMED = (STATUS_DONE, STATUS_RUNNING)


def plan_fingerprint(plan) -> str:
    """影响排班的字段指纹（变了就重排未执行部分）。"""
    key = {
        # 排班算法版本：算法改了要 +1，否则「今天已经排过」的旧时间轴不会被重排
        # （v2 = 2026-09-19 改成「时段内铺开」+ 上限公式；
        #   v3 = 2026-09-19 修「中途改计划从早上重铺 / 未来作业被凭空顺延」，
        #        并让新排班只落在「现在之后」）
        "sched": 3,
        "window": plan.get("window"),
        "min_gap": plan.get("min_gap_minutes"),
        "gap_jitter": plan.get("gap_jitter_ratio"),
        "jitter": plan.get("jitter_minutes"),
        "catch_up": plan.get("catch_up"),
        "tasks": {t: {k: v for k, v in (cfg or {}).items() if k != "params"}
                  for t, cfg in (plan.get("tasks") or {}).items()},
        "params": {t: (cfg or {}).get("params")
                   for t, cfg in (plan.get("tasks") or {}).items()},
    }
    return hashlib.sha1(
        json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def _window_bounds(day, plan, cfg):
    """返回 (窗口起点 datetime, 窗口终点 datetime)。"""
    win = cfg.get("window") if isinstance(cfg.get("window"), dict) else None
    win = win or plan.get("window") or {}
    start_t = parse_hhmm(win.get("start"), parse_hhmm("08:00"))
    end_t = parse_hhmm(win.get("end"), parse_hhmm("23:30"))
    base = datetime.strptime(day, "%Y-%m-%d")
    return (base.replace(hour=start_t.hour, minute=start_t.minute),
            base.replace(hour=end_t.hour, minute=end_t.minute))


def feasible_count(window_minutes, min_gap_minutes):
    """时段内最多能排几个作业（用户问的「数学关系」）。

    N 个作业之间有 N-1 个间隔，每个 ≥ G，且总跨度不能超过时段 W：
        (N - 1) * G ≤ W   →   N ≤ floor(W / G) + 1
    例：08:00–23:30 = 930 分钟，G = 60 → floor(930/60) + 1 = 16 个。
    G ≤ 0（不限制间隔）时退化为「按分钟铺」——上限取时段分钟数。
    """
    try:
        w = float(window_minutes)
        g = float(min_gap_minutes)
    except (TypeError, ValueError):
        return 1
    if w <= 0:
        return 1
    if g <= 0:
        return max(1, int(w))
    return max(1, int(w // g) + 1)


def spread_times(win_start, win_end, count, min_gap_minutes, rng,
                 jitter_minutes=0.0, spread=0.6):
    """把 count 个时间点**铺满** [win_start, win_end]，并保证两两间隔 ≥ G。

    做法（分层随机 + 修复）：
      1) 时段等分成 count 份，第 i 个点在它那一份里随机取（spread 控制随机幅度：
         0 = 完全均匀、1 = 份内随便放）→ 天然「相对随机地分布在整段时间里」；
      2) 再叠加 jitter_minutes 的抖动（± 一半）；
      3) 修复：正向推一遍保证间隔 ≥ G，若越过终点则回推一遍；可行时结果一定落在时段内。

    可行条件：(count - 1) * G ≤ W（调用方用 feasible_count 保证）。
    返回按时间升序的 datetime 列表；不可行时尽量铺开（不抛异常，排班不能崩）。
    """
    count = int(count)
    if count <= 0:
        return []
    span = (win_end - win_start).total_seconds() / 60.0
    if span <= 0:
        return [win_start] * count
    gap = max(0.0, float(min_gap_minutes or 0))
    jitter = max(0.0, float(jitter_minutes or 0))
    spread = min(max(float(spread or 0.0), 0.0), 1.0)

    slot = span / count
    offsets = []
    for i in range(count):
        center = slot * (i + 0.5)
        raw = center + rng.uniform(-0.5, 0.5) * spread * slot
        lo, hi = slot * i, slot * (i + 1)
        offsets.append(min(max(raw, lo), hi))
    if jitter > 0:
        offsets = [min(max(o + rng.uniform(-jitter / 2.0, jitter / 2.0), 0.0),
                        span) for o in offsets]
    offsets.sort()

    # 修复 1：正向推，保证相邻间隔 ≥ G
    for i in range(1, count):
        if offsets[i] - offsets[i - 1] < gap:
            offsets[i] = offsets[i - 1] + gap
    # 修复 2：越过终点 → 从末尾回推（保证最后一个不超时段）
    if offsets[-1] > span:
        offsets[-1] = span
        for i in range(count - 2, -1, -1):
            offsets[i] = min(offsets[i], offsets[i + 1] - gap)
    # 修复 3：回推可能把第一个推到时段之前 → 再正向收一次
    if offsets[0] < 0:
        offsets[0] = 0.0
        for i in range(1, count):
            offsets[i] = max(offsets[i], offsets[i - 1] + gap)
    return [win_start + timedelta(minutes=round(o, 3)) for o in offsets]


def window_bounds(day, plan, cfg=None):
    """当天运行时段的 (起点, 终点)——公开版本：调度器要判断「时段是不是已经过了」。"""
    return _window_bounds(day, plan, cfg or {})


def _done_jobs(schedule, task_type):
    """已消耗配额的作业（done/running）——用于「先看今天已经做了多少」。"""
    return [j for j in schedule
            if j.get("type") == task_type and j.get("status") in _CONSUMED]

def _build_schedule(day, plan, schedule=None, counters=None, not_before=None):
    """生成当天作业列表（含去碰撞）。

    「今天已经做了多少」有两个来源，取较大者（避免重复计）：
      a) schedule 里 status=done/running 的作业（本进程排班内）；
      b) counters（当天台账统计，跨重启/排班丢失后的依据——用户明确要求）。

    not_before：不要把作业排在这个时间之前（中途改计划/晚上才启动时传「现在」）。
    否则晚上 22:40 改一次配额，当天时间轴会从早上 05:00 重新铺一遍，
    生成十几条「已经过去的点」，界面上一片「已跳过」——用户看到的全是噪音
    （线上踩过：6 发布 + 8 撰写全部在生成那一刻就过期）。
    """
    schedule = schedule or []
    counters = counters or {}
    jobs = []
    types = sorted(TASK_TYPES, key=lambda t: TASK_PRIORITY.get(t, 99))
    for task_type in types:
        meta = TASK_TYPES[task_type]
        cfg = (plan.get("tasks") or {}).get(task_type) or {}
        cap = int(cfg.get("daily_cap") or 0)
        if not (cfg.get("enabled") and meta["implemented"] and cap > 0):
            continue
        done = _done_jobs(schedule, task_type)
        done_n = max(len(done), int(counters.get(task_type) or 0))
        remaining = cap - done_n
        if remaining <= 0:
            continue
        cfg_gap = cfg.get("min_gap_minutes") or plan.get("min_gap_minutes") or 60
        min_gap = float(cfg_gap)
        # 随机幅度：0 = 完全均匀铺开，1 = 每份里随便放（份内）
        spread = float(plan.get("gap_jitter_ratio") or 0.0)
        jitter = float(plan.get("jitter_minutes") or 0)
        seed = "%s|%s|%s" % (day, plan_fingerprint(plan), task_type)
        rng = random.Random(seed)
        win_start, win_end = _window_bounds(day, plan, cfg)
        last_done = None
        for job in done:
            try:
                t = datetime.fromisoformat(job.get("planned_at", ""))
            except Exception:
                continue
            last_done = t if last_done is None else max(last_done, t)
        # 续做：已经做过的部分不重排，剩下的在「剩余时段」里铺开
        # （last_done 只有在本进程排班里才有；重启后只靠 counters 时用整段时段，
        #   确实错过的由 catch_up 顺延——两者互不打架）
        eff_start = win_start
        if last_done is not None:
            eff_start = max(win_start, last_done + timedelta(minutes=min_gap))
        if not_before is not None and not_before > eff_start:
            eff_start = not_before          # 只在「现在之后」铺（见函数 docstring）
        window_minutes = (win_end - eff_start).total_seconds() / 60.0
        # ★ 数量上限是算出来的：(N-1) * G ≤ W → N ≤ floor(W/G) + 1
        limit = feasible_count(window_minutes, min_gap)
        if remaining > limit:
            jobs.append({
                "key": job_key(task_type, day, "overflow", 0),
                "type": task_type, "planned_at": "",
                "status": STATUS_SKIPPED, "units": 0,
                "params": cfg.get("params") or {},
                "note": ("配额 %d %s 排不下：时段剩 %.0f 分钟、最小间隔 %.0f 分钟 → "
                         "最多 %d %s（已按上限排班；想多排就拉长时段或调小间隔）"
                         % (cap, meta["unit"], window_minutes, min_gap, limit,
                            meta["unit"])),
            })
        times = spread_times(eff_start, win_end, min(remaining, limit), min_gap, rng,
                             jitter_minutes=jitter, spread=spread)
        for i, when in enumerate(times):
            jobs.append({
                "key": job_key(task_type, day, "slot", i),
                "type": task_type,
                "planned_at": when.replace(microsecond=0).isoformat(),
                "status": STATUS_PLANNED,
                "units": 1,
                "params": cfg.get("params") or {},
                "note": "",
            })
    _, plan_win_end = _window_bounds(day, plan, {})
    return _deconflict(jobs, win_end=plan_win_end)


def _deconflict(jobs, win_end=None):
    """去碰撞：任意两个待执行作业至少隔开 DECOLLISION_MINUTES 分钟。

    只会往后推，所以同类型的最小间隔不会被破坏；但密集排班（比如把时段排满）
    时可能把尾巴推出运行时段——那样排了也不会执行，索性标记跳过并说明原因。
    """
    pending = [j for j in jobs
               if j.get("status") == STATUS_PLANNED and j.get("planned_at")]
    pending.sort(key=lambda j: j["planned_at"])
    prev = None
    for job in pending:
        t = datetime.fromisoformat(job["planned_at"])
        if prev is not None:
            floor = prev + timedelta(minutes=DECOLLISION_MINUTES)
            if t < floor:
                t = floor
                if win_end is not None and t > win_end:
                    job["planned_at"] = ""
                    job["status"] = STATUS_SKIPPED
                    job["units"] = 0
                    job["note"] = (job.get("note") or "") + \
                        "（避让同刻任务后越出运行时段，本次不排）"
                    continue
                job["planned_at"] = t.replace(microsecond=0).isoformat()
                job["note"] = (job.get("note") or "") + "（避让同刻任务）"
        prev = t
    return jobs


def materialize_day(now, plan, day_data, done_counts=None):
    """确保当日排班存在且与当前计划一致；返回更新后的 day_data。

    三种情况：
      a) 首次进入某一天 → 按配额生成整条时间轴；
      b) 计划被改（配额/时段/间隔/参数）→ 保留已完成的作业，重排未执行部分；
      c) 什么都没变 → 原样返回（时间轴稳定，不因每次 tick 而抖动）。
    """
    day = now.strftime("%Y-%m-%d")
    fingerprint = plan_fingerprint(plan)
    if (not day_data) or day_data.get("date") != day:
        day_data = {"date": day, "plan_hash": fingerprint, "schedule": [],
                    "counters": dict(done_counts or {}), "rescheduled": 0,
                    "notes": []}
    changed = day_data.get("plan_hash") != fingerprint
    if changed or not day_data.get("schedule"):
        keep = [j for j in day_data.get("schedule", [])
                if j.get("status") in _CONSUMED or j.get("type") not in TASK_TYPES]
        day_data["schedule"] = keep + _build_schedule(day, plan, keep,
                                                       day_data.get("counters") or {},
                                                       not_before=now)
        day_data["plan_hash"] = fingerprint
        day_data.setdefault("notes", []).append(
            "%s 按最新计划重排（保留已完成 %d 项）" % (now.strftime("%H:%M"), len(keep)))
    day_data["schedule"] = _deconflict(day_data["schedule"])
    return day_data

def apply_catch_up(now, plan, day_data):
    """处理错过的作业（电脑关机 / 程序没开 / 暂停导致）。

    判定：计划时间已过 **且超过宽限期**（CATCH_UP_GRACE_MINUTES）才算「错过」——
    刚过点的作业是正常到期，直接执行即可（否则每次 tick 都会把它顺延，永远不执行）。

    same_day 策略下顺延要**保住同类最小间隔**：
      1) 错过的作业按原计划顺序顺延（从 now 起排，且与「同类上一次完成/上一次顺延」保持间隔）；
      2) 后续未执行的作业跟着往后推（保持顺序与间隔）；
      3) 最后统一去碰撞，仍排不下的标记跳过并说明原因。
    """
    policy = plan.get("catch_up", "same_day")
    grace = timedelta(minutes=CATCH_UP_GRACE_MINUTES)
    missed, future, changed = [], [], False
    for job in day_data.get("schedule", []):
        if job.get("status") != STATUS_PLANNED or not job.get("planned_at"):
            continue
        t = datetime.fromisoformat(job["planned_at"])
        if t <= now - grace:
            missed.append((t, job))
        elif t > now:
            future.append((t, job))
    if not missed:
        return day_data
    missed.sort(key=lambda x: x[0])
    future.sort(key=lambda x: x[0])
    if policy != "same_day":
        for _, job in missed:
            job["status"] = STATUS_SKIPPED
            job["note"] = ("错过时间点（策略：不补做）" if policy == "none"
                           else "错过时间点（策略：顺延到下一个运行时段）")
        changed = True
        if changed:
            day_data["schedule"] = _deconflict(day_data["schedule"])
        return day_data
    # ---- same_day：从 now 起按间隔把错过的作业排进当天剩余时段 ----
    anchor = {}                     # {type: 该类最后一次「占位」时间}
    for job in day_data.get("schedule", []):
        if job.get("status") in _CONSUMED and job.get("planned_at"):
            try:
                t = datetime.fromisoformat(job["planned_at"])
            except Exception:
                continue
            anchor[job["type"]] = max(anchor.get(job["type"], t), t)
    placed_missed = 0
    for _, job in missed:
        task_type = job["type"]
        cfg = (plan.get("tasks") or {}).get(task_type) or {}
        gap = float(cfg.get("min_gap_minutes") or plan.get("min_gap_minutes") or 60)
        win_end = _window_bounds(day_data["date"], plan, cfg)[1]
        # ★ anchor 里没有该类（今天还什么都没做）→ 第一项就从 now+2 开始，
        #   不能按「now + 一个完整间隔」推：那要求剩余时段 ≥ 一个间隔，
        #   否则晚上启动时第一项也会被误判成「当天排不下」而全部跳过
        prev = anchor.get(task_type)
        if job.get("caught_up"):
            # 已经顺延过一次还没轮到（tick 间隔被拖长/电脑休眠）→ 直接现在执行，
            # 否则「顺延到 now+2、下次又错过、再顺延到 now+2」会永远轮不到它
            base = now
        else:
            base = now + timedelta(minutes=2)
            if prev is not None:
                base = max(base, prev + timedelta(minutes=gap))
        if base > win_end:
            job["status"] = STATUS_SKIPPED
            job["note"] = "错过时间点且当天时段已过（未补做）"
            continue
        job["planned_at"] = base.replace(microsecond=0).isoformat()
        job["caught_up"] = True
        job["note"] = (job.get("note") or "") + "（错过原时间点，当日内顺延）"
        anchor[task_type] = base
        placed_missed += 1
    # 未执行的后续作业跟着往后推（保持同类间隔）
    for t, job in future:
        task_type = job["type"]
        cfg = (plan.get("tasks") or {}).get(task_type) or {}
        gap = float(cfg.get("min_gap_minutes") or plan.get("min_gap_minutes") or 60)
        win_end = _window_bounds(day_data["date"], plan, cfg)[1]
        # ★ 只有「前面真有作业被顺延」时才需要跟着推；没有 anchor 就保持原时间。
        #   旧写法 anchor.get(type, t) 会退化成 t + gap —— 一个还没到点的作业
        #   会被推到「自己 + 一个间隔」之后，凑巧越过时段末端就被判跳过（线上踩到过）
        prev = anchor.get(task_type)
        base = t if prev is None else max(t, prev + timedelta(minutes=gap))
        if base > win_end:
            job["status"] = STATUS_SKIPPED
            job["note"] = "顺延后已超出当天时段（未执行）"
            continue
        if base != t:
            job["planned_at"] = base.replace(microsecond=0).isoformat()
            job["note"] = (job.get("note") or "") + "（因补做顺延）"
        anchor[task_type] = base
    day_data["rescheduled"] = int(day_data.get("rescheduled") or 0) + placed_missed
    day_data["schedule"] = _deconflict(day_data["schedule"])
    return day_data


def plan_retry(now, plan, day_data, failed_job, delay_minutes=20):
    """失败补位：当天配额还没做完时，晚些再补一次（用户要求「当日完成即可」）。

    约束：补位作业也占配额、也走同一个窗口与去碰撞；窗口内排不下就不补。
    返回 True 表示已补位（调用方负责落盘）。
    """
    task_type = failed_job.get("type")
    cfg = (plan.get("tasks") or {}).get(task_type) or {}
    cap = int(cfg.get("daily_cap") or 0)
    if cap <= 0:
        return False
    sched = day_data.get("schedule") or []
    same = [j for j in sched if j.get("type") == task_type]
    done = len([j for j in same if j.get("status") in _CONSUMED])
    pending = len([j for j in same if j.get("status") == STATUS_PLANNED])
    if done + pending >= cap:
        return False
    win_end = _window_bounds(day_data["date"], plan, cfg)[1]
    when = now + timedelta(minutes=max(5, int(delay_minutes)))
    if when > win_end:
        return False
    seq = len(same)
    job = {
        "key": job_key(task_type, day_data["date"], "retry", seq),
        "type": task_type,
        "planned_at": when.replace(microsecond=0).isoformat(),
        "status": STATUS_PLANNED,
        "units": 1,
        "params": failed_job.get("params") or {},
        "note": "失败补位（当日配额未完成）",
    }
    sched.append(job)
    day_data["schedule"] = _deconflict(sched)
    return True


def due_jobs(now, day_data):
    """该执行的作业：已到点且未执行（按任务优先级与计划时间排序）。"""
    out = []
    for job in day_data.get("schedule", []):
        if job.get("status") != STATUS_PLANNED or not job.get("planned_at"):
            continue
        if datetime.fromisoformat(job["planned_at"]) <= now:
            out.append(job)
    out.sort(key=lambda j: (TASK_PRIORITY.get(j["type"], 99), j["planned_at"]))
    return out


def next_job(now, day_data):
    """下一个待执行作业（UI 倒计时用）；没有返回 None。"""
    pending = [j for j in day_data.get("schedule", [])
               if j.get("status") == STATUS_PLANNED and j.get("planned_at")]
    if not pending:
        return None
    return min(pending, key=lambda j: j["planned_at"])


def window_open(now, plan):
    """当前是否在运行时段内。"""
    win = plan.get("window") or {}
    start_t = parse_hhmm(win.get("start"), parse_hhmm("08:00"))
    end_t = parse_hhmm(win.get("end"), parse_hhmm("23:30"))
    return start_t <= now.time() <= end_t


def summarize(now, plan, day_data):
    """给 UI 的进度摘要：每类任务的配额/已完成/待执行 + 下一个时间点。"""
    schedule = day_data.get("schedule", []) or []
    per_type = {}
    for task_type, meta in TASK_TYPES.items():
        cfg = (plan.get("tasks") or {}).get(task_type) or {}
        jobs = [j for j in schedule if j.get("type") == task_type]
        done = len([j for j in jobs if j.get("status") in _CONSUMED])
        failed = len([j for j in jobs if j.get("status") == STATUS_FAILED])
        pending = len([j for j in jobs if j.get("status") == STATUS_PLANNED])
        skipped = len([j for j in jobs if j.get("status") == STATUS_SKIPPED])
        win = cfg.get("window") or plan.get("window") or {}
        min_gap = int(cfg.get("min_gap_minutes") or plan.get("min_gap_minutes") or 60)
        start_t = parse_hhmm(win.get("start"), parse_hhmm("08:00"))
        end_t = parse_hhmm(win.get("end"), parse_hhmm("23:30"))
        win_minutes = ((end_t.hour * 60 + end_t.minute)
                       - (start_t.hour * 60 + start_t.minute))
        per_type[task_type] = {
            "label": meta["label"],
            "unit": meta["unit"],
            "lane": meta["lane"],
            "implemented": meta["implemented"],
            "enabled": bool(cfg.get("enabled")),
            "cap": int(cfg.get("daily_cap") or 0),
            "window": win,
            "min_gap_minutes": min_gap,
            # ★ 上限 = floor(时段分钟 / 最小间隔) + 1：设置页据此提示「最多能排几个」
            "window_minutes": win_minutes,
            "max_per_day": feasible_count(win_minutes, min_gap),
            "done": done, "pending": pending, "failed": failed, "skipped": skipped,
            "desc": meta["desc"],
        }
    nxt = next_job(now, day_data)
    active_types = [v for v in per_type.values() if v["enabled"] and v["implemented"]]
    return {
        "day": day_data.get("date"),
        "in_window": window_open(now, plan),
        "next_job": nxt,
        "rescheduled": int(day_data.get("rescheduled") or 0),
        "per_type": per_type,
        "done_total": sum(v["done"] for v in per_type.values()),
        "plan_total": sum(v["cap"] for v in active_types),
        "window_label": "%s-%s" % ((plan.get("window") or {}).get("start", ""),
                                   (plan.get("window") or {}).get("end", "")),
        "notes": (day_data.get("notes") or [])[-5:],
    }