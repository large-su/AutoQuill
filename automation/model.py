# -*- coding: utf-8 -*-
"""自动化模块：任务类型契约 + 计划默认值与校验。

设计取自用户口径（2026-09-19 讨论）：
  - 发布草稿 3 篇 / 全链路撰写 3 篇，都可自由设置；**发布顺序从旧到新**；
  - 运行时段默认 08:00–23:30（可设置）；
  - 同类任务最小间隔 ≥60 分钟且**随机化**，当日完成即可（不固定时间点）；
  - 随时可停；再次开始时先看「今天已发布多少 / 已写多少」，在此基础上续做。

**任务类型只保留两个（用户 2026-09-24 口径）**：发布草稿 + 全链路撰写。
打卡挑战（网页端不好操作）与互动类（感谢/赞同/回复评论，看着太乱）都不做——
「把写故事、发布故事这两件事做准、做稳」比铺功能重要。
契约层刻意不再登记任何「预留/未实现」类型：清单里有的就是能跑的，
免得 UI 与排班里出现永远不执行的空泳道。
"""

import copy
import re
from datetime import time as _time

# ------------------------------------------------------------
# 任务类型契约：新增任务只需在这里登记 + 在 executor 里实现
# ------------------------------------------------------------
# lane：时间轴泳道（越小越靠上）——同一类任务画在同一行，避免视觉打架
TASK_TYPES = {
    "publish_drafts": {
        "label": "发布草稿",
        "unit": "篇",
        "lane": 0,
        "implemented": True,           # M2 已接入（2026-09-19 真机探针 + 发布链路）
        "default_cap": 3,
        "desc": "从草稿箱按「从旧到新」逐篇发布（公开可见，不可逆）",
        "params": {"order": "oldest_first", "source": "all"},
    },
    "full_chain": {
        "label": "全链路撰写",
        "unit": "篇",
        "lane": 1,
        "implemented": True,
        "default_cap": 3,
        "desc": "选题 → 提取 → 生成 → 校验 → 写入草稿箱（不公开）",
        "params": {"mode": "single", "rounds": 1},   # single=经典链路，clean=纯净链路
    },
}

# 执行顺序上的偏好：同一分钟到点时，先发布（对外可见的动作尽量落在白天）
TASK_PRIORITY = {"publish_drafts": 0, "full_chain": 1}

# 全局去碰撞：任意两个作业至少隔开的分钟数（避免同一分钟挤成一堆）
DECOLLISION_MINUTES = 15

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text, default=_time(8, 0)):
    """'09:30' → datetime.time；非法值回退 default（配置永远不该让程序崩）。"""
    m = _HHMM.match(str(text or "").strip())
    if not m:
        return default
    return _time(int(m.group(1)), int(m.group(2)))


def hhmm(t):
    """datetime.time → '09:30'。"""
    if t is None:
        return ""
    return "%02d:%02d" % (t.hour, t.minute)


DEFAULT_PLAN = {
    "enabled": False,                 # 总开关（用户点「开始」才置 True）
    "window": {"start": "08:00", "end": "23:30"},
    "jitter_minutes": 8,              # 触发点随机抖动上限（±）
    "min_gap_minutes": 60,            # 同类任务最小间隔（用户要求 ≥1 小时）
    "gap_jitter_ratio": 0.6,          # 间隔随机化：实际 ∈ [min, min*(1+ratio)]
    "catch_up": "same_day",           # none | same_day | next_window
    "pause_when_user_busy": True,     # 手动任务在跑时，自动化让路
    "notify_on_needs_human": True,    # 登录失效/验证码 → 暂停并通知
    "tasks": {},                      # 见 normalize_plan：按 TASK_TYPES 补齐
}


def _norm_task(task_type, raw):
    """归一单个任务配置：缺省值来自 TASK_TYPES，非法值退回默认。"""
    meta = TASK_TYPES[task_type]
    raw = raw if isinstance(raw, dict) else {}
    cap = raw.get("daily_cap", meta["default_cap"])
    try:
        cap = max(0, int(cap))
    except (TypeError, ValueError):
        cap = meta["default_cap"]
    params = copy.deepcopy(meta["params"])
    if isinstance(raw.get("params"), dict):
        params.update(raw["params"])
    enabled = bool(raw.get("enabled", task_type in ("full_chain", "publish_drafts")))
    return {
        "enabled": enabled,
        "daily_cap": cap,
        "min_gap_minutes": raw.get("min_gap_minutes"),   # None = 用计划全局值
        "window": raw.get("window"),                     # None = 用计划全局值
        "params": params,
    }


def normalize_plan(raw):
    """把任意（可能残缺/手改坏）的计划归一成完整可用的计划。

    约定：**永不因为配置问题抛错**——无法识别的值一律回退默认，
    保证自动化在用户改坏配置后仍能按安全默认值运行。
    """
    raw = raw if isinstance(raw, dict) else {}
    plan = copy.deepcopy(DEFAULT_PLAN)
    plan["enabled"] = bool(raw.get("enabled", False))
    win = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    start = parse_hhmm(win.get("start"), parse_hhmm(DEFAULT_PLAN["window"]["start"]))
    end = parse_hhmm(win.get("end"), parse_hhmm(DEFAULT_PLAN["window"]["end"]))
    if end <= start:                      # 跨天或写反 → 退回默认，避免排班为空
        start = parse_hhmm(DEFAULT_PLAN["window"]["start"])
        end = parse_hhmm(DEFAULT_PLAN["window"]["end"])
    plan["window"] = {"start": hhmm(start), "end": hhmm(end)}
    for key, lo, hi, default in (("jitter_minutes", 0, 120, 8),
                                 ("min_gap_minutes", 5, 720, 60),
                                 ("gap_jitter_ratio", 0.0, 1.0, 0.6)):
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            val = default
        val = min(max(val, lo), hi)
        plan[key] = int(val) if key != "gap_jitter_ratio" else val
    catch_up = str(raw.get("catch_up", "same_day")).strip()
    plan["catch_up"] = catch_up if catch_up in ("none", "same_day", "next_window") \
        else "same_day"
    plan["pause_when_user_busy"] = bool(raw.get("pause_when_user_busy", True))
    plan["notify_on_needs_human"] = bool(raw.get("notify_on_needs_human", True))
    tasks_raw = raw.get("tasks") if isinstance(raw.get("tasks"), dict) else {}
    plan["tasks"] = {t: _norm_task(t, tasks_raw.get(t)) for t in TASK_TYPES}
    return plan


def task_label(task_type):
    """任务类型的中文名（UI/台账/日志共用）。"""
    return TASK_TYPES.get(task_type, {}).get("label", task_type)


def task_lane(task_type):
    return int(TASK_TYPES.get(task_type, {}).get("lane", 99))


def job_key(task_type, day, target="", seq=0):
    """幂等键：同一天 + 同类型 + 同目标 只执行一次（重启/重试都不重复）。"""
    return ":".join(["auto", str(day), task_type, str(target or "-"), str(seq)])
