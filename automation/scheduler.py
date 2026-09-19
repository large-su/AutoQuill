# -*- coding: utf-8 -*-
"""自动化调度器：tick 到点入队 → 串行执行 → 记台账 → 熔断/暂停/通知。

分工与约束：
  - **极轻**：tick 只做时间比较与读写小 JSON（<1KB），浏览器只在作业真正执行时拉起；
  - **串行**：同一时刻只跑一个作业（共享浏览器 profile 的硬约束），手动任务优先让路；
  - **幂等**：作业带唯一 key（日期+类型+序号），重启/重试都不重复；
  - **可解释**：每一次「做了/没做」都落台账，UI 能查到原因；
  - **可停**：随时 pause/stop；stop 会请求中断正在执行的作业；
  - **续做**：再次 start 时按台账统计「今天已发布/已写多少」，只补差额。
"""

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta

from automation import model, planner, store
from automation.executor import BrowserBusy, NeedHuman, execute
from automation.planner import (
    STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_HUMAN, STATUS_PLANNED,
    STATUS_RUNNING, STATUS_SKIPPED,
)

log = logging.getLogger(__name__)

# 同类任务连续失败达到这个次数 → 熔断该类任务（停用并通知），避免无人值守时反复撞墙
CIRCUIT_BREAK_AFTER = 3
# 浏览器被占用时的重排延迟（分钟）
BUSY_RETRY_MINUTES = 5
# 作业失败后的补位延迟（分钟）：当日配额没完成就再补一次（熔断前最多补 2 次）
RETRY_DELAY_MINUTES = 20


