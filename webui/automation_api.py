# -*- coding: utf-8 -*-
"""自动化模块 API（时间轴调度：状态/启停/计划/立即执行/历史）。

分工：本模块只做 HTTP 边界（校验 + 转发），调度逻辑全在 automation/ 包里，
因此纯逻辑单测不需要起服务。
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from automation import store as _store
from automation.model import DEFAULT_PLAN, TASK_TYPES, normalize_plan
from automation.scheduler import get_scheduler

log = logging.getLogger(__name__)

router = APIRouter()


class _PlanBody(BaseModel):
    plan: dict | None = None


class _RunNowBody(BaseModel):
    type: str = ""
    dry_run: bool = False       # 演练：只走到「发布回答」按钮前，不点（不可逆）


class _PauseBody(BaseModel):
    reason: str = ""


@router.get("/api/automation")
def api_automation_status():
    """时间轴 + 今日进度 + 台账 + 通知（前端每几秒轮询一次）。"""
    return get_scheduler().status()


@router.get("/api/automation/types")
def api_automation_types():
    """任务类型目录（UI 用它渲染泳道与配置表；未实现的类型也返回，标 implemented=false）。"""
    return {"types": [dict(id=k, **v) for k, v in TASK_TYPES.items()],
            "default_plan": DEFAULT_PLAN}


@router.post("/api/automation/plan")
def api_automation_plan(body: _PlanBody):
    """保存计划（前端改配额/时段/开关／间隔后调用）。返回归一化后的计划。"""
    raw = body.plan if isinstance(body.plan, dict) else {}
    plan = _store.save_plan(raw)
    log.info("自动化计划已更新：enabled=%s 配额=%s", plan.get("enabled"),
             {k: v.get("daily_cap") for k, v in (plan.get("tasks") or {}).items()
              if v.get("enabled")})
    return {"ok": True, "plan": plan, "status": get_scheduler().status()}


@router.post("/api/automation/start")
def api_automation_start():
    """开始（按台账续做剩余配额）。"""
    sched = get_scheduler()
    sched.start()
    return {"ok": True, "status": sched.status()}


@router.post("/api/automation/stop")
def api_automation_stop():
    """停止（会请求中断正在执行的作业；已完成记录保留）。"""
    sched = get_scheduler()
    sched.stop()
    return {"ok": True, "status": sched.status()}


@router.post("/api/automation/pause")
def api_automation_pause(body: _PauseBody):
    sched = get_scheduler()
    sched.pause(body.reason or "手动暂停")
    return {"ok": True, "status": sched.status()}


@router.post("/api/automation/resume")
def api_automation_resume():
    sched = get_scheduler()
    sched.resume()
    return {"ok": True, "status": sched.status()}


@router.post("/api/automation/run-now")
def api_automation_run_now(body: _RunNowBody):
    """立即执行下一个（或指定类型的）作业。"""
    task_type = (body.type or "").strip()
    if task_type and task_type not in TASK_TYPES:
        raise HTTPException(400, "未知任务类型：%s" % task_type)
    r = get_scheduler().run_now(task_type, dry_run=bool(body.dry_run))
    return {"ok": bool(r.get("ok")), "message": r.get("message", ""),
            "status": get_scheduler().status()}


@router.get("/api/automation/history")
def api_automation_history(days: int = 7):
    """最近 N 天的台账（默认 7 天；UI 的历史视图）。"""
    from datetime import datetime, timedelta
    days = max(1, min(int(days or 7), 60))
    today = datetime.now()
    out = []
    for i in range(days):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        out.extend(_store.load_ledger(day))
    out.sort(key=lambda r: r.get("finished_at") or r.get("started_at") or "",
             reverse=True)
    return {"rows": out[:500], "days": days}
