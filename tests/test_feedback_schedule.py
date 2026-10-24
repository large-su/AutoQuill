# -*- coding: utf-8 -*-
"""每日反馈刷新排班契约（纯逻辑，不访问站点或浏览器）。"""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from core import paths
from automation import planner, store
from automation.executor import BrowserBusy
from automation.model import TASK_TYPES, normalize_plan
from automation.scheduler import AutomationScheduler


def _feedback_plan(**feedback):
    tasks = {name: {"enabled": False, "daily_cap": 0} for name in TASK_TYPES}
    tasks["feedback_refresh"] = dict({"enabled": True}, **feedback)
    return normalize_plan({"enabled": True,
                           "window": {"start": "08:00", "end": "22:00"},
                           "tasks": tasks})


class FeedbackScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_feedback_schedule_"))
        self.patch_root = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self.patch_root.start()
        self.now = datetime(2026, 10, 14, 12, 0)
        self.calls = []

        def fake_execute(job, should_stop=None, progress=None):
            self.calls.append(dict(job))
            return {"ok": True, "units": 1, "status": planner.STATUS_DONE,
                    "message": "快照已保存", "artifacts": []}

        self.scheduler = AutomationScheduler(executor=fake_execute,
                                             now_fn=lambda: self.now)

    def tearDown(self):
        self.scheduler.stop(cancel_running=False)
        self.patch_root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _day(self, plan=None):
        plan = plan or store.load_plan()
        return planner.materialize_day(self.now, plan,
                                       store.load_day(self.now.strftime("%Y-%m-%d")),
                                       store.done_counts(self.now.strftime("%Y-%m-%d")))

    def _fixed(self, day):
        rows = [j for j in day["schedule"] if j["type"] == "feedback_refresh"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _save_interrupted_feedback(self, plan, *, started_at="2026-10-14T11:55:00"):
        """预置上个进程留下的当日 running 固定作业。"""
        day = planner.materialize_day(self.now, plan, {}, {})
        job = self._fixed(day)
        job["status"] = planner.STATUS_RUNNING
        job["started_at"] = started_at
        store.save_day(self.now.strftime("%Y-%m-%d"), day)
        return job["key"]

    def test_default_is_enabled_fixed_at_and_legacy_disable_is_preserved(self):
        default = normalize_plan({})["tasks"]["feedback_refresh"]
        self.assertTrue(default["enabled"])
        self.assertEqual(default["at"], "23:00")
        self.assertEqual(default["window"], {"start": "23:00", "end": "23:59"})
        old = normalize_plan({"tasks": {"feedback_refresh": {
            "enabled": False, "at": "7:05"}}})
        self.assertFalse(old["tasks"]["feedback_refresh"]["enabled"])
        self.assertEqual(old["tasks"]["feedback_refresh"]["at"], "07:05")

    def test_fixed_time_never_moves_for_collision_or_global_window(self):
        plan = _feedback_plan(at="23:00")
        # 同时添加一条普通任务，固定刷新也不能因避让被挪到别处。
        plan["tasks"]["full_chain"] = {"enabled": True, "daily_cap": 1,
                                             "min_gap_minutes": None,
                                             "window": {"start": "22:50", "end": "23:30"},
                                             "params": {"mode": "single", "rounds": 1}}
        day = planner.materialize_day(self.now, plan, {}, {})
        job = self._fixed(day)
        self.assertEqual(job["key"], "auto:2026-10-14:feedback_refresh:fixed:0")
        self.assertEqual(job["planned_at"], "2026-10-14T23:00:00")
        self.assertEqual(job["status"], planner.STATUS_PLANNED)

    def test_fixed_job_is_independent_from_writing_quota_and_no_duplicate_after_restart(self):
        plan = _feedback_plan()
        day = planner.materialize_day(self.now, plan, {}, {"full_chain": 99})
        job = self._fixed(day)
        day["schedule"] = [dict(job, status=planner.STATUS_DONE)]
        again = planner.materialize_day(self.now, plan, day, {"feedback_refresh": 1})
        self.assertEqual(len([j for j in again["schedule"]
                              if j["type"] == "feedback_refresh"]), 1)
        self.assertEqual(self._fixed(again)["status"], planner.STATUS_DONE)

    def test_change_at_replans_pending_but_retains_failed_responsibility(self):
        plan = _feedback_plan(at="23:00")
        day = planner.materialize_day(self.now, plan, {}, {})
        changed = _feedback_plan(at="21:30")
        rescheduled = planner.materialize_day(self.now, changed, day, {})
        self.assertEqual(self._fixed(rescheduled)["planned_at"], "2026-10-14T21:30:00")
        failed = self._fixed(rescheduled)
        failed["status"] = planner.STATUS_FAILED
        later = _feedback_plan(at="20:00")
        retained = planner.materialize_day(self.now, later, rescheduled, {})
        self.assertEqual(self._fixed(retained)["status"], planner.STATUS_FAILED)
        self.assertEqual(self._fixed(retained)["planned_at"], "2026-10-14T21:30:00")

    def test_same_day_catch_up_never_backfills_an_old_day(self):
        plan = _feedback_plan(at="20:00")
        day = planner.materialize_day(self.now, plan, {}, {})
        late = datetime(2026, 10, 14, 22, 30)
        caught = planner.apply_catch_up(late, plan, day)
        job = self._fixed(caught)
        self.assertEqual(job["status"], planner.STATUS_PLANNED)
        self.assertGreaterEqual(datetime.fromisoformat(job["planned_at"]), late)
        self.assertIn("当日内补做", job["note"])
        next_day = planner.materialize_day(datetime(2026, 10, 15, 8, 0), plan, caught, {})
        next_job = self._fixed(next_day)
        self.assertEqual(next_job["key"], "auto:2026-10-15:feedback_refresh:fixed:0")
        self.assertEqual(next_job["planned_at"], "2026-10-15T20:00:00")

    def test_none_and_next_window_mark_missed_fixed_job_skipped(self):
        for policy in ("none", "next_window"):
            plan = _feedback_plan(at="20:00")
            plan["catch_up"] = policy
            day = planner.materialize_day(self.now, plan, {}, {})
            result = planner.apply_catch_up(datetime(2026, 10, 14, 22, 0), plan, day)
            self.assertEqual(self._fixed(result)["status"], planner.STATUS_SKIPPED)

    def test_window_outside_runs_fixed_task_and_close_out_does_not_skip_it(self):
        plan = _feedback_plan(at="23:00")
        store.save_plan(plan)
        self.now = datetime(2026, 10, 14, 23, 0)
        self.scheduler._tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["type"], "feedback_refresh")
        job = self._fixed(store.load_day("2026-10-14"))
        self.assertEqual(job["status"], planner.STATUS_DONE)

    def test_paused_and_stopped_leave_fixed_job_visible_then_retry_stays_same_job(self):
        plan = _feedback_plan(at="20:00")
        store.save_plan(plan)
        self.now = datetime(2026, 10, 14, 20, 0)
        # 暂停时 tick 不会新建排班；先落盘既有责任，验证它不会被暂停吞掉。
        store.save_day("2026-10-14", planner.materialize_day(self.now, plan, {}, {}))
        self.scheduler.pause("测试")
        self.scheduler._tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self._fixed(store.load_day("2026-10-14"))["status"], planner.STATUS_PLANNED)
        self.scheduler.resume()
        # 停止也不吞掉责任：停止期间不自动执行，重新开启后仍是当天同一条固定作业。
        self.scheduler.stop(cancel_running=False)
        self.scheduler._tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self._fixed(store.load_day("2026-10-14"))["status"], planner.STATUS_PLANNED)
        # Use the actual restart entry point: flipping plan.enabled alone does
        # not revoke a previous stop request. Only mock physical thread startup.
        with mock.patch("automation.scheduler.threading.Thread"):
            self.scheduler.start()
        self.scheduler._execute = lambda *a, **k: {"ok": False, "units": 0,
                                                    "status": planner.STATUS_FAILED,
                                                    "message": "网络失败", "artifacts": []}
        self.scheduler._tick()
        job = self._fixed(store.load_day("2026-10-14"))
        self.assertEqual(job["status"], planner.STATUS_PLANNED)
        self.assertIn("稍后重试", job["note"])
        self.assertEqual(job["key"], "auto:2026-10-14:feedback_refresh:fixed:0")

    def test_midnight_retry_is_not_scheduled_into_tomorrow(self):
        plan = _feedback_plan(at="23:00")
        day = planner.materialize_day(self.now, plan, {}, {})
        job = self._fixed(day)
        job["status"] = planner.STATUS_FAILED
        self.assertFalse(planner.plan_retry(datetime(2026, 10, 14, 23, 55),
                                            plan, day, job, delay_minutes=20))
        self.assertEqual(job["status"], planner.STATUS_FAILED)

    def test_browser_busy_at_midnight_marks_fixed_observation_skipped(self):
        """23:58 忙到 00:03 时不把 10-14 的反馈观测挪到 10-15。"""
        plan = _feedback_plan(at="23:58")
        store.save_plan(plan)
        self.now = datetime(2026, 10, 14, 23, 58)
        self.scheduler._execute = mock.Mock(side_effect=BrowserBusy("手动浏览器任务"))
        self.scheduler._tick()
        job = self._fixed(store.load_day("2026-10-14"))
        self.assertEqual(job["status"], planner.STATUS_SKIPPED)
        self.assertIn("当日未采集", job["note"])
        self.assertEqual(job["planned_at"], "2026-10-14T23:58:00")
        self.scheduler._execute.assert_called_once()

    def test_fresh_scheduler_recovers_interrupted_feedback_once_with_same_key(self):
        plan = _feedback_plan(at="23:00")
        store.save_plan(plan)
        key = self._save_interrupted_feedback(plan)
        calls = []

        def fake_execute(job, **_kwargs):
            calls.append(dict(job))
            return {"ok": True, "units": 1, "status": planner.STATUS_DONE,
                    "message": "已确认原快照", "artifacts": []}

        fresh = AutomationScheduler(executor=fake_execute, now_fn=lambda: self.now)
        fresh._tick()  # 先恢复为 now+3s，不能在同一 tick 立刻重复启动
        recovered = self._fixed(store.load_day("2026-10-14"))
        self.assertEqual(recovered["status"], planner.STATUS_PLANNED)
        self.assertEqual(recovered["planned_at"], "2026-10-14T12:00:03")
        self.assertEqual(recovered["original_planned_at"], "2026-10-14T23:00:00")
        self.assertEqual(recovered["started_at"], "2026-10-14T11:55:00")
        self.assertEqual(calls, [])
        self.now += timedelta(seconds=4)
        fresh._tick()
        finished = self._fixed(store.load_day("2026-10-14"))
        self.assertEqual(finished["status"], planner.STATUS_DONE)
        self.assertEqual(calls[0]["key"], key)
        self.assertEqual(len(calls), 1)

    def test_interrupted_feedback_stays_visible_when_disabled_or_live(self):
        plan = _feedback_plan()
        store.save_plan(plan)
        self._save_interrupted_feedback(plan)
        stopped = store.load_plan()
        stopped["enabled"] = False
        store.save_plan(stopped)
        self.scheduler._tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self._fixed(store.load_day("2026-10-14"))["status"], planner.STATUS_RUNNING)

        enabled = _feedback_plan()
        store.save_plan(enabled)
        self.scheduler._running_job = {"key": "live"}
        self.scheduler._tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self._fixed(store.load_day("2026-10-14"))["status"], planner.STATUS_RUNNING)
        self.scheduler._running_job = None

        disabled_task = _feedback_plan(enabled=False)
        store.save_plan(disabled_task)
        self.scheduler._tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self._fixed(store.load_day("2026-10-14"))["status"], planner.STATUS_RUNNING)

    def test_interrupted_feedback_at_midnight_is_marked_skipped(self):
        plan = _feedback_plan(at="23:00")
        store.save_plan(plan)
        self._save_interrupted_feedback(plan)
        self.now = datetime(2026, 10, 14, 23, 59, 58)
        fresh = AutomationScheduler(executor=lambda *_a, **_k: self.fail("must not execute"),
                                    now_fn=lambda: self.now)
        fresh._tick()
        job = self._fixed(store.load_day("2026-10-14"))
        self.assertEqual(job["status"], planner.STATUS_SKIPPED)
        self.assertIn("已过午夜", job["note"])

    def test_single_axis_payload_has_fixed_time_and_mode(self):
        from automation.scheduler import _single_axis_payload
        plan = _feedback_plan(at="21:15")
        day = planner.materialize_day(self.now, plan, {}, {})
        item = _single_axis_payload("2026-10-14", day, plan)["axis"][0]
        self.assertEqual(item["job_mode"], "fixed_daily")
        self.assertEqual(item["at"], "21:15")