class AutomationScheduler:
    """自动化调度器（单例挂在 webui 侧；纯逻辑，可注入时钟与执行器做单测）。"""

    def __init__(self, notify=None, executor=None, now_fn=datetime.now,
                 sleep_fn=time.sleep, tick_seconds=5):
        self._notify = notify or (lambda *a, **k: None)
        self._execute = executor or execute
        self._now = now_fn
        self._sleep = sleep_fn
        self._tick_seconds = tick_seconds
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._paused = False
        self._pause_reason = ""
        self._running_job = None
        self._run_progress = None
        self._fails = {}          # {task_type: 连续失败次数}
        self._notices = []        # 给 UI 的通知（环形，最多 50 条）
        self._last_day_hash = ""
        self._closed_day = ""     # 已经为哪一天做过「时段结束收尾」（每天只通知一次）

    # ---------------- 通知 ----------------

    def _push_notice(self, level, text):
        item = {"at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
                "level": level, "text": text}
        with self._lock:
            self._notices.append(item)
            del self._notices[:-50]
        if level == "info":
            log.info("自动化通知：%s", text)
        else:
            log.warning("自动化通知[%s]：%s", level, text)
        try:
            self._notify(item)
        except Exception:          # noqa: BLE001
            pass

    # ---------------- 生命周期 ----------------

    def start(self):
        """用户点「开始」：置计划 enabled、按今日台账续做、拉起 tick 线程。"""
        plan = store.load_plan()
        plan["enabled"] = True
        store.save_plan(plan)
        with self._lock:
            self._paused = False
            self._pause_reason = ""
        self._stop_event.clear()
        if not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="automation-scheduler")
            self._thread.start()
        done = store.done_counts(self._now().strftime("%Y-%m-%d"))
        self._push_notice("info", "自动化已启动；今日已完成：%s"
                          % (", ".join("%s %d" % (k, v) for k, v in done.items())
                             or "暂无"))
        return self.status()

    def pause(self, reason=""):
        with self._lock:
            self._paused = True
            self._pause_reason = reason or "手动暂停"
        self._push_notice("warn", "自动化已暂停：%s" % self._pause_reason)
        return self.status()

    def resume(self):
        with self._lock:
            self._paused = False
            self._pause_reason = ""
        self._push_notice("info", "自动化已继续")
        return self.status()

    def stop(self, cancel_running=True):
        """用户点「停止」：关闭总开关，并请求中断正在执行的作业。"""
        plan = store.load_plan()
        plan["enabled"] = False
        store.save_plan(plan)
        self._stop_event.set()
        if cancel_running and self._running_job:
            try:
                from webui.run_manager import runner
                runner.stop()
            except Exception:      # noqa: BLE001
                pass
        self._push_notice("info", "自动化已停止（今天已完成的记录保留，再次开始会续做）")
        return self.status()

    # ---------------- 主循环（极轻：只比时间 + 读写小 JSON） ----------------

    def _loop(self):
        log.info("自动化调度器已启动（tick=%ss）", self._tick_seconds)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:          # noqa: BLE001
                log.exception("自动化 tick 异常（已忽略，下一轮继续）")
            self._sleep(self._tick_seconds)
        log.info("自动化调度器已退出")

    def _tick(self):
        now = self._now()
        plan = store.load_plan()
        if not plan.get("enabled"):
            return
        with self._lock:
            if self._paused or self._running_job:
                return
        day = now.strftime("%Y-%m-%d")
        day_data = store.load_day(day)
        # 续做：按台账里「今天已完成的量」扣减配额后再生成排班（用户明确要求）
        day_data = planner.materialize_day(now, plan, day_data,
                                           store.done_counts(day))
        day_data = planner.apply_catch_up(now, plan, day_data)
        self._save_day_if_changed(day, day_data)
        in_window = planner.window_open(now, plan)
        if not in_window:
            # 时段外不派活（这就是「运行时段」的全部含义），但：
            #  · 用户手动点「立即执行」的作业照做（手动优先于时段）；
            #  · 时段**已经过了**（不是还没到）时给今天收尾：剩下的标记跳过 + 通知一次，
            #    否则它们会一直挂在「待执行」，看起来像程序卡住了。
            _, win_end = planner.window_bounds(day, plan)
            if now > win_end:
                self._close_out_day(now, plan, day, day_data)
            due_manual = [j for j in planner.due_jobs(now, day_data)
                          if j.get("manual")]
            if not due_manual:
                return
            due = due_manual
        else:
            due = planner.due_jobs(now, day_data)
            if not due:
                return
        if plan.get("pause_when_user_busy"):
            from webui.browser_tasks import browser_busy
            if browser_busy():
                return                     # 手动任务优先，等它跑完再派活
        self._run_job(due[0], day, day_data, plan)

    def _close_out_day(self, now, plan, day, day_data):
        """时段已过：给今天剩下的「待执行」作业一个明确收尾（每天只通知一次）。

        为什么要做：apply_catch_up 会把「错过且当天排不下」的作业标成跳过，
        但如果程序一直在跑、只是时段结束了，作业会停在「待执行」——用户看到的是
        「说好做 3 篇，结果一直挂着」。这里统一收尾：标记跳过 + 明确原因 + 通知一次。
        """
        if self._closed_day == day:
            return
        schedule = day_data.get("schedule", []) or []
        leftover = [j for j in schedule if j.get("status") == STATUS_PLANNED]
        # 「本该做但没做成」的口径：还挂着的 + 被跳过/失败的（排不下那种 units=0 的说明不算）
        unfinished = [j for j in schedule
                      if j.get("status") in (STATUS_PLANNED, STATUS_FAILED)
                      or (j.get("status") == STATUS_SKIPPED and j.get("units"))]
        self._closed_day = day
        if not unfinished:
            return
        for job in leftover:
            job["status"] = STATUS_SKIPPED
            job["note"] = (job.get("note") or "") + "（运行时段已结束，今天不再执行）"
            store.append_ledger({
                "day": day, "key": job["key"], "type": job["type"],
                "status": STATUS_SKIPPED, "units": 0,
                "planned_at": job.get("planned_at"),
                "finished_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "message": "运行时段已结束，未执行",
                "artifacts": [], "dry_run": bool(job.get("dry_run")),
            })
        if leftover:
            self._save_day_if_changed(day, day_data)
        done = len([j for j in schedule if j.get("status") == STATUS_DONE])
        self._push_notice(
            "info",
            "运行时段已结束：今天完成 %d 项、未执行 %d 项（原因见时间轴）；明天 %s 继续"
            % (done, len(unfinished), (plan.get("window") or {}).get("start", "08:00")))

    def _save_day_if_changed(self, day, day_data):
        blob = json.dumps(day_data, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()
        key = day + ":" + digest
        if key != self._last_day_hash:
            store.save_day(day, day_data)
            self._last_day_hash = key

    def _should_stop(self):
        return self._stop_event.is_set()

    def _on_progress(self, st):
        with self._lock:
            self._run_progress = dict(st.get("progress") or {}) or {
                "text": st.get("message") or "", "pct": None}

    # ---------------- 执行单个作业 ----------------

    def _run_job(self, job, day, day_data, plan):
        started = self._now()
        job["status"] = STATUS_RUNNING
        job["started_at"] = started.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._running_job = dict(job)
            self._run_progress = {"text": "启动…", "pct": None}
        self._save_day_if_changed(day, day_data)
        store.append_ledger({"day": day, "key": job["key"], "type": job["type"],
                             "status": STATUS_RUNNING,
                             "planned_at": job.get("planned_at"),
                             "started_at": job["started_at"]})
        log.info("自动化执行：%s（计划 %s）", job["type"],
                 (job.get("planned_at") or "")[11:16])
        result = None
        try:
            result = self._execute(job, should_stop=self._should_stop,
                                   progress=self._on_progress)
        except BrowserBusy as exc:
            # 排队重来：浏览器被手动任务占用，不算失败（用户优先）
            nxt = (self._now() + timedelta(minutes=BUSY_RETRY_MINUTES))
            job["status"] = STATUS_PLANNED
            job["planned_at"] = nxt.replace(microsecond=0).isoformat()
            job["note"] = (job.get("note") or "") + "（浏览器被占用，顺延）"
            result = {"ok": False, "units": 0, "status": STATUS_PLANNED,
                      "message": str(exc), "artifacts": []}
        except NeedHuman as exc:
            job["status"] = STATUS_NEEDS_HUMAN
            result = {"ok": False, "units": 0, "status": STATUS_NEEDS_HUMAN,
                      "message": str(exc), "artifacts": []}
            if plan.get("notify_on_needs_human", True):
                self.pause(reason=str(exc))
        except Exception as exc:       # noqa: BLE001
            log.exception("自动化作业异常：%s", job["key"])
            job["status"] = STATUS_FAILED
            result = {"ok": False, "units": 0, "status": STATUS_FAILED,
                      "message": str(exc), "artifacts": []}
        finished = self._now()
        job["finished_at"] = finished.strftime("%Y-%m-%dT%H:%M:%S")
        final_status = result.get("status") or (STATUS_DONE if result.get("ok")
                                               else STATUS_FAILED)
        if job["status"] not in (STATUS_PLANNED, STATUS_NEEDS_HUMAN):
            job["status"] = final_status
        if final_status == STATUS_PLANNED:      # 排队顺延：保留原因，等下一轮
            job["note"] = ((job.get("note") or "") + " "
                           + (result.get("message") or ""))[:200]
        else:
            job["note"] = (result.get("message") or job.get("note") or "")[:200]
        store.append_ledger({
            "day": day, "key": job["key"], "type": job["type"],
            "status": job["status"], "planned_at": job.get("planned_at"),
            "started_at": job["started_at"], "finished_at": job["finished_at"],
            "units": int(result.get("units") or 0),
            "message": result.get("message") or "",
            "artifacts": result.get("artifacts") or [],
            "dry_run": bool(job.get("dry_run")),
        })
        with self._lock:
            self._running_job = None
            self._run_progress = None
        before = self._fails.get(job["type"], 0)
        self._update_failures(job["type"], job["status"], plan)
        # 失败补位：熔断前先试着「当日完成」（用户口径：数量当日完成即可）
        if (job["status"] == STATUS_FAILED and before + 1 < CIRCUIT_BREAK_AFTER
                and planner.plan_retry(self._now(), plan, day_data, job,
                                       RETRY_DELAY_MINUTES)):
            self._push_notice("info", "%s 失败，已安排 %d 分钟后补位一次"
                              % (job["type"], RETRY_DELAY_MINUTES))
        self._save_day_if_changed(day, day_data)
        if job["status"] == STATUS_DONE:
            self._push_notice("info", "%s 完成：%s" % (job["type"],
                              (result.get("message") or "")[:80]))
        elif job["status"] == STATUS_FAILED:
            self._push_notice("warn", "%s 失败：%s" % (job["type"],
                              (result.get("message") or "")[:80]))
        return result

    def _update_failures(self, task_type, status, plan):
        """成功清零；连续失败到阈值 → 熔断该类任务（停用 + 通知）。"""
        if status == STATUS_DONE:
            self._fails[task_type] = 0
            return
        if status not in (STATUS_FAILED, STATUS_NEEDS_HUMAN):
            return
        n = self._fails.get(task_type, 0) + 1
        self._fails[task_type] = n
        if n >= CIRCUIT_BREAK_AFTER:
            from automation.model import task_label
            fresh = store.load_plan()
            cfg = (fresh.get("tasks") or {}).get(task_type) or {}
            if cfg.get("enabled"):
                cfg["enabled"] = False
                store.save_plan(fresh)
            self._fails[task_type] = 0
            self._push_notice("error", "%s 连续失败 %d 次，已自动停用该类任务"
                              % (task_label(task_type), CIRCUIT_BREAK_AFTER))

    # ---------------- 手动干预 ----------------

    def run_now(self, task_type="", dry_run=False):
        """立即执行：把队首（或指定类型的下一个）作业提前到现在。

        dry_run=True → 只演练（当前仅发布草稿支持：不点发布按钮），
        用于用户首次验证链路，避免不可逆动作。
        """
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        plan = store.load_plan()
        day_data = store.load_day(day)
        day_data = planner.materialize_day(now, plan, day_data,
                                           store.done_counts(day))
        target = None
        for job in day_data.get("schedule", []):
            if job.get("status") != STATUS_PLANNED or not job.get("planned_at"):
                continue
            if task_type and job.get("type") != task_type:
                continue
            if target is None or job["planned_at"] < target["planned_at"]:
                target = job
        if target is None and dry_run:
            # 演练要能「先验证再启用」：发布草稿没启用/配额用完时，
            # 也要能造一个一次性演练作业（units=0 → 不占配额，只是个探路者）。
            task_type = task_type or "publish_drafts"
            if task_type != "publish_drafts":
                return {"ok": False, "message": "该任务类型暂不支持演练"}
            seq = len([j for j in day_data.get("schedule", [])
                       if ":rehearsal:" in str(j.get("key") or "")])
            target = {"key": model.job_key(task_type, day, "rehearsal", seq),
                      "type": task_type, "planned_at": "",
                      "status": STATUS_PLANNED, "units": 0,
                      "params": {}, "note": ""}
            day_data.setdefault("schedule", []).append(target)
        if target is None:
            return {"ok": False, "message": "没有可提前执行的作业（配额已用完或该任务未启用）"}
        target["planned_at"] = (now + timedelta(seconds=3)).replace(microsecond=0).isoformat()
        target["manual"] = True          # 手动：时段外也照做（用户明确要求现在执行）
        if dry_run:
            target["dry_run"] = True
            target["note"] = (target.get("note") or "") + "（演练：不会真的发布）"
        else:
            target["note"] = (target.get("note") or "") + "（手动立即执行）"
        self._save_day_if_changed(day, day_data)
        outside = "" if planner.window_open(now, plan) else "（当前不在运行时段，手动执行照做）"
        self._push_notice("info", "已安排立即执行：%s%s%s"
                          % (target["type"], "（演练）" if dry_run else "",
                             outside))
        return {"ok": True, "job": target, "outside_window": bool(outside)}

    # ---------------- 状态（给 UI） ----------------

    def status(self):
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        plan = store.load_plan()
        day_data = store.load_day(day)
        day_data = planner.materialize_day(now, plan, day_data,
                                           store.done_counts(day))
        summary = planner.summarize(now, plan, day_data)
        with self._lock:
            running = dict(self._running_job) if self._running_job else None
            progress = dict(self._run_progress) if self._run_progress else None
            paused = self._paused
            reason = self._pause_reason
            notices = list(self._notices)[-20:]
            fails = dict(self._fails)
        try:
            from webui.browser_tasks import browser_busy
            busy = browser_busy()
        except Exception:              # noqa: BLE001
            busy = []
        return {
            "now": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "plan": plan,
            "enabled": bool(plan.get("enabled")),
            "paused": paused,
            "pause_reason": reason,
            "running": running,
            "progress": progress,
            "summary": summary,
            "schedule": day_data.get("schedule", []),
            "counters": day_data.get("counters") or {},
            "ledger": store.load_ledger(day)[-40:],
            "notices": notices,
            "fails": fails,
            "browser_busy": busy,
            "notes": day_data.get("notes") or [],
        }


_scheduler = None
_scheduler_lock = threading.Lock()


def get_scheduler():
    """进程内单例（webui 侧共用；测试可自行 new 一个注入时钟）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = AutomationScheduler()
        return _scheduler
