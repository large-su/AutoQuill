"""The task watchdog observes bounded browser work, independently of logs."""
import threading
import unittest
from unittest import mock

from webui import run_manager


class _OneTick:
    def __init__(self):
        self.calls = 0

    def wait(self, timeout):
        self.calls += 1
        return self.calls > 1


class GenerationWatchdogTests(unittest.TestCase):
    def control(self, sample):
        return {"id": None, "stop": threading.Event(), "finished": _OneTick(),
                "watchdog_stop": threading.Event(), "timeout": None,
                "generations": {1: sample}, "last_generation_check": sample["at"]}

    def watch(self, control, now):
        with mock.patch("webui.run_manager.time.time", return_value=now), \
                mock.patch.object(run_manager, "_last_log_ts", [0]):
            run_manager.TaskRunner()._watchdog(run_manager._RunSpec(mode="single"), control)

    def test_healthy_long_generation_survives_250_seconds_without_logs(self):
        control = self.control({"state": "thinking", "at": 250, "deadline": 600})
        self.watch(control, 250)
        self.assertIsNone(control["timeout"])
        self.assertFalse(control["stop"].is_set())

    def test_live_browser_does_not_extend_model_deadline(self):
        control = self.control({"state": "generating", "at": 605, "deadline": 600})
        self.watch(control, 605)
        self.assertIsNotNone(control["timeout"])
        self.assertIn("生成等待期限", control["timeout"])

    def test_missing_browser_samples_still_time_out(self):
        control = self.control({"state": "thinking", "at": 1, "deadline": 900})
        self.watch(control, 250)
        self.assertIsNotNone(control["timeout"])
        self.assertTrue(control["stop"].is_set())
