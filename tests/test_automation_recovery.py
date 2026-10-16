"""Deterministic scheduler recovery coverage (no browser or real automation)."""
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from automation import planner, store
from automation.model import TASK_TYPES
from automation.scheduler import AutomationScheduler, CIRCUIT_BREAK_AFTER
from core import paths


def _plan(task_type="full_chain", cap=1):
    tasks = {name: {"enabled": False, "daily_cap": 0} for name in TASK_TYPES}
    tasks[task_type] = {"enabled": True, "daily_cap": cap}
    return {"enabled": True, "tasks": tasks}


class SchedulerRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_scheduler_recovery_"))
        self.patch = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self.patch.start()
        self.now = datetime(2026, 10, 16, 12, 0, 0)
        store.save_plan(_plan())

    def tearDown(self):
        self.patch.stop()

    def _scheduler(self, result=None):
        result = result or {"ok": False, "status": planner.STATUS_FAILED,
                            "units": 0, "message": "network"}
        return AutomationScheduler(executor=lambda *args, **kwargs: dict(result),
                                   now_fn=lambda: self.now, tick_seconds=3600)

    def test_circuit_is_persistent_visible_and_does_not_change_plan(self):
        scheduler = self._scheduler()
        day = self.now.strftime("%Y-%m-%d")
        day_data = {"date": day, "schedule": [], "counters": {}, "notes": []}
        failed = {"key": "auto:%s:full_chain:0" % day, "type": "full_chain",
                  "params": {}, "units": 1}
        for _ in range(CIRCUIT_BREAK_AFTER):
            scheduler._update_failures("full_chain", planner.STATUS_FAILED,
                                       store.load_plan(), day_data, day, failed)
        self.assertTrue(store.load_plan()["tasks"]["full_chain"]["enabled"])
        persisted = store.load_day(day)["circuit_breakers"]["full_chain"]
        self.assertIn("until", persisted)
        restarted = AutomationScheduler(now_fn=lambda: self.now)
        visible = restarted.status()["circuit_breakers"]["full_chain"]
        self.assertEqual(visible["until"], persisted["until"])
        self.assertEqual(visible["failed_count"], CIRCUIT_BREAK_AFTER)

    def test_cooldown_restores_one_retry_when_cap_one_has_no_pending_job(self):
        scheduler = self._scheduler()
        day = self.now.strftime("%Y-%m-%d")
        original = {"key": "auto:%s:full_chain:0" % day, "type": "full_chain",
                    "status": planner.STATUS_FAILED, "planned_at": self.now.isoformat(),
                    "params": {}, "units": 1}
        data = {"date": day, "schedule": [original], "counters": {}, "notes": [],
                "circuit_breakers": {"full_chain": {
                    "until": (self.now - timedelta(seconds=1)).isoformat(),
                    "last_failed_job": {"key": original["key"], "type": "full_chain",
                                        "params": {}, "units": 1}}}}
        store.save_day(day, data)
        scheduler._expire_circuit_breakers(day, data, self.now, store.load_plan())
        retries = [job for job in data["schedule"]
                   if job["status"] == planner.STATUS_PLANNED and job["type"] == "full_chain"]
        self.assertEqual(len(retries), 1)
        self.assertNotIn("circuit_breakers", data)

    def test_explicit_run_now_clears_only_requested_breaker(self):
        scheduler = self._scheduler()
        day = self.now.strftime("%Y-%m-%d")
        data = {"date": day, "schedule": [], "counters": {}, "notes": [],
                "circuit_breakers": {
                    "full_chain": {"until": (self.now + timedelta(hours=1)).isoformat()},
                    "publish_drafts": {"until": (self.now + timedelta(hours=1)).isoformat()},
                }}
        store.save_day(day, data)
        result = scheduler.run_now("full_chain")
        self.assertTrue(result["ok"])
        breakers = store.load_day(day)["circuit_breakers"]
        self.assertNotIn("full_chain", breakers)
        self.assertIn("publish_drafts", breakers)
        self.now += timedelta(seconds=4)
        scheduler._tick()
        self.assertTrue(any(row.get("type") == "full_chain"
                            for row in store.load_ledger(day)),
                        "解除熔断后 cap=1 也必须真的获得一次执行机会")

    def test_disabled_fixed_task_is_not_reactivated_by_cooldown(self):
        day = self.now.strftime("%Y-%m-%d")
        plan = _plan("feedback_refresh")
        plan["tasks"]["feedback_refresh"]["enabled"] = False
        store.save_plan(plan)
        job = {"key": "auto:%s:feedback_refresh:fixed:0" % day,
               "type": "feedback_refresh", "status": planner.STATUS_FAILED,
               "planned_at": self.now.isoformat(), "params": {}, "units": 0}
        data = {"date": day, "schedule": [job], "counters": {}, "notes": [],
                "circuit_breakers": {"feedback_refresh": {
                    "until": (self.now - timedelta(seconds=1)).isoformat(),
                    "last_failed_job": {"key": job["key"], "type": "feedback_refresh"}}}}
        scheduler = self._scheduler()
        scheduler._expire_circuit_breakers(day, data, self.now, store.load_plan())
        self.assertEqual(job["status"], planner.STATUS_FAILED)

    def test_stop_start_waits_for_old_worker_then_restarts_once(self):
        day = self.now.strftime("%Y-%m-%d")
        started, release = threading.Event(), threading.Event()
        calls = []

        def blocking_executor(job, **kwargs):
            calls.append(job["key"])
            started.set()
            release.wait(1)
            return {"ok": False, "status": planner.STATUS_SKIPPED,
                    "units": 0, "message": "stopped"}

        scheduler = AutomationScheduler(executor=blocking_executor,
                                        now_fn=lambda: self.now, tick_seconds=0.01)
        self.assertTrue(scheduler.run_now("full_chain")["ok"])
        self.now += timedelta(seconds=4)
        scheduler._ensure_thread()
        self.assertTrue(started.wait(1))
        old = scheduler._thread
        scheduler.stop()
        scheduler.start()
        self.assertIs(scheduler._thread, old, "旧作业未退出前不得并行启动第二个调度器")
        release.set()
        old.join(1)
        deadline = time.time() + 1
        while time.time() < deadline and scheduler._thread is old:
            time.sleep(0.01)
        self.assertIsNot(scheduler._thread, old)
        self.assertEqual(len(calls), 1)
        restarted = scheduler._thread
        scheduler.stop()
        if restarted is not None:
            restarted.join(1)

    def test_stop_without_restart_does_not_resurrect_thread(self):
        scheduler = AutomationScheduler(now_fn=lambda: self.now, tick_seconds=0.01)
        scheduler._ensure_thread()
        old = scheduler._thread
        scheduler.stop()
        old.join(1)
        self.assertFalse(old.is_alive())
        self.assertIsNone(scheduler._thread)

    def test_user_cancellation_is_skipped_without_failure_or_retry(self):
        scheduler = self._scheduler({"ok": False, "status": planner.STATUS_SKIPPED,
                                     "units": 0, "message": "用户停止"})
        day = self.now.strftime("%Y-%m-%d")
        job = {"key": "auto:%s:full_chain:0" % day, "type": "full_chain",
               "status": planner.STATUS_PLANNED, "planned_at": self.now.isoformat(),
               "params": {}, "units": 1}
        data = {"date": day, "schedule": [job], "counters": {}, "notes": []}
        scheduler._run_job(job, day, data, store.load_plan())
        self.assertEqual(job["status"], planner.STATUS_SKIPPED)
        self.assertEqual(scheduler._fails.get("full_chain", 0), 0)
        self.assertEqual(len(data["schedule"]), 1)

    def test_deferred_manual_thread_restart_works_with_plan_disabled(self):
        scheduler = self._scheduler()
        scheduler.stop()
        scheduler._thread = mock.Mock()
        scheduler._thread.is_alive.return_value = True
        self.assertFalse(scheduler._ensure_thread())
        self.assertTrue(scheduler._restart_requested)
        with mock.patch.object(scheduler, "_start_thread_locked") as restart:
            scheduler._loop()
        restart.assert_called_once()
        self.assertFalse(store.load_plan()["enabled"])

    def test_stop_revokes_a_pending_restart_request(self):
        scheduler = self._scheduler()
        scheduler._stop_event.set()
        scheduler._restart_requested = True
        scheduler.stop()
        with mock.patch.object(scheduler, "_start_thread_locked") as restart:
            scheduler._loop()
        restart.assert_not_called()

    def test_malformed_cooling_metadata_cannot_block_status_or_ticks(self):
        scheduler = self._scheduler()
        day = self.now.strftime("%Y-%m-%d")
        for malformed in (["bad"], {"full_chain": "bad"},
                          {"full_chain": {"until": "bad", "last_failed_job": []}}):
            data = store.load_day(day)
            data["circuit_breakers"] = malformed
            store.save_day(day, data)
            self.assertEqual(scheduler.status()["circuit_breakers"], {})
            scheduler._expire_circuit_breakers(day, data, self.now, store.load_plan())
            self.assertFalse(scheduler._circuit_open("full_chain", data, self.now))

    def test_bad_executor_result_releases_running_state(self):
        scheduler = AutomationScheduler(executor=lambda *args, **kwargs: None,
                                        now_fn=lambda: self.now)
        day = self.now.strftime("%Y-%m-%d")
        job = {"key": "auto:%s:full_chain:0" % day, "type": "full_chain",
               "status": planner.STATUS_PLANNED, "planned_at": self.now.isoformat(),
               "params": {}, "units": 1}
        data = {"date": day, "schedule": [job], "counters": {}, "notes": []}
        result = scheduler._run_job(job, day, data, store.load_plan())
        self.assertEqual(result["status"], planner.STATUS_FAILED)
        self.assertIsNone(scheduler.status()["running"])


if __name__ == "__main__":
    unittest.main()
