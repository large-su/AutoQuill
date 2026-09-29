# -*- coding: utf-8 -*-
"""自动化模块：任务类型契约 + 计划默认值与校验。

设计取自用户口径（2026-09-19 讨论）：
  - 发布草稿 3 篇 / 全链路撰写 3 篇，都可自由设置；**发布顺序从旧到新**；
  - 运行时段默认 08:00–23:30（可设置）；
  - 同类任务最小间隔 ≥60 分钟且**随机化**，当日完成即可（不固定时间点）；
  - 随时可停；再次开始时先看「今天已发布多少 / 已写多少」，在此基础上续做。

**当前四个任务类型**（2026-09-27 更新）：
  · 发布草稿 / 全链路撰写——「要做 N 次」的重复任务，画在 24 小时时间轴上；
  · 打卡互动 / 回复评论——「一天一次」的单次任务（job_mode=single），
    画在「单次任务轴」上，另有阶段闸门保证「先回复、后检查」。
历史：2026-09-24 曾砍到只剩发布+撰写；2026-09-27 用户要求把打卡与评论回复做回来
（真机探针证明可行且成本低），于是重新登记这两个类型。
契约层不登记任何「预留/未实现」类型：清单里有的就是能跑的，
免得 UI 与排班里出现永远不执行的空泳道。
默认开关：DEFAULT_ENABLED 里的四个任务在新装（计划里没有该键）时默认开启；
已有计划里的显式开关（含熔断自动停用）永不被覆盖。
"""

import copy
import re
from datetime import time as _time

# ------------------------------------------------------------
# 任务类型契约：新增任务只需在这里登记 + 在 executor 里实现
# ------------------------------------------------------------
# lane：时间轴泳道（越小越靠上）——同一类任务画在同一行，避免视觉打架
TASK_TYPES = {
    "checkin": {
        "label": "打卡互动",
        "unit": "项",
        "lane": 2,
        "implemented": True,          # 2026-09-27：打卡巡检 + 写草稿顺带互动
        "default_cap": 1,
        # 一天只排一次班（一次把该做的都做完，不分散在多个时间点）
        "job_mode": "single",
        # 单次任务轴：谁在轴上、以及先后次序（阶段小的先跑）
        "axis": "single",
        "single_stage": 2,            # 阶段 2：必须等回复评论（阶段 1）结束才允许派发
        # 默认落在晚上：它同时是「今天打卡成没成」的兜底与数据源。
        # 20:30–22:30 留出重试余量：补做失败会自动补位（最晚 22:30）。
        "default_window": {"start": "20:30", "end": "22:30"},
        "desc": "读当期打卡页；关注/赞同还没达成时补做（含取关再关注兜底）",
        "params": {},
    },
    "reply_comment": {
        "label": "回复评论",
        "unit": "条",
        "lane": 3,
        "implemented": True,          # 2026-09-27：挑最友善的读者评论回复
        "default_cap": 3,
        "job_mode": "single",         # 一天一班，一次把该回的都回完
        "axis": "single",
        "single_stage": 1,            # 阶段 1：回复必须先做完，打卡检查才动手
        # 窗口收在 20:00 前：保证「回复 → 检查」这条链在当天留出余量
        "default_window": {"start": "10:00", "end": "20:00"},
        "desc": "挑最友善的读者评论回复（默认演练：只生成不发送）",
        # dry_run 默认 True：用户要求先演练两天，语气确认后再切自动
        "params": {"dry_run": True},
    },
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

# 执行顺序上的偏好（同一分钟到点时按此排序）：
#   先发布（对外可见的动作尽量落在白天）→ 再写草稿 → 然后回复评论 → 最后打卡检查。
# ★ 打卡检查排在最后不是"顺手"，是语义：它要确认的是"今天该做的都做完了没"。
#   真正的保障是 planner 的「阶段闸门」（single_stage），这里只是同刻排序的兜底。
TASK_PRIORITY = {"publish_drafts": 0, "full_chain": 1, "reply_comment": 2,
                 "checkin": 3}

# 全局去碰撞：任意两个作业至少隔开的分钟数（避免同一分钟挤成一堆）
DECOLLISION_MINUTES = 15

# 新装默认开启的任务类型。
# ★ 2026-09-29 稳定期调整：reply_comment **移出默认**。
#   它是四个任务里唯一需要「共享浏览器 + 网页版大模型」双通道的一环，也是最
#   容易出错的一环（09-27/28 连续误判失败、09-29 的跨线程崩溃又把它和发布链路
#   一起带崩）。稳定期先不默认开启；要用的用户在面板上手动打开即可。
#   注意：**只在计划里没有这个键时生效**——用户已有的显式开关（含熔断自动停用、
#   以及本次手工关闭）永远不会被覆盖。
DEFAULT_ENABLED = ("full_chain", "publish_drafts", "checkin")

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
    enabled = bool(raw.get("enabled", task_type in DEFAULT_ENABLED))
    return {
        "enabled": enabled,
        "daily_cap": cap,
        "min_gap_minutes": raw.get("min_gap_minutes"),   # None = 用计划全局值
        # None = 用计划全局值；任务类型自带默认时段时用它的（如打卡巡检在晚上）
        "window": raw.get("window") or meta.get("default_window"),
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
