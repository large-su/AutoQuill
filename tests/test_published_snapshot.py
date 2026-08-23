# -*- coding: utf-8 -*-
"""已发布内容快照层单测：双格式兼容 / 好快照回退 / 筛选 / 抓取质量防护。"""
import json
import tempfile
import types
import unittest
from pathlib import Path

from webui import published


def _raw_rows():
    return [
        {"aid": "1", "url": "u1", "title": "甲", "publish": "2026-08-20 10:00",
         "content": "内容" * 20, "metrics": {"阅读": "300", "赞同": "20",
                                            "评论": "3", "收藏": "2", "喜欢": "0"}},
        {"aid": "2", "url": "u2", "title": "乙", "publish": "2026-08-22 10:00",
         "content": "短", "metrics": {"阅读": "400", "赞同": "30",
                                       "评论": "4", "收藏": "1", "喜欢": "0"}},
    ]


class PublishedSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_pub_t_"))
        published._DATA_DIR = self.tmp
        self._orig_sleep = published.time.sleep
        published.time.sleep = lambda s: None

    def tearDown(self):
        published.time.sleep = self._orig_sleep

    def _seed(self, name, rows):
        (self.tmp / name).write_text(json.dumps(rows), encoding="utf-8")

    def test_coerce_raw_and_normalized(self):
        raw = _raw_rows()[0]
        r1 = published._coerce_row(raw)
        self.assertEqual(r1["likes"], 20)
        self.assertEqual(r1["publish_date"], "2026-08-20")
        self.assertIn("genre", r1)
        # 已归一化行（无 metrics）应原样保留并补齐 genre
        r2 = published._coerce_row(dict(r1))
        self.assertEqual(r2["likes"], r1["likes"])
        self.assertIn("genre", r2)

    def test_load_falls_back_to_good_snapshot(self):
        self._seed("published_answers_2026-08-22.json", _raw_rows())
        broken = [dict(r, metrics={}) for r in _raw_rows()]  # 互动全 0
        self._seed("published_answers_2026-08-23.json", broken)
        d = published.load()
        self.assertEqual(d["total"], 2)
        self.assertGreater(sum(r["likes"] for r in d["rows"]), 0)

    def test_filter_and_sort(self):
        self._seed("published_answers_2026-08-22.json", _raw_rows())
        rows = published.load()["rows"]
        fr = published.filter_rows(rows, q="甲")
        self.assertEqual([r["aid"] for r in fr], ["1"])
        fr2 = published.filter_rows(rows, sort="likes")
        self.assertEqual(fr2[0]["aid"], "2")
        fr3 = published.filter_rows(rows, start="2026-08-21")
        self.assertEqual([r["aid"] for r in fr3], ["2"])

    def test_scrape_guard_rejects_broken_metrics(self):
        self._seed("published_answers_2026-08-22.json", _raw_rows())
        # 假浏览器：返回指标全空的卡片
        import applications.zhihu_story.browser_adapter as ba

        class Fake:
            def __init__(self, headless=True):
                self.page = types.SimpleNamespace(goto=lambda *a, **k: None)
                self.payload = "bad"
            def start(self): pass
            def close(self): pass
            def _safe_evaluate(self, js):
                if "scrollTo" in js: return True
                if "querySelectorAll('.CreationManage-CreationCard').length" in js: return 2
                if "CreationCardTitle-wrapper" in js:
                    return [dict(r, metrics={}) for r in _raw_rows()]
                return True

        orig = ba.ZhihuBrowser
        ba.ZhihuBrowser = lambda headless=True: Fake()
        try:
            rows = published.scrape()
            self.assertEqual(rows, [], "坏数据应拒绝落盘")
            self.assertEqual(len(list(self.tmp.glob("published_answers_*.json"))), 1,
                             "不应写入新文件")
        finally:
            ba.ZhihuBrowser = orig

    def test_scrape_writes_good_data(self):
        self._seed("published_answers_2026-08-22.json", _raw_rows())
        import applications.zhihu_story.browser_adapter as ba

        class Fake:
            def __init__(self, headless=True):
                self.page = types.SimpleNamespace(goto=lambda *a, **k: None)
            def start(self): pass
            def close(self): pass
            def _safe_evaluate(self, js):
                if "scrollTo" in js: return True
                if "querySelectorAll('.CreationManage-CreationCard').length" in js: return 2
                if "CreationCardTitle-wrapper" in js: return _raw_rows()
                return True

        orig = ba.ZhihuBrowser
        ba.ZhihuBrowser = lambda headless=True: Fake()
        try:
            rows = published.scrape()
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(list(self.tmp.glob("published_answers_*.json"))), 2)
        finally:
            ba.ZhihuBrowser = orig


if __name__ == "__main__":
    unittest.main()
