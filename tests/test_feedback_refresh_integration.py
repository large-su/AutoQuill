# -*- coding: utf-8 -*-
"""反馈刷新最小真实桥接：临时台账 + Fake 浏览器，不访问外部账号。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import paths
from webui import published
import applications.zhihu_story.browser_adapter as browser_adapter
from config import story as story_config


class _FakePage:
    url = "https://www.zhihu.com/creator/manage/creation/answer"

    def goto(self, *args, **kwargs):
        return None


class _FakeBrowser:
    payload = []
    counts = []

    def __init__(self, headless=True):
        self.page = _FakePage()
        self._i = 0

    def start(self):
        return None

    def close(self):
        return None

    def _safe_evaluate(self, js):
        if "scrollTo" in js:
            return True
        if "querySelectorAll('.CreationManage-CreationCard').length" in js:
            value = self.counts[min(self._i, len(self.counts) - 1)]
            self._i += 1
            return value
        if "CreationCardTitle-wrapper" in js:
            return list(self.payload)
        return True


class FeedbackRefreshIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aq_feedback_it_")
        self.tmp = Path(self.temporary.name)
        self.old_root = paths.DATA_ROOT
        paths.DATA_ROOT = str(self.tmp)
        self.patches = [
            mock.patch.object(published, "_DATA_DIR", self.tmp / "data"),
            mock.patch("webui.browser_tasks._DASH_REFRESH", {
                "status": "idle", "progress": "", "count": 0, "pct": None,
                "error": "", "need_login": False,
            }),
            mock.patch("core.feedback_loop._perf_path",
                        lambda: self.tmp / "state" / "story_performance.jsonl"),
            mock.patch.object(browser_adapter, "ZhihuBrowser", _FakeBrowser),
            mock.patch.object(browser_adapter, "page_needs_login", return_value=False),
            mock.patch.object(browser_adapter, "_check_cancel"),
            mock.patch.object(published.time, "sleep", lambda seconds: None),
            mock.patch.object(story_config, "FEEDBACK_LOOP_ENABLE", True),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        paths.DATA_ROOT = self.old_root
        self.temporary.cleanup()

    def _refresh(self, payload, counts=(2, 2, 2, 2)):
        _FakeBrowser.payload = payload
        _FakeBrowser.counts = list(counts)
        return published.refresh_feedback(source="automation", refresh_key="it-2026-10-14")

    def test_success_uses_one_stamp_first_wins_and_preserves_missing_vs_zero(self):
        payload = [
            {"aid": "a1", "url": "u1", "title": "one", "publish": "10-01 10:00",
             "metrics": {"阅读": "0", "赞同": "2"}},
            {"aid": "a1", "url": "u1", "title": "duplicate",
             "metrics": {"阅读": "999", "赞同": "99", "评论": "9"}},
            {"aid": "a2", "url": "u2", "title": "two", "publish": "10-02 10:00",
             "metrics": {"阅读": "4", "赞同": "0", "评论": "0"}},
        ]
        result = self._refresh(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["observed_at"])
        state = json.loads((self.tmp / "data" / "state" / "evolution.json").read_text(encoding="utf-8"))
        observations = {aid: row["observations"][-1] for aid, row in state["articles"].items()}
        self.assertEqual(observations["a1"]["likes"], 2)
        self.assertEqual(observations["a1"]["reads"], 0)
        self.assertIsNone(observations["a1"]["comments"])
        self.assertEqual({o["observed_at"] for o in observations.values()}, {result["observed_at"]})
        perf = (self.tmp / "state" / "story_performance.jsonl").read_text(encoding="utf-8").splitlines()
        perf_rows = [json.loads(line) for line in perf]
        self.assertEqual({r["observed"] for r in perf_rows}, {result["observed_at"]})
        self.assertEqual({r["aid"] for r in perf_rows}, {"a1", "a2"})

    def test_cancel_and_incomplete_scroll_keep_old_snapshot_and_observations(self):
        old = [{"aid": "old", "metrics": {"阅读": "8", "赞同": "1"}}]
        snapshot_dir = self.tmp / "data"
        snapshot_dir.mkdir(parents=True)
        old_path = snapshot_dir / "published_answers_2026-10-13.json"
        old_path.write_text(json.dumps(old), encoding="utf-8")
        result = published.refresh_feedback(stop_flag=lambda: True,
                                            source="automation", refresh_key="cancel")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(old_path.read_text(encoding="utf-8"), json.dumps(old))
        self.assertFalse((self.tmp / "data" / "state" / "evolution.json").exists())

        _FakeBrowser.payload = [{"aid": "new", "metrics": {"阅读": "1", "赞同": "1"}}]
        _FakeBrowser.counts = list(range(1, 1001))
        result = published.refresh_feedback(source="automation", refresh_key="incomplete")
        self.assertEqual(result["status"], "error")
        self.assertTrue(old_path.exists())
        state = json.loads((self.tmp / "data" / "state" / "evolution.json").read_text(encoding="utf-8"))
        self.assertEqual(state["articles"], {})
        self.assertTrue(state["refreshes"])

    def test_automation_success_is_idempotent_for_same_key(self):
        payload = [{"aid": "a1", "metrics": {"阅读": "1", "赞同": "1"}}]
        first = self._refresh(payload)
        self.assertTrue(first["ok"])
        with mock.patch.object(published, "scrape", side_effect=AssertionError("must reuse")):
            second = published.refresh_feedback(source="automation", refresh_key="it-2026-10-14")
        self.assertTrue(second["ok"])
        self.assertEqual(second["node_id"], first["node_id"])

    def test_failed_ingest_is_visible_and_does_not_claim_committed_observation(self):
        _FakeBrowser.payload = [{"aid": "new", "metrics": {"阅读": "2", "赞同": "1"}}]
        _FakeBrowser.counts = [1, 1, 1, 1]
        with mock.patch('core.evolution.ingest_snapshot', side_effect=OSError('disk failure')):
            result = published.refresh_feedback(source='manual')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'error')
        state = json.loads((self.tmp / 'data' / 'state' / 'evolution.json').read_text(encoding='utf-8'))
        self.assertEqual(state['refreshes'][-1]['status'], 'error')
        self.assertEqual(state['articles'], {})
        self.assertTrue(Path(result['source_file']).exists())


if __name__ == "__main__":
    unittest.main()
