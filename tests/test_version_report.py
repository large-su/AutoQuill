# -*- coding: utf-8 -*-
"""tools/version_feedback_report.py 的单元测试（解析/归因/关联）。"""
import os
import tempfile
import unittest

from tools.version_feedback_report import (join, load_snapshot_latest,
                                           norm_title, parse_logs,
                                           version_label)


class ParseLogsTest(unittest.TestCase):

    def test_parse_events(self):
        log = """2026-08-29 12:03:14,668 [INFO] 步骤 4：发布故事到知乎（DOM 通道）
2026-08-29 12:03:14,668 [INFO] 使用已有文件：output/story_20260829_120314.md
2026-08-29 12:03:15,000 [INFO]   格式检测：6/10 ✓合规
2026-08-29 12:03:19,000 [INFO] 草稿已保存，完成：「测试标题...」
2026-08-29 12:03:19,000 [INFO] 本轮完成！
"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "autoquill_t.log"), "w",
                      encoding="utf-8") as f:
                f.write(log)
            evs = parse_logs(d)
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual(e["draft"], "测试标题...")
        self.assertEqual(e["fmt"], 6)
        self.assertEqual(e["file"], "story_20260829_120314.md")
        self.assertFalse(e["dead"])

    def test_retry_and_dead_flags(self):
        log = """2026-08-29 13:46:16,000 [INFO] 使用已有文件：output/story_a.md
2026-08-29 13:53:30,000 [WARNING] 故事格式不合规（5/10），第 1/3 次重试…
2026-08-29 13:54:31,000 [WARNING] 故事格式不合规（5/10），第 2/3 次重试…
2026-08-29 13:55:32,000 [WARNING] 多次尝试均未通过格式校验（最高分版已存盘），标记废稿
"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "autoquill_t2.log"), "w",
                      encoding="utf-8") as f:
                f.write(log)
            evs = parse_logs(d)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["retries"], 2)
        self.assertTrue(evs[0]["dead"])


class VersionLabelTest(unittest.TestCase):

    def test_latest_commit_wins(self):
        tl = [(1000, "V4.5.0 发布"), (2000, "v4.6.1 修复"), (3000, "质量守则")]
        self.assertEqual(version_label(1500, tl), "V4.5.0")
        self.assertEqual(version_label(2500, tl), "V4.6.1")
        self.assertEqual(version_label(3500, tl), "质量守则")

    def test_descending_timeline_handled(self):
        # git log 默认为新→旧，version_label 应按升序扫描取最新提交
        tl = [(3000, "V4.7.0 发布"), (2000, "V4.6.1 修复"), (1000, "V4.5.0 发布")]
        self.assertEqual(version_label(2500, tl), "V4.6.1")
        self.assertEqual(version_label(1500, tl), "V4.5.0")

    def test_no_commit_falls_back_to_date(self):
        self.assertEqual(version_label(0, []), "01-01 dev")

    def test_empty_timeline_via_join_path(self):
        import time
        t = time.time()
        self.assertIsInstance(version_label(int(t), []), str)


class JoinAndSnapshotTest(unittest.TestCase):

    def test_norm_title(self):
        self.assertEqual(norm_title("有没有好看的小说推荐？..."),
                         "有没有好看的小说推荐")

    def test_join_matches_by_normalized_title(self):
        evs = [{"time": "2026-08-29 12:00:00",
                "draft": "有没有好看的小说推荐？..."}]
        rows = [{"title": "有没有好看的小说推荐？", "likes": 3, "reads": 10,
                 "publish_date": "2026-08-29"}]
        out = join(evs, rows, days=60)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pub"]["likes"], 3)

    def test_join_prefix_fallback_for_truncated_log_title(self):
        evs = [{"time": "2026-08-23 08:00:00",
                "draft": "写网文重要的是先写出一本一百万字垃圾，还是不断试错切书，直到..."}]
        rows = [{"title": "写网文重要的是先写出一本一百万字垃圾，还是不断试错切书，直到开始有了正反馈？对新人来讲选哪一个更好？",
                 "likes": 1, "reads": 71, "publish_date": "2026-08-23"}]
        out = join(evs, rows, days=60)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pub"]["likes"], 1)

    def test_join_prefix_ambiguous_no_match(self):
        # 截断前缀命中多条快照标题 → 视为不匹配（避免张冠李戴）
        evs = [{"time": "2026-08-23 08:00:00", "draft": "有没有好看的小说推荐..."}]
        rows = [
            {"title": "有没有好看的小说推荐一百本", "likes": 1, "reads": 1,
             "publish_date": "2026-08-23"},
            {"title": "有没有好看的小说推荐两百本", "likes": 2, "reads": 2,
             "publish_date": "2026-08-23"},
        ]
        out = join(evs, rows, days=60)
        self.assertIsNone(out[0]["pub"])

    def test_join_filters_old_events(self):
        evs = [{"time": "2026-01-01 12:00:00", "draft": "旧题"},]
        rows = [{"title": "旧题", "likes": 3, "reads": 10,
                 "publish_date": "2026-01-01"}]
        self.assertEqual(join(evs, rows, days=30), [])

    def test_load_snapshot_latest_both_schemas(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            snap = os.path.join(d, "published_answers_2026-08-30.json")
            with open(snap, "w", encoding="utf-8") as f:
                json.dump([
                    {"aid": "1", "url": "u1", "title": "老格式",
                     "publish": "08-29 10:00",
                     "metrics": {"阅读": "10", "赞同": "1", "评论": "0",
                                 "收藏": "0", "喜欢": "0"}},
                    {"aid": "2", "url": "u2", "title": "新格式",
                     "publish_date": "2026-08-29",
                     "reads": 20, "likes": 2, "comments": 0, "collects": 0,
                     "favors": 0},
                ], f, ensure_ascii=False)
            rows = load_snapshot_latest(d)
        self.assertEqual(len(rows), 2)
        by_title = {r["title"]: r for r in rows}
        self.assertEqual(by_title["老格式"]["likes"], 1)
        self.assertEqual(by_title["新格式"]["likes"], 2)
        self.assertEqual(by_title["老格式"]["publish_date"], "2026-08-29")


if __name__ == "__main__":
    unittest.main()
