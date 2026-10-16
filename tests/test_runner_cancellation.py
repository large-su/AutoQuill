"""Deterministic cancellation/lifecycle regressions for the web TaskRunner."""

import threading
import unittest
from unittest import mock

from web_drivers import browser_pool
from webui import run_manager
from webui.run_manager import TaskRunner, _RunSpec


class CancellationScopeTest(unittest.TestCase):
    def tearDown(self):
        browser_pool.set_cancel_hook(None)

    def test_scope_is_thread_local_and_none_disables_legacy_hook(self):
        browser_pool.set_cancel_hook(lambda: True)
        with browser_pool.cancellation_scope(None):
            browser_pool._check_cancel()

        cancelled = []

        def other_thread():
            try:
                browser_pool._check_cancel()
            except browser_pool.WorkflowCancelled:
                cancelled.append(True)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled, [True])

    def test_scope_checker_overrides_legacy_hook(self):
        browser_pool.set_cancel_hook(lambda: True)
        with browser_pool.cancellation_scope(lambda: False):
            browser_pool._check_cancel()

    def test_concurrent_scopes_do_not_share_cancellation_state(self):
        outcomes, ready, release = [], threading.Event(), threading.Event()

        def cancelled_worker():
            with browser_pool.cancellation_scope(lambda: True):
                ready.set()
                release.wait(2)
                with self.assertRaises(browser_pool.WorkflowCancelled):
                    browser_pool._check_cancel()
                outcomes.append("cancelled")

        thread = threading.Thread(target=cancelled_worker)
        thread.start()
        self.assertTrue(ready.wait(2))
        with browser_pool.cancellation_scope(None):
            browser_pool._check_cancel()
            outcomes.append("allowed")
        release.set()
        thread.join(2)
        self.assertEqual(sorted(outcomes), ["allowed", "cancelled"])


class TaskRunnerCancellationTest(unittest.TestCase):
    def _cleanup_patches(self):
        return (mock.patch("web_drivers.reset_driver"),
                mock.patch.object(browser_pool, "close_shared_browser"))

    def test_false_dispatch_is_error_not_done(self):
        runner = TaskRunner()
        runner._set_state("running", "test")
        reset, close = self._cleanup_patches()
        with mock.patch.object(runner, "_dispatch", return_value=False), reset, close:
            runner._run_task(_RunSpec(mode="select"))
        self.assertEqual(runner.status()["state"], "error")
        self.assertIn("未成功", runner.status()["message"])

    def test_preflight_failure_reaches_terminal_error(self):
        runner = TaskRunner()
        runner._set_state("running", "test")
        reset, close = self._cleanup_patches()
        with mock.patch("webui.run_manager._llm_configured", return_value=False), \
                mock.patch("config.LLM_MODE", "api"), reset, close:
            runner._run_task(_RunSpec(mode="generate"))
        status = runner.status()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["guide_needed"], "api_key")

    def test_terminal_state_waits_for_owned_cleanup(self):
        runner = TaskRunner()
        runner._set_state("running", "test")
        seen = []

        def during_cleanup(*args, **kwargs):
            seen.append(runner.status()["state"])

        with mock.patch.object(runner, "_dispatch", return_value=True), \
                mock.patch("web_drivers.reset_driver", side_effect=during_cleanup), \
                mock.patch.object(browser_pool, "close_shared_browser"):
            runner._run_task(_RunSpec(mode="select"))
        self.assertEqual(seen, ["running"])
        self.assertEqual(runner.status()["state"], "done")

    def test_status_reports_live_worker_until_cleanup_finishes(self):
        runner = TaskRunner()
        entered, release = threading.Event(), threading.Event()

        def dispatch(spec):
            entered.set()
            self.assertTrue(release.wait(2))
            return True

        with mock.patch.object(runner, "_dispatch", side_effect=dispatch), \
                mock.patch("web_drivers.reset_driver"), \
                mock.patch.object(browser_pool, "close_shared_browser"):
            runner.start(_RunSpec(mode="select"))
            self.assertTrue(entered.wait(2))
            self.assertTrue(runner.status()["worker_alive"])
            release.set()
            runner._thread.join(2)
        self.assertFalse(runner.status()["worker_alive"])
        self.assertEqual(runner.status()["state"], "done")

    def test_finished_old_watchdog_cannot_cancel_new_run_event(self):
        runner = TaskRunner()
        old_control = {"id": 1, "stop": threading.Event(),
                       "finished": threading.Event(), "timeout": None}
        old_control["finished"].set()
        new_stop = threading.Event()
        runner._active_control = {"id": 2, "stop": new_stop,
                                  "finished": threading.Event(), "timeout": None}
        runner._watchdog(_RunSpec(mode="select"), old_control)
        self.assertFalse(new_stop.is_set())
        self.assertFalse(old_control["stop"].is_set())

    def test_active_old_watchdog_exits_when_a_new_run_becomes_active(self):
        class NoWait:
            def wait(self, timeout):
                return False

        runner = TaskRunner()
        old_control = {"id": 1, "stop": threading.Event(),
                       "finished": NoWait(), "watchdog_stop": threading.Event(),
                       "timeout": None}
        new_control = {"id": 2, "stop": threading.Event(),
                       "finished": threading.Event(), "watchdog_stop": threading.Event(),
                       "timeout": None}
        runner._active_control = new_control
        runner._watchdog(_RunSpec(mode="select"), old_control)
        self.assertFalse(old_control["stop"].is_set())
        self.assertFalse(new_control["stop"].is_set())

    def test_watchdog_timeout_is_terminal_and_has_timeout_reason(self):
        class NoWait:
            def wait(self, timeout):
                return False

            def set(self):
                return None

        runner = TaskRunner()
        control = {"id": None, "stop": threading.Event(), "finished": NoWait(),
                   "watchdog_stop": threading.Event(), "timeout": None}
        runner._set_state("running", "test")
        run_manager._last_log_ts[0] = 0
        with mock.patch("webui.run_manager.STALL_LIMIT", 0), \
                mock.patch("webui.run_manager.time.time", return_value=1):
            runner._watchdog(_RunSpec(mode="select"), control)
        self.assertIn("无进展", control["timeout"])

        def dispatch(spec):
            browser_pool._check_cancel()

        with mock.patch.object(runner, "_dispatch", side_effect=dispatch), \
                mock.patch("web_drivers.reset_driver"), \
                mock.patch.object(browser_pool, "close_shared_browser"):
            runner._run_task(_RunSpec(mode="select"), control)
        self.assertEqual(runner.status()["state"], "timeout")
        self.assertIn("无进展", runner.status()["message"])

    def test_late_stop_after_confirmed_dispatch_keeps_done(self):
        runner = TaskRunner()
        runner._set_state("running", "test")

        def dispatch(spec):
            runner._stop_flag.set()
            return True

        with mock.patch.object(runner, "_dispatch", side_effect=dispatch), \
                mock.patch("web_drivers.reset_driver"), \
                mock.patch.object(browser_pool, "close_shared_browser"):
            runner._run_task(_RunSpec(mode="select"))
        self.assertEqual(runner.status()["state"], "done")


if __name__ == "__main__":
    unittest.main()
