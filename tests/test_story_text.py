# ============================================================
# tests/test_story_text.py — core.story_text 文本管线测试
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import json
import unittest

from core.story_text import (
    clean_story_output,
    enforce_short_sentences,
    replace_em_dashes,
    fix_story_format,
    validate_story_format,
    parse_score_json,
    sample_reference_sections,
)


class TestCleanStoryOutput(unittest.TestCase):
    def test_removes_think_tags(self):
        text = "<think>内部推理过程</think>\n正文第一行。"
        self.assertNotIn("think", clean_story_output(text))
        self.assertNotIn("内部推理", clean_story_output(text))

    def test_removes_leading_noise(self):
        text = "好的\n收到！\n以下是正文。"
        self.assertFalse(clean_story_output(text).startswith("好的"))
        self.assertFalse(clean_story_output(text).startswith("收到"))

    def test_removes_trailing_noise(self):
        text = "正文。\n希望您喜欢这个故事！\n如有修改需求请联系。"
        out = clean_story_output(text)
        self.assertNotIn("希望您喜欢", out)
        self.assertNotIn("修改需求", out)

    def test_empty_input(self):
        self.assertIsNone(clean_story_output(None))
        self.assertEqual(clean_story_output(""), "")

    def test_over_half_shrink_keeps_original(self):
        # 清洗后剩余 < 原文 50% 且原文 > 200 字 → 回退原文
        noise = "好的。\n" * 60  # 180 字噪音
        body = "这是一段正文。" * 20  # 120 字正文（< 300*0.5）
        text = noise + body
        out = clean_story_output(text)
        self.assertEqual(out, text.strip())


class TestEnforceShortSentences(unittest.TestCase):
    def test_splits_at_sentence_ends(self):
        out = enforce_short_sentences("第一句。第二句！第三句？")
        self.assertEqual(out, "第一句。\n\n第二句！\n\n第三句？")

    def test_no_split_inside_brackets(self):
        text = "他说（这里是。内部标点）然后走了。"
        out = enforce_short_sentences(text)
        self.assertIn("（这里是。内部标点）", out)
        self.assertTrue(out.endswith("走了。"))

    def test_no_split_inside_quotes(self):
        text = "「等等。我还没说完！」她喊道。然后跑了。"
        out = enforce_short_sentences(text)
        self.assertIn("「等等。我还没说完！」", out)
        self.assertEqual(out.count("\n\n"), 1)

    def test_existing_single_newline_completed_to_double(self):
        out = enforce_short_sentences("第一句。\n第二句。")
        self.assertEqual(out, "第一句。\n\n第二句。")

    def test_existing_double_newline_untouched(self):
        out = enforce_short_sentences("第一句。\n\n第二句。")
        self.assertEqual(out, "第一句。\n\n第二句。")

    def test_nested_quote_and_bracket(self):
        text = "「他说（这。里）没事。」之后。"
        out = enforce_short_sentences(text)
        self.assertIn("「他说（这。里）没事。」", out)
        self.assertTrue(out.endswith("之后。"))


class TestReplaceEmDashes(unittest.TestCase):
    def test_outside_quotes_replaced_with_comma(self):
        self.assertEqual(replace_em_dashes("他说——这是插入语。"), "他说，这是插入语。")

    def test_inside_quotes_preserved(self):
        out = replace_em_dashes("「等等——」她喊道。")
        self.assertIn("——", out)

    def test_no_dashes_unchanged(self):
        self.assertEqual(replace_em_dashes("普通文本。"), "普通文本。")

    def test_mixed(self):
        out = replace_em_dashes("「我——」他顿住——然后笑了。")
        self.assertIn("「我——」", out)
        self.assertIn("顿住，然后笑了", out)


class TestFixStoryFormat(unittest.TestCase):
    def test_curly_quotes_normalized(self):
        out = fix_story_format("她说“你好”他说”再见“")
        self.assertIn("「你好」", out)

    def test_h1_title_removed(self):
        out = fix_story_format("# 故事标题\n\n正文第一行。")
        self.assertNotIn("# 故事标题", out)

    def test_bold_title_removed(self):
        out = fix_story_format("**故事标题**\n\n正文第一行。")
        self.assertNotIn("**故事标题**", out)

    def test_ai_prefix_stripped(self):
        out = fix_story_format("好的，马上为您创作。\n\n正文。")
        self.assertFalse(out.startswith("好的"))

    def test_em_dashes_replaced(self):
        out = fix_story_format("他想了想——然后点头。")
        self.assertIn("，", out)
        self.assertNotIn("——", out)

    def test_separator_lines_removed(self):
        out = fix_story_format("正文。\n\n---\n\n另一段。")
        self.assertNotIn("---", out)

    def test_single_newlines_merged_to_double(self):
        out = fix_story_format("第一行。\n第二行。")
        self.assertEqual(out, "第一行。\n\n第二行。")

    def test_triple_newlines_compressed(self):
        out = fix_story_format("第一段。\n\n\n\n第二段。")
        self.assertNotIn("\n\n\n", out)

    def test_empty_input(self):
        self.assertIsNone(fix_story_format(None))
        self.assertEqual(fix_story_format("   "), "   ")


