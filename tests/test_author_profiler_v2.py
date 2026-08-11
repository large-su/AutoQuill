# ============================================================
# tests/test_author_profiler_v2.py — 提炼模块 v2 升级维度测试
#
# 覆盖：句长分位数、感官词/比喻/对话标签统计、编号小节检测、
#       结尾钩子检测、跨篇一致性、赞加权 prompt、禁忌分级渲染
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import unittest

from applications.zhihu_story.author_profiler import (
    _percentile,
    _count_sense_words,
    _count_metaphors,
    _count_dialogue_tags,
    _count_numbered_sections,
    _has_cliffhanger,
    compute_text_stats,
    _format_consistency_for_prompt,
    _format_stories_for_prompt,
    render_style_section,
)


class TestPercentile(unittest.TestCase):
    def test_linear_interpolation(self):
        # [1,2,3,4] P50 = 2.5
        self.assertAlmostEqual(_percentile([1, 2, 3, 4], 50), 2.5)
        # P25 = 1.75
        self.assertAlmostEqual(_percentile([1, 2, 3, 4], 25), 1.75)

    def test_single_value(self):
        self.assertEqual(_percentile([7], 90), 7)

    def test_empty(self):
        self.assertEqual(_percentile([], 50), 0)


class TestSenseWords(unittest.TestCase):
    def test_counts_across_senses(self):
        text = "她看着我，目光温柔。我听到他的声音，伸手抱住了他。香气扑鼻，味道很甜。"
        counts = _count_sense_words(text)
        self.assertGreater(counts["视觉"], 0)
        self.assertGreater(counts["听觉"], 0)
        self.assertGreater(counts["触觉"], 0)
        self.assertGreater(counts["嗅觉"], 0)
        self.assertGreater(counts["味觉"], 0)

    def test_empty_text(self):
        counts = _count_sense_words("")
        self.assertEqual(sum(counts.values()), 0)


class TestMetaphors(unittest.TestCase):
    def test_counts(self):
        text = "他像一阵风，仿佛从未存在，好像这一切都是梦。"
        self.assertEqual(_count_metaphors(text), 3)

    def test_zero(self):
        self.assertEqual(_count_metaphors("没有任何比喻"), 0)


class TestDialogueTags(unittest.TestCase):
    def test_tag_counts(self):
        text = "他说：走吧。她问道：去哪？他喊了一声。"
        counts = _count_dialogue_tags(text)
        self.assertEqual(counts["说"], 1)
        self.assertEqual(counts["问"], 1)
        self.assertEqual(counts["喊"], 1)

    def test_noise_words_excluded(self):
        # "知道/小说/味道"中的"道/说/味"不应计入对话标签
        counts = _count_dialogue_tags("我知道这件事。这是一本小说。味道不错。")
        self.assertEqual(counts["说"], 0)
        self.assertEqual(counts["道"], 0)


class TestNumberedSections(unittest.TestCase):
    def test_detects_numbered_sections(self):
        text = ("开头……1第一节内容。2第二节内容。"
                "3第三节内容。10第十节。")
        self.assertEqual(_count_numbered_sections(text), 4)

    def test_ignores_inline_numbers(self):
        # 正文中的数字（金额/年龄/百分比）不误判为小节
        text = "他一个月工资7500。她26岁。涨价了50%。"
        self.assertEqual(_count_numbered_sections(text), 0)


class TestCliffhanger(unittest.TestCase):
    def test_detects_serialized_ending(self):
        self.assertTrue(_has_cliffhanger("……他盯着我：“别走。”（未完待续）"))
        self.assertTrue(_has_cliffhanger("……（未完，待续……）"))

    def test_no_cliffhanger(self):
        self.assertFalse(_has_cliffhanger("她笑了。故事到这里就结束了。"))


class TestComputeStatsV2(unittest.TestCase):
    def _stories(self):
        return [
            {"title": "甲的故事", "answer": "她哭了。他走了。"
             "……1她追了出去。2他回头。", "chars": 20, "footer": {"likes": 500}},
            {"title": "乙的故事", "answer": "他笑了。她说：“别走。”"
             "……1他留下。未完待续", "chars": 18, "footer": {"likes": 100}},
        ]

    def test_distribution_fields(self):
        stats = compute_text_stats(self._stories())
        self.assertIn("sentence_len_p10", stats)
        self.assertIn("sentence_len_p50", stats)
        self.assertIn("sentence_len_p90", stats)
        self.assertLessEqual(stats["sentence_len_p10"], stats["sentence_len_p50"])
        self.assertLessEqual(stats["sentence_len_p50"], stats["sentence_len_p90"])
        self.assertIn("long_sentence_ratio", stats)

    def test_upgrade_dimension_fields(self):
        stats = compute_text_stats(self._stories())
        self.assertIn("sense_words_per_1000", stats)
        self.assertIn("metaphor_per_1000", stats)
        self.assertIn("dialogue_tags_per_1000", stats)
        self.assertIn("section_ratio", stats)
        self.assertIn("avg_sections_per_story", stats)
        self.assertIn("cliffhanger_ratio", stats)
        self.assertIn("per_story", stats)

    def test_per_story_signals(self):
        stats = compute_text_stats(self._stories())
        per = stats["per_story"]
        self.assertEqual(len(per), 2)
        self.assertEqual(per[0]["title"], "甲的故事")
        self.assertEqual(per[0]["likes"], 500)
        self.assertFalse(per[0]["has_cliffhanger"])
        self.assertTrue(per[1]["has_cliffhanger"])

    def test_section_ratio(self):
        stats = compute_text_stats(self._stories())
        self.assertEqual(stats["section_ratio"], 1.0)

    def test_v1_fields_preserved(self):
        stats = compute_text_stats(self._stories())
        for key in ("avg_sentence_len", "short_sentence_ratio",
                    "dialogue_ratio", "exclamation_per_1000",
                    "first_person_per_1000", "em_dash_count",
                    "openings", "endings"):
            self.assertIn(key, stats)

    def test_empty_input(self):
        self.assertEqual(compute_text_stats([]), {})


