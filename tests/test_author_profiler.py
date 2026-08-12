# ============================================================
# tests/test_author_profiler.py — 作者写作技能提炼模块测试
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import json
import os
import tempfile
import unittest
from unittest import mock

from applications.zhihu_story.author_profiler import (
    compute_text_stats,
    load_author_profile,
    load_author_stories,
    load_general_stories,
    parse_publish_date,
    save_profile,
    story_weight,
    _first_n_sentences,
    _last_n_sentences,
    _parse_profile_json,
    _sample_story,
)

SHORT_STORY = "我推开门。屋里很暗。他站在窗前，没回头。\n「你来了。」他说。\n我的心跳漏了一拍。我们之间只隔着几步，却像隔着整个冬天。"


def _write_lib(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class TestLoadAuthorStories(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = os.path.join(self.tmp.name, "lib.jsonl")
        _write_lib(self.lib, [
            {"author": "甲", "title": "t1", "answer": "短" * 120,
             "footer": {"likes": 100}},
            {"author": "甲", "title": "t2", "answer": "中" * 200,
             "footer": {"likes": 50}},
            {"author": "甲", "title": "t3", "answer": "长" * 300,
             "footer": {"likes": 200}},
            {"author": "乙", "title": "other", "answer": "他" * 200,
             "footer": {"likes": 999}},
            {"author": "甲", "title": "bad-json", "answer": "x"},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_filters_by_author_and_sorts_by_likes_desc(self):
        stories = load_author_stories("甲", source=self.lib)
        self.assertEqual([s["title"] for s in stories], ["t3", "t1", "t2"])
        self.assertNotIn("bad-json", [s["title"] for s in stories])

    def test_min_likes_filters(self):
        stories = load_author_stories("甲", min_likes=100, source=self.lib)
        self.assertEqual([s["title"] for s in stories], ["t3", "t1"])

    def test_missing_footer_likes_treated_as_zero(self):
        _write_lib(self.lib, [
            {"author": "甲", "title": "no-footer", "answer": "x" * 200},
        ])
        # 无互动数据按 0 处理：min_likes=0 时保留，min_likes=1 时剔除
        self.assertEqual(len(load_author_stories("甲", min_likes=0, source=self.lib)), 1)
        self.assertEqual(len(load_author_stories("甲", min_likes=1, source=self.lib)), 0)

    def test_missing_library_returns_empty(self):
        self.assertEqual(
            load_author_stories("甲", source=os.path.join(self.tmp.name, "nope.jsonl")),
            [])


class TestStoryWeight(unittest.TestCase):
    def test_parse_publish_date_formats(self):
        # 兼容两种分隔符：'·广东' / '・广东' / 无后缀
        self.assertEqual(str(parse_publish_date(
            {"publish_time": "2026-02-20 11:03·广东"})), "2026-02-20")
        self.assertEqual(str(parse_publish_date(
            {"publish_time": "2026-02-20 11:03・广东"})), "2026-02-20")
        self.assertEqual(str(parse_publish_date(
            {"publish_time": "2026-07-22 13:23"})), "2026-07-22")
        self.assertIsNone(parse_publish_date({"publish_time": "未知"}))
        self.assertIsNone(parse_publish_date({}))
        self.assertIsNone(parse_publish_date(None))

    def test_weight_requires_likes(self):
        self.assertEqual(story_weight({}), 0.0)
        self.assertEqual(story_weight({"likes": 0}), 0.0)
        self.assertGreater(story_weight({"likes": 500}), 0.0)

    def test_weight_prefers_fresh_high_likes(self):
        import datetime
        today = datetime.date(2026, 8, 10)
        fresh_high = story_weight(
            {"likes": 900, "publish_time": "2026-07-01 10:00"}, today=today)
        old_high = story_weight(
            {"likes": 900, "publish_time": "2025-07-01 10:00"}, today=today)
        no_time = story_weight({"likes": 900}, today=today)
        self.assertGreater(fresh_high, no_time)
        self.assertGreater(no_time, old_high)
        # 新鲜度衰减不能归零
        self.assertGreater(old_high, 0.0)

class TestLoadGeneralStories(unittest.TestCase):
    def test_cross_author_collection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, "lib.jsonl")
            _write_lib(lib, [
                {"author": "甲", "title": "a1", "answer": "x" * 300,
                 "footer": {"likes": 500, "publish_time": "2026-07-01 10:00"}},
                {"author": "乙", "title": "b1", "answer": "y" * 300,
                 "footer": {"likes": 300, "publish_time": "2026-06-01 10:00"}},
                {"author": "甲", "title": "low", "answer": "z" * 300,
                 "footer": {"likes": 5}},
            ])
            all_stories = load_general_stories(source=lib)
            self.assertEqual([s["author"] for s in all_stories],
                             ["甲", "乙", "甲"])
            only_a = load_general_stories(source=lib, authors=["甲"])
            self.assertEqual(len(only_a), 2)
            gated = load_general_stories(source=lib, min_likes=100)
            self.assertEqual(len(gated), 2)


class TestComputeTextStats(unittest.TestCase):
    def test_basic_signals(self):
        story = ("「我爱你。」他说。我们走吧！"
                 "\n\n" + SHORT_STORY)
        stats = compute_text_stats([{"answer": story, "chars": len(story)}])
        self.assertEqual(stats["stories_count"], 1)
        self.assertEqual(stats["total_chars"], len(story))
        self.assertGreater(stats["avg_sentence_len"], 0)
        self.assertGreater(stats["short_sentence_ratio"], 0.5)
        self.assertGreater(stats["dialogue_ratio"], 0)
        self.assertGreater(stats["exclamation_per_1000"], 0)
        self.assertGreater(stats["first_person_per_1000"], 0)

    def test_em_dash_count(self):
        story = "他说——等等——然后又沉默了。"
        stats = compute_text_stats([{"answer": story, "chars": len(story)}])
        self.assertEqual(stats["em_dash_count"], 2)

    def test_dialogue_quote_styles(self):
        # 三种引号风格都应算作对话行
        for quote_open, quote_close in [("「", "」"), ("“", "”"), ('"', '"')]:
            story = f"第一行叙事。\n{quote_open}你好{quote_close}他说。\n第三行叙事。"
            stats = compute_text_stats([{"answer": story, "chars": len(story)}])
            self.assertEqual(stats["dialogue_ratio"], round(1 / 3, 2))

    def test_openings_and_endings(self):
        stories = [{"answer": SHORT_STORY, "chars": len(SHORT_STORY)}]
        stats = compute_text_stats(stories)
        self.assertEqual(stats["openings"][0], "我推开门。屋里很暗。他站在窗前，没回头。")
        self.assertTrue(stats["endings"][0].endswith("像隔着整个冬天。"))

    def test_empty_input(self):
        self.assertEqual(compute_text_stats([]), {})


class TestSentenceSlicers(unittest.TestCase):
    def test_first_n_sentences(self):
        self.assertEqual(
            _first_n_sentences("一。二！三？四……五。", 3),
            "一。二。三。")

    def test_last_n_sentences(self):
        self.assertEqual(
            _last_n_sentences("一。二！三？四……五。", 2),
            "四。五。")


class TestSampleStory(unittest.TestCase):
    def test_short_story_unchanged(self):
        self.assertEqual(_sample_story(SHORT_STORY), SHORT_STORY)

    def test_long_story_has_head_middle_tail(self):
        long = "甲" * 4000
        sample = _sample_story(long)
        self.assertIn("中段略", sample)
        self.assertLess(len(sample), 3500)
        self.assertTrue(sample.startswith("甲"))
        self.assertTrue(sample.endswith("甲"))


class TestParseProfileJson(unittest.TestCase):
    def test_plain_json(self):
        text = '{"style": "短句白描", "tone": "克制"}'
        profile = _parse_profile_json(text)
        self.assertEqual(profile["style"], "短句白描")

    def test_code_fenced_json(self):
        text = '```json\n{"style": "短句白描"}\n```'
        self.assertEqual(_parse_profile_json(text)["style"], "短句白描")

    def test_surrounding_text(self):
        text = '好的，以下是剖析：\n{"style": "短句白描", "opening_patterns": ["a"]}\n希望有用'
        profile = _parse_profile_json(text)
        self.assertEqual(profile["opening_patterns"], ["a"])

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_profile_json("完全不是 JSON"))
        self.assertIsNone(_parse_profile_json(""))
        self.assertIsNone(_parse_profile_json('{"no_style": 1}'))


class TestProfileProgress(unittest.TestCase):
    """文风提炼的分阶段进度回调（webui 进度条驱动）。"""

    def setUp(self):
        from applications.zhihu_story import author_profiler as ap
        self._ap = ap
        self._orig_lib = ap.STORY_LIB
        self._orig_dir = ap.AUTHORS_DIR
        self.tmp = tempfile.TemporaryDirectory()
        ap.STORY_LIB = os.path.join(self.tmp.name, "lib.jsonl")
        ap.AUTHORS_DIR = os.path.join(self.tmp.name, "authors")
        _write_lib(ap.STORY_LIB, [
            {"author": "甲", "title": f"t{i}", "answer": "长" * 300,
             "footer": {"likes": 200}}
            for i in range(3)
        ])

    def tearDown(self):
        self._ap.STORY_LIB = self._orig_lib
        self._ap.AUTHORS_DIR = self._orig_dir
        self.tmp.cleanup()

    def test_stages_reported_in_order(self):
        events = []
        fake = {"style": "短句", "tone": "冷"}
        with mock.patch.object(self._ap, "_call_profile_llm",
                               return_value=fake):
            profile = self._ap.profile_author(
                "甲", progress=lambda t, p=None: events.append((t, p)))
        self.assertIsNotNone(profile)
        self.assertGreaterEqual(len(events), 4)
        # 阶段顺序：读取 → 统计 → 剖析中 → 保存
        self.assertIn("读取作者", events[0][0])
        self.assertTrue(any("文本统计" in t for t, _ in events))
        self.assertTrue(any("剖析中" in t for t, _ in events))
        self.assertTrue(any("保存" in t for t, _ in events))
        # 剖析中阶段无百分比（不确定进度）
        pct = [p for t, p in events if "剖析中" in t][0]
        self.assertIsNone(pct)
        # 有确定进度的阶段最终到 100
        known = [p for _, p in events if p is not None]
        self.assertEqual(known[-1], 100)

    def test_insufficient_stories_stops_before_llm(self):
        _write_lib(self._ap.STORY_LIB, [
            {"author": "甲", "title": "t1", "answer": "长" * 300,
             "footer": {"likes": 200}}])
        events = []
        with mock.patch.object(self._ap, "_call_profile_llm") as m:
            result = self._ap.profile_author(
                "甲", progress=lambda t, p=None: events.append(t))
        self.assertIsNone(result)
        m.assert_not_called()
        self.assertFalse(any("剖析中" in t for t in events),
                         "样本不足不应进入剖析阶段")


class TestSaveLoadProfile(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = {"author": "镜中花", "signature": {"style": "短句"},
                       "text_stats": {}, "source_stories": []}
            path = save_profile(profile, out_dir=tmp)
            self.assertTrue(os.path.exists(path))
            loaded = load_author_profile("镜中花", out_dir=tmp)
            self.assertEqual(loaded["signature"]["style"], "短句")

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_author_profile("不存在", out_dir=tmp))


if __name__ == "__main__":
    unittest.main()
