# -*- coding: utf-8 -*-
"""反馈刷新包装器：节点、并发和取消语义。"""

import unittest
from datetime import datetime
from unittest import mock

from webui import published


class FeedbackRefreshTest(unittest.TestCase):
    def setUp(self):
        self.node = {"id": "refresh:test"}
        self.start = mock.patch("core.evolution.start_refresh", return_value=self.node)
        self.finish = mock.patch("core.evolution.finish_refresh", side_effect=lambda *a, **kw: dict(self.node, **kw))
        self.start_mock = self.start.start()
        self.finish_mock = self.finish.start()
        def _scrape(**kwargs):
            kwargs["result_meta"].update({
                "observed_at": "2026-10-14T23:00:00+08:00",
                "source_file": "published_answers_2026-10-14.json",
                "count": 1,
            })
            return [{"aid": "1"}]
        self.scrape = mock.patch.object(published, "scrape", side_effect=_scrape)
        self.scrape_mock = self.scrape.start()
        self.clear = mock.patch("webui.browser_tasks.clear_zhihu_login_stale")
        self.clear.start()
        self.find = mock.patch("core.evolution.find_refresh", return_value=None)
        self.find.start()
        self.dash = mock.patch("webui.browser_tasks._DASH_REFRESH", {
            "status": "idle", "progress": "", "count": 0, "pct": None,
            "error": "", "need_login": False,
        })
        self.dash.start()

    def tearDown(self):
        published._FEEDBACK_REFRESH_LOCK.release() if published._FEEDBACK_REFRESH_LOCK.locked() else None
        self.clear.stop()
        self.dash.stop()
        self.scrape.stop()
        self.finish.stop()
        self.start.stop()
        self.find.stop()

    def test_success_creates_and_finishes_one_node(self):
        result = published.refresh_feedback(source="automation", refresh_key="k")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["count"], 1)
        self.start_mock.assert_called_once_with("automation", "k")
        self.finish_mock.assert_called_once()
        self.assertEqual(self.finish_mock.call_args.kwargs["status"], "done")

    def test_automation_reuses_today_success_for_same_key(self):
        existing = {"id": "refresh:old", "source": "automation",
                    "key": "k", "status": "done", "count": 2,
                    "observed_at": datetime.now().astimezone().isoformat(),
                    "source_file": "published_answers_2026-10-14.json"}
        with mock.patch("core.evolution.find_refresh", return_value=existing):
            result = published.refresh_feedback(source="automation", refresh_key="k")
        self.assertTrue(result["ok"])
        self.assertEqual(result["node_id"], "refresh:old")
        self.start_mock.assert_not_called()
        self.scrape_mock.assert_not_called()

    def test_stop_before_browser_does_not_start_node_or_scrape(self):
        result = published.refresh_feedback(stop_flag=lambda: True)
        self.assertEqual(result["status"], "stopped")
        self.start_mock.assert_not_called()
        self.scrape_mock.assert_not_called()

    def test_busy_does_not_create_node(self):
        self.assertTrue(published._FEEDBACK_REFRESH_LOCK.acquire(blocking=False))
        result = published.refresh_feedback()
        self.assertEqual(result["status"], "busy")
        self.start_mock.assert_not_called()
        self.scrape_mock.assert_not_called()
        published._FEEDBACK_REFRESH_LOCK.release()

    def test_scrape_error_finishes_failed_node(self):
        self.scrape_mock.side_effect = RuntimeError("页面失败")
        result = published.refresh_feedback()
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["ok"])
        self.assertEqual(self.finish_mock.call_args.kwargs["status"], "error")

    def test_stopped_during_scrape_is_visible(self):
        stop = mock.Mock(side_effect=[False, True, True])
        self.scrape_mock.side_effect = lambda **kw: (kw["stop_flag"](), [])[1]
        result = published.refresh_feedback(stop_flag=stop)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.finish_mock.call_args.kwargs["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
