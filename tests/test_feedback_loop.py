# -*- coding: utf-8 -*-
"""core/feedback_loop + topic_ledger 元数据 + 题材分类 + 选题加权 单测。

全部使用临时目录隔离（不触碰真实 data/state），并重置反馈闭环缓存。
"""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import core.feedback_loop as fb
import core.topic_ledger as tl
import core.version as ver
from core.detectors import classify_genre


def _reset_cache():
    fb._cache.update(mtime_ns=None, at=0.0, data=None)


class FeedbackLoopTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.perf = pathlib.Path(self.tmp.name) / "story_performance.jsonl"
        self.ledger = pathlib.Path(self.tmp.name) / "published_topics.jsonl"
        self._p1 = mock.patch.object(fb, "_perf_path",
                                     lambda: self.perf)
        self._p2 = mock.patch.object(tl, "_ledger_path",
                                     lambda: self.ledger)
        # 自动回填的默认目录指向临时目录（保留真实实现，仅隔离数据源，
        # 防止 summarize 自动回填扫到仓库真实 data/ 快照）
        _real_seed = fb.seed_from_snapshots

        def _fake_seed(data_dir=None, verbose=False):
            return _real_seed(
                data_dir or str(pathlib.Path(self.tmp.name) / "data"),
                verbose=verbose)

        self._p3 = mock.patch.object(fb, "seed_from_snapshots", _fake_seed)
        self._p1.start()
        self._p2.start()
        self._p3.start()
        _reset_cache()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self._p3.stop()
        self.tmp.cleanup()

    def _write_perf(self, recs):
        with open(self.perf, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------------- 发布落账 ----------------

    def test_record_story_published_adds_version_and_meta(self):
        fb.record_story_published(
            "https://www.zhihu.com/question/1",
            "有没有甜甜的小说推荐？",
            {"story_file": "output/story_x.md", "genre": "甜文",
             "session_id": "s1"})
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["url"], "https://www.zhihu.com/question/1")
        self.assertEqual(rec["story_file"], "output/story_x.md")
        self.assertEqual(rec["genre"], "甜文")
        self.assertEqual(rec["session_id"], "s1")
        self.assertEqual(rec["version"], ver.VERSION)
        self.assertIn("date", rec)

    def test_record_story_published_backward_compatible(self):
        # 老调用（2 参）仍可写
        tl.record("https://www.zhihu.com/question/2", "标题")
        rec = json.loads(self.ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["title"], "标题")
        self.assertEqual(rec["version"], ver.VERSION)

    def test_record_without_url_is_noop(self):
        self.assertIsNone(tl.record(""))

    # ---------------- 表现观测 ----------------

    def test_attach_performance_appends_and_is_idempotent(self):
        ok = fb.attach_performance(
            "https://www.zhihu.com/answer/9", likes=3, reads=120,
            comments=1, collects=2, favors=0, aid="9",
            title="有没有甜文", publish_date="2026-08-27")
        self.assertTrue(ok)
        self.assertFalse(fb.attach_performance(""))
        fb.attach_performance("https://www.zhihu.com/answer/9",
                              likes=3, reads=120, comments=1, collects=2,
                              favors=0, aid="9", publish_date="2026-08-27")
        # 同指标同日不重复；不同观测日允许追加（时间序列）
        fb.attach_performance("https://www.zhihu.com/answer/9",
                              likes=5, reads=200, comments=2, collects=2,
                              favors=0, aid="9", publish_date="2026-08-27",
                              observed="2026-08-30")
        lines = self.perf.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    # ---------------- 快照回填 ----------------

    def test_seed_from_snapshots_both_schemas(self):
        d = pathlib.Path(self.tmp.name) / "snap"
        d.mkdir()
        # 老格式（metrics 字典）
        (d / "published_answers_2026-08-22.json").write_text(
            json.dumps([{
                "aid": "1", "url": "https://www.zhihu.com/answer/1",
                "title": "有什么好看的双男主文？", "publish": "08-20 10:00",
                "metrics": {"阅读": "88", "赞同": "2", "评论": "0",
                            "收藏": "1", "喜欢": "0"},
            }], ensure_ascii=False), encoding="utf-8")
        # 新格式（扁平字段）
        (d / "published_answers_2026-08-30.json").write_text(
            json.dumps([{
                "aid": "2", "url": "https://www.zhihu.com/answer/2",
                "title": "有没有甜甜的小说推荐？",
                "publish_date": "2026-08-29", "publish": "08-29 12:00",
                "reads": 63, "likes": 3, "comments": 0, "collects": 1,
                "favors": 0, "genre": "甜文",
            }], ensure_ascii=False), encoding="utf-8")

        n = fb.seed_from_snapshots(data_dir=str(d), verbose=False)
        self.assertEqual(n, 2)
        lines = [json.loads(x) for x in
                 self.perf.read_text(encoding="utf-8").splitlines()]
        by_url = {r["url"]: r for r in lines}
        self.assertEqual(by_url["https://www.zhihu.com/answer/1"]["observed"],
                         "2026-08-22")
        self.assertEqual(by_url["https://www.zhihu.com/answer/1"]["likes"], 2)
        self.assertEqual(by_url["https://www.zhihu.com/answer/2"]["observed"],
                         "2026-08-30")
        self.assertEqual(by_url["https://www.zhihu.com/answer/2"]["genre"],
                         "甜文")
        # 幂等：重复回填不产生重复观测
        self.assertEqual(fb.seed_from_snapshots(data_dir=str(d)), 0)

    # ---------------- 题材先验 ----------------

    def test_summarize_priors_and_boost(self):
        self._write_perf([
            {"url": "a1", "title": "甜文1", "genre": "甜文",
             "publish_date": "2026-08-29", "observed": "2026-08-30",
             "reads": 100, "likes": 20, "comments": 0, "collects": 0,
             "favors": 0},
            {"url": "a2", "title": "甜文2", "genre": "甜文",
             "publish_date": "2026-08-29", "observed": "2026-08-30",
             "reads": 50, "likes": 10, "comments": 0, "collects": 0,
             "favors": 0},
            {"url": "a3", "title": "虐文1", "genre": "虐文/火葬场",
             "publish_date": "2026-08-29", "observed": "2026-08-30",
             "reads": 60, "likes": 0, "comments": 0, "collects": 0,
             "favors": 0},
        ])
        s = fb.summarize(auto_seed=False)
        self.assertEqual(s["n_articles"], 3)
        self.assertIn("甜文", s["genres"])
        self.assertIn("虐文/火葬场", s["genres"])
        self.assertGreater(
            s["genres"]["甜文"]["score"],
            s["genres"]["虐文/火葬场"]["score"])

        # 高互动题材乘数 > 1；低互动 < 1；未知题材 = 1
        high = fb.topic_genre_multiplier("有没有甜甜的小说推荐？")
        low = fb.topic_genre_multiplier("有没有追妻火葬场的文？")
        unknown = fb.topic_genre_multiplier("量子计算是什么？")
        self.assertGreater(high, 1.0)
        self.assertLess(low, 1.0)
        self.assertGreaterEqual(low, 0.5)
        self.assertLessEqual(low, 2.0)
        self.assertEqual(unknown, 1.0)

    def test_summarize_empty_returns_ok(self):
        s = fb.summarize(auto_seed=False)
        self.assertEqual(s["n_articles"], 0)
        self.assertEqual(s["genres"], {})
        self.assertEqual(fb.topic_genre_multiplier("随便什么题"), 1.0)

    def test_rare_genre_falls_back_to_overall(self):
        # 单篇题材 n=1 < 2 → 有效分回落全局；但返回明细仍给原始分
        self._write_perf([
            {"url": "a1", "title": "独苗", "genre": "古言",
             "publish_date": "2026-08-29", "observed": "2026-08-30",
             "reads": 10, "likes": 5, "comments": 0, "collects": 0,
             "favors": 0},
        ])
        s = fb.summarize(auto_seed=False)
        g = s["genres"]["古言"]
        self.assertEqual(g["n"], 1)
        self.assertEqual(g["effective_score"], s["overall"]["score"])

    # ---------------- 题材分类 ----------------

    def test_dom_score_unchanged_without_prior_data(self):
        # 无任何表现观测时，题目带题材也返回原始分（乘数恒 1.0）
        from workflows.zhihu import ZhihuWorkflow
        q = {"likes": 100, "comments": 4,
             "title": "量子计算相关的书籍推荐？"}
        self.assertEqual(ZhihuWorkflow._dom_score(q), 500)
        q2 = {"likes": 100, "comments": 4, "is_hot": True,
              "title": "有没有甜甜的小说推荐？"}
        self.assertEqual(ZhihuWorkflow._dom_score(q2), 1000)

    def test_classify_genre_basic(self):
        self.assertEqual(classify_genre("有没有好看的病娇双男主文？"),
                         "双男主/耽美")
        self.assertEqual(classify_genre("讲一个悬疑灵异的故事"),
                         "悬疑/灵异")
        self.assertEqual(classify_genre("随便聊聊今天天气"), "其他")


if __name__ == "__main__":
    unittest.main()
