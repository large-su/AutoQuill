"""Regression scenarios from the 2026-10-15 installed automation incident."""
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

from automation import executor, model, planner, store
from automation.scheduler import AutomationScheduler
from core import paths
from web_drivers import browser_pool as pool
from webui.run_manager import TaskRunner, _RunSpec


class IncidentReproductionTest(unittest.TestCase):
    def tearDown(self):
        pool.set_cancel_hook(None)

    def test_finished_runner_stop_does_not_cancel_next_automation_job(self):
        stopped = threading.Event()
        stopped.set()
        pool.set_cancel_hook(stopped.is_set)  # v5.4.0 server-wide hook
        page = mock.Mock()
        page.evaluate.return_value = "read-ok"

        def collect(job, **kwargs):
            self.assertEqual(pool.safe_evaluate(page, "() => 'read-ok'"), "read-ok")
            return {"ok": True, "status": "done", "units": 1}

        with mock.patch.dict(executor._HANDLERS, feedback_refresh=collect):
            result = executor.execute({"type": "feedback_refresh"}, should_stop=lambda: False)
        self.assertTrue(result["ok"])

    def test_timeout_cause_survives_worker_unwind(self):
        runner = TaskRunner()
        runner.state = "running"

        def timed_out(spec):
            runner._set_state("timeout", "总时长超 900s")
            runner._stop_flag.set()
            raise pool.WorkflowCancelled("已由用户停止")

        with mock.patch.object(runner, "_dispatch", side_effect=timed_out), \
                mock.patch.object(runner, "_watchdog"), \
                mock.patch("web_drivers.reset_driver"), \
                mock.patch.object(pool, "close_shared_browser"):
            runner._run_task(_RunSpec(mode="select"))
        self.assertEqual(runner.status()["state"], "timeout")
        self.assertIn("900s", runner.status()["message"])

    def test_own_cancel_skips_handler_without_counting_failure(self):
        handler = mock.Mock()
        with mock.patch.dict(executor._HANDLERS, publish_drafts=handler):
            result = executor.execute({"type": "publish_drafts"}, should_stop=lambda: True)
        handler.assert_not_called()
        self.assertEqual(result["status"], planner.STATUS_SKIPPED)
        self.assertEqual(result["units"], 0)

    def test_cancel_during_job_cleans_up_then_next_job_runs(self):
        stopped = threading.Event()
        events = []

        def task(job, **kwargs):
            try:
                if job.get("cancel"):
                    stopped.set()
                pool._check_cancel()
                return {"ok": True, "status": "done", "units": 1}
            finally:
                with pool.session_cleanup():
                    pool._check_cancel()
                    events.append("cleanup")

        with mock.patch.dict(executor._HANDLERS, checkin=task):
            result = executor.execute({"type": "checkin", "cancel": True}, should_stop=stopped.is_set)
            self.assertEqual(result["status"], planner.STATUS_SKIPPED)
            result = executor.execute({"type": "checkin"}, should_stop=lambda: False)
        self.assertTrue(result["ok"])
        self.assertEqual(events, ["cleanup", "cleanup"])

    def test_full_chain_waits_for_cleanup_and_preserves_timeout(self):
        fake_runner = mock.Mock()
        fake_runner.status.side_effect = [
            {"state": "timeout", "message": "总时长超 900s", "worker_alive": True},
            {"state": "timeout", "message": "总时长超 900s", "worker_alive": False},
        ]
        with mock.patch("webui.run_manager.runner", fake_runner), \
                mock.patch.object(executor, "_browser_busy", return_value=[]), \
                mock.patch.object(pool, "profile_in_use", return_value=False), \
                mock.patch.object(executor, "_checkin_context", return_value={}), \
                mock.patch.object(executor, "_set_checkin_context"), \
                mock.patch.object(executor.time, "sleep") as sleep:
            result = executor._full_chain({"type": "full_chain"})
        self.assertEqual(fake_runner.status.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(result["status"], planner.STATUS_FAILED)
        self.assertIn("900s", result["message"])

    def test_circuit_does_not_disable_user_plan_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "DATA_ROOT", tmp):
            plan = model.normalize_plan({})
            plan["enabled"] = True
            plan["tasks"]["publish_drafts"]["enabled"] = True
            plan = store.save_plan(plan)
            scheduler = AutomationScheduler(now_fn=lambda: datetime(2026, 10, 15, 13))
            for _ in range(3):
                scheduler._update_failures("publish_drafts", planner.STATUS_FAILED, plan)
            self.assertTrue(store.load_plan()["tasks"]["publish_drafts"]["enabled"])


if __name__ == "__main__":
    unittest.main()
