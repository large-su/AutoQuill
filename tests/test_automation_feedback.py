# -*- coding: utf-8 -*-
"""已发布内容反馈刷新自动化处理器的状态与取消语义。"""

import sys
import types
import unittest
from unittest import mock

from automation import planner
from automation.executor import BrowserBusy, NeedHuman, execute


class FeedbackRefreshExecutorTest(unittest.TestCase):
    def setUp(self):
        self.busy = mock.patch("automation.executor._browser_busy", return_value=[])
        self.busy.start()
        self.profile = mock.patch("web_drivers.browser_pool.profile_in_use", return_value=False)
        self.profile.start()
        self.refresh = mock.Mock(return_value={
            "ok": True, "status": "done", "count": 3,
            "observed_at": "2026-10-14T23:00:00+08:00",
            "source_file": "data/feedback.jsonl", "node_id": "v5.3.1",
        })
        self.published = types.ModuleType("webui.published")
        self.published.refresh_feedback = self.refresh
        self.modules = mock.patch.dict(sys.modules, {"webui.published": self.published})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.profile.stop()
        self.busy.stop()

    def _job(self):
        return {"type": "feedback_refresh", "key": "feedback-20261014"}

    def test_done_calls_refresh_once_and_records_snapshot_metadata(self):
        updates = []
        result = execute(self._job(), progress=updates.append)

        self.assertEqual(result["status"], planner.STATUS_DONE)
        self.assertEqual(result["units"], 1)
        self.assertEqual(self.refresh.call_count, 1)
        kwargs = self.refresh.call_args.kwargs
        self.assertEqual(kwargs["source"], "automation")
        self.assertEqual(kwargs["refresh_key"], "feedback-20261014")
        self.assertEqual(result["artifacts"], [
            "snapshot:data/feedback.jsonl",
            "observed_at:2026-10-14T23:00:00+08:00",
            "node_id:v5.3.1",
        ])

    def test_empty_or_bad_result_is_failed_and_not_counted(self):
        for value in ({}, {"ok": True, "status": "done", "count": 0},
                      {"ok": False, "status": "error", "count": 9}):
            with self.subTest(value=value):
                self.refresh.reset_mock()
                self.refresh.return_value = value
                result = execute(self._job())
                self.assertEqual(result["status"], planner.STATUS_FAILED)
                self.assertEqual(result["units"], 0)

    def test_login_is_escalated_to_need_human(self):
        self.refresh.return_value = {"ok": False, "status": "error",
                                     "need_login": True, "message": "请登录"}
        with self.assertRaises(NeedHuman):
            execute(self._job())

    def test_busy_profile_or_refresh_result_does_not_call_refresh(self):
        with mock.patch("automation.executor._browser_busy", return_value=["手动任务"]):
            with self.assertRaises(BrowserBusy):
                execute(self._job())
        self.refresh.assert_not_called()
        with mock.patch("web_drivers.browser_pool.profile_in_use", return_value=True):
            with self.assertRaises(BrowserBusy):
                execute(self._job())
        self.refresh.assert_not_called()
        self.refresh.return_value = {"ok": False, "status": "busy", "message": "占用"}
        with self.assertRaises(BrowserBusy):
            execute(self._job())

    def test_cancel_before_and_during_refresh_are_skipped(self):
        stop = mock.Mock(return_value=True)
        result = execute(self._job(), should_stop=stop)
        self.assertEqual(result["status"], planner.STATUS_SKIPPED)
        self.refresh.assert_not_called()

        stop.reset_mock(side_effect=None)
        stop.side_effect = [False, True]
        self.refresh.return_value = {"ok": False, "status": "stopped", "count": 0}
        result = execute(self._job(), should_stop=stop)
        self.assertEqual(result["status"], planner.STATUS_SKIPPED)
        self.assertEqual(result["units"], 0)

    def test_stop_after_committed_refresh_preserves_the_completed_result(self):
        stop = mock.Mock(side_effect=[False, True])
        result = execute(self._job(), should_stop=stop)
        self.assertEqual(result['status'], planner.STATUS_DONE)
        self.assertEqual(result['units'], 1)

    def test_progress_is_wrapped_for_scheduler(self):
        updates = []
        self.refresh.side_effect = lambda **kw: (kw["progress"]("抓取中", 60), {
            "ok": True, "status": "done", "count": 1})[1]
        execute(self._job(), progress=updates.append)
        self.assertEqual(updates, [{"progress": {"text": "抓取中", "pct": 60}}])


if __name__ == "__main__":
    unittest.main()