class TestConsistencyFormat(unittest.TestCase):
    def test_formats_per_story_rows(self):
        stats = compute_text_stats([
            {"title": "甲", "answer": "……1第一章。2第二章。未完待续",
             "chars": 15, "footer": {"likes": 300}},
            {"title": "乙", "answer": "……1第一章。", "chars": 7,
             "footer": {"likes": 50}},
        ])
        text = _format_consistency_for_prompt(stats)
        self.assertIn("跨篇一致性", text)
        self.assertIn("甲", text)
        self.assertIn("短句比例跨篇范围", text)
        self.assertIn("数字编号小节的篇数占比：100%", text)

    def test_too_few_stories(self):
        stats = {"per_story": [{"title": "仅一篇", "likes": 1, "chars": 10,
                                "short_sentence_ratio": 1.0,
                                "dialogue_ratio": 0.0,
                                "numbered_sections": 1,
                                "has_cliffhanger": True}]}
        self.assertIn("无法做跨篇一致性判断",
                      _format_consistency_for_prompt(stats))


class TestLikesWeighting(unittest.TestCase):
    def test_high_likes_marked(self):
        stories = [
            {"title": "爆款", "answer": "x" * 500, "chars": 500,
             "footer": {"likes": 924, "comments": 179}},
            {"title": "冷门", "answer": "y" * 500, "chars": 500,
             "footer": {"likes": 50, "comments": 2}},
        ]
        text = _format_stories_for_prompt(stories)
        # v3：经验权重标记（无 weight 字段的旧数据按点赞分级）
        self.assertIn("经验权重", text)
        self.assertIn("爆款", text)
        self.assertIn("[高]", text)
        self.assertIn("冷门", text)
        self.assertIn("[低]", text)

    def test_weight_field_used_when_present(self):
        import datetime
        stories = [
            {"title": "近新", "answer": "x" * 500, "chars": 500,
             "footer": {"likes": 900, "publish_time": "2026-07-01 10:00"},
             "weight": 6.8, "publish_date": datetime.date(2026, 7, 1)},
            {"title": "远古", "answer": "y" * 500, "chars": 500,
             "footer": {"likes": 900, "publish_time": "2025-07-01 10:00"},
             "weight": 0.6, "publish_date": datetime.date(2025, 7, 1)},
        ]
        text = _format_stories_for_prompt(stories)
        self.assertIn("经验权重=6.8 [高]", text)
        self.assertIn("经验权重=0.6 [低]", text)
        self.assertIn("发表于 2026-07-01", text)


class TestRenderTabooList(unittest.TestCase):
    def test_dict_items_with_source(self):
        profile = {"author": "测试作者", "signature": {
            "style": "短句白描",
            "taboo_list": [
                {"rule": "回避环境描写", "source": "统计"},
                {"rule": "作者自述不写悲剧", "source": "自述"},
            ],
            "tension_conflicts": ["冷静叙述与激烈情绪并存"],
            "cross_story_consistency": "短句稳定，题材化技法随题变化",
            "sentence_rhythm": "P10=4字 P50=12字 P90=30字",
            "sensory_preference": "视觉主导",
            "metaphor_fingerprint": "低密度，生活化喻体",
            "dialogue_tag_style": "说/道为主",
        }}
        section = render_style_section(profile)
        self.assertIn("回避环境描写 [统计]", section)
        self.assertIn("作者自述不写悲剧 [自述]", section)
        self.assertIn("冷静叙述与激烈情绪并存", section)
        self.assertIn("跨篇稳定性", section)
        self.assertIn("P10=4字", section)
        self.assertIn("视觉主导", section)
        self.assertIn("低密度，生活化喻体", section)
        self.assertIn("说/道为主", section)

    def test_string_items_backward_compat(self):
        profile = {"author": "旧作者", "signature": {
            "style": "x", "taboo_list": ["旧格式字符串条目"]}}
        section = render_style_section(profile)
        self.assertIn("- 旧格式字符串条目", section)
        self.assertIn("（未提炼）", section)

    def test_missing_v2_fields(self):
        profile = {"author": "旧作者", "signature": {"style": "x"}}
        section = render_style_section(profile)
        for field in ("句长节奏", "感官偏好", "比喻指纹",
                      "对话标签习惯", "禁忌清单", "风格张力", "跨篇稳定性"):
            self.assertIn(field, section)
            # 未提炼的降级提示出现多次
        self.assertGreaterEqual(section.count("（未提炼）"), 5)


if __name__ == "__main__":
    unittest.main()
