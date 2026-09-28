# ============================================================
# webui/site_progress.py — 进度校核的编排层（读页面 → core 解析 → 落盘 → 记差异）
#
# 为什么单独一层：把「浏览器交互」「纯逻辑」「自动化计数」三件事分开，
# 任何一侧改版都只影响自己：
#     browser_progress.py（只读原语）→ site_progress.py（本文件，编排）
#                                        ├→ core/progress.py（解析/快照/计数出口）
#                                        └→ reconcile.jsonl（差异）
#
# ★ 用法：**在浏览器已经开着的时候**调用（任务开始、任务结束后、开机）。
#   绝不为了校核单独拉起浏览器——那会与正在跑的任务抢 profile 锁。
# ============================================================

import logging

from core import progress as _progress

log = logging.getLogger(__name__)


def refresh(browser, *, ledger_units=None, now=None, note="", force=False):
    """校核一次并落盘。返回 ProgressSnapshot 或 None（读不到时）。

    ledger_units：本次校核时台账口径的数字（用于记差异）。不传则不算差异。
    force=True：即使刚校核过也重新读（发布类作业失败后用）。
    """
    from applications.zhihu_story import browser_progress

    if not force and is_fresh(now=now):
        return _progress.load(_today(now))
    raw = browser_progress.read_raw(browser, now=now)
    if not raw:
        log.info("进度校核：本次没读到线上状态，继续沿用本地计数")
        return None
    snap = _progress.build_snapshot(
        day=raw.get("day") or _today(now),
        published_payload=raw.get("published"),
        draft_payload=raw.get("drafts"),
        # 时间戳由 Python 统一打：本地时间，与全系统口径一致
        at=(now or _dt_now()).strftime("%Y-%m-%dT%H:%M:%S"))
    if not _progress.save(snap):
        return snap                        # 落盘失败也把快照交给调用方用
    log.info("进度校核：线上今日已发布 %d 篇 · 待发草稿 %d 篇（%s）",
             snap.published_today, snap.drafts_pending, snap.at)
    if ledger_units is not None:
        _progress.log_reconcile(day=snap.day, ledger_units=ledger_units,
                                snapshot=snap, note=note)
    return snap


def load(day=""):
    """读当天快照（不碰浏览器）。跨天/缺失返回 None，调用方退回本地计数。"""
    return _progress.load(day or _today())


def counts_for_ledger(day, task_type, ledger_units):
    """给台账口径的计数做「以线上为准」的合并。

    这是全系统**唯一的计数合并出口**：automation/store.py 只调它，
    不直接读快照、更不直接读页面。
    """
    snap = load(day)
    if snap is None:
        return int(ledger_units or 0), None
    return _progress.merge_counts(task_type, ledger_units, snap, day=day), snap


def summary_line(day=""):
    """给界面/通知用的一行事实；没有快照返回空串。"""
    snap = load(day or _today())
    if snap is None:
        return ""
    age = snap.age_seconds()
    when = snap.at[11:16] if len(snap.at) >= 16 else snap.at
    stale = ""
    if age is not None and age > 3600:
        stale = "（%.1f 小时前，可能已过期）" % (age / 3600.0)
    return ("进度校核 %s：线上今日已发布 %d 篇 · 待发草稿 %d 篇%s"
            % (when, snap.published_today, snap.drafts_pending, stale))


def status_payload(day=""):
    """给 API 的结构化状态（UI 直接渲染，不再自己拼）。"""
    day = day or _today()
    snap = load(day)
    if snap is None:
        return {"ok": False, "day": day, "snapshot": None,
                "reconcile": _progress.load_reconcile(day)}
    return {"ok": True, "day": day, "snapshot": snap.to_dict(),
            "reconcile": _progress.load_reconcile(day)}


def is_fresh(max_age_seconds=300, now=None):
    """刚刚校核过就不重复读（同一任务内多次调用只读一次）。"""
    snap = load(_today(now))
    if snap is None:
        return False
    age = snap.age_seconds(now)
    return age is not None and age <= max(1, int(max_age_seconds))


def _today(now=None):
    return (now or _dt_now()).strftime("%Y-%m-%d")


def _dt_now():
    import datetime as _dt
    return _dt.datetime.now()