class TestValidateStoryFormat(unittest.TestCase):
    def test_empty_text_invalid(self):
        score, valid, details = validate_story_format("")
        self.assertEqual(score, 0)
        self.assertFalse(valid)

    def test_well_formatted_story_passes(self):
        # 8 章 × 15 段 × ~42 字/段 ≈ 5000 字，段落都低于长段阈值
        para = "这是正文内容。" * 7  # 42 字
        chapters = [f"## **{i}**\n\n" + (para + "\n\n") * 14 + para for i in range(1, 9)]
        body = "\n\n".join(chapters)
        self.assertGreaterEqual(len(body), 4000)
        score, valid, details = validate_story_format(body)
        self.assertTrue(valid, details)
        self.assertGreaterEqual(score, 6)

    def test_missing_chapters_penalized(self):
        body = "## **1**\n\n" + "这是正文。" * 500 + "\n\n## **2**\n\n" + "这是正文。" * 500
        score, valid, _ = validate_story_format(body)
        self.assertLessEqual(score, 6)

    def test_ai_prefix_penalized(self):
        body = "好的，这是故事开头。" + "正文。" * 3000 + "\n\n## **1**\n\n正文。" * 6
        score, valid, details = validate_story_format(body)
        self.assertIn("废话", details)

    def test_long_paragraph_penalized(self):
        long_para = "这是超长段落内容。" * 30  # 300+ 字
        body = "## **1**\n\n" + long_para + "\n\n## **2**\n\n" + long_para + "\n\n"
        body += "## **3**\n\n" + long_para + "\n\n## **4**\n\n" + long_para + "\n\n"
        body += "## **5**\n\n" + long_para + "\n\n## **6**\n\n" + long_para
        score, valid, details = validate_story_format(body)
        self.assertIn("长段", details)


class TestParseScoreJson(unittest.TestCase):
    def test_valid_json_list(self):
        payload = json.dumps([{"index": 1, "hook": 8, "plot": 7, "emotion": 6,
                               "authenticity": 5, "ending": 9, "format": 8,
                               "total": 43, "comment": "好"}])
        out = parse_score_json(payload, 1)
        self.assertEqual(out[0]["total"], 43)

    def test_json_with_prefix_text(self):
        reply = '解析结果如下：{"index":1,"hook":9,"plot":9,"emotion":9,"authenticity":9,"ending":9,"format":9,"total":54,"comment":"不错"}'
        out = parse_score_json(reply, 1)
        self.assertEqual(out[0]["total"], 54)
        self.assertEqual(out[0]["comment"], "不错")

    def test_regex_fallback_multiple(self):
        reply = ('评分：{"index":1,"hook":8,"plot":7,"emotion":6,"authenticity":5,"ending":9,"format":8,"total":43,"comment":"好"}；'
                 '下一条：{"index":2,"hook":6,"plot":8,"emotion":7,"authenticity":6,"ending":7,"format":9,"total":43,"comment":"不错"}')
        out = parse_score_json(reply, 2)
        self.assertEqual(len(out), 2)
        self.assertEqual([s["index"] for s in out], [1, 2])

    def test_unparseable_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_score_json("完全不是评分内容", 1)


class TestSampleReferenceSections(unittest.TestCase):
    """参考文章注入素材：直接截取开头 max_chars 字，零 LLM 调用。"""

    def test_empty_input_returns_empty(self):
        self.assertEqual(sample_reference_sections(""), "")
        self.assertEqual(sample_reference_sections(None), "")
        self.assertEqual(sample_reference_sections("  "), "")

    def test_short_answer_kept_whole(self):
        ans = "开头一段。\n\n第二段。\n\n第三段。"
        self.assertEqual(sample_reference_sections(ans), ans)

    def test_long_answer_truncated_to_head(self):
        # 超过阈值只留开头：直接注入前 max_chars 字
        ans = "开头第一段。\n\n" + ("填充段落内容。" * 400)
        out = sample_reference_sections(ans)
        self.assertLessEqual(len(out), 3000)
        self.assertTrue(out.startswith("开头第一段。"))
        # 截断只保留开头，不再有尾部/中略标注
        self.assertNotIn("中略", out)

    def test_max_chars_parameter(self):
        ans = "开头段。" + ("x" * 5000)
        out = sample_reference_sections(ans, max_chars=1000)
        self.assertLessEqual(len(out), 1000)

    def test_truncation_breaks_at_sentence_boundary(self):
        # 超过阈值且后半段存在句末标点时，在句末截断而非硬切
        ans = "甲。" * 200 + "乙。" * 400
        out = sample_reference_sections(ans, max_chars=1000)
        self.assertLessEqual(len(out), 1000)
        # 回退截断保留完整句子（不带半句）
        self.assertTrue(out.endswith("。") or out.endswith("！")
                        or out.endswith("？") or out.endswith("\n\n"))

    def test_truncation_no_boundary_falls_back_to_hard_cut(self):
        # 无句末标点可用时直接硬切（仍然不超阈值）
        ans = "无标点文本" * 1000
        out = sample_reference_sections(ans, max_chars=500)
        self.assertLessEqual(len(out), 500)

    def test_preserves_original_paragraph_breaks(self):
        ans = "\n\n".join(f"第{i}段内容。" for i in range(50))
        out = sample_reference_sections(ans)
        self.assertIn("\n\n", out)


if __name__ == "__main__":
    unittest.main()
