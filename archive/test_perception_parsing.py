# ============================================================
# tests/test_perception_parsing.py — perception 纯解析函数测试
#
# 覆盖：结尾标记 / footer 互动解析 / 时间戳解析 / 标题提取 /
# 点赞合并。仅测纯函数（字符串→结构），IO 部分（OCR 调用）不测。
#
# 运行：python -m unittest discover -s tests
# ============================================================

import unittest

from applications.zhihu_story.perception import (
    _END_PATTERN, _is_answer_end_marker, _check_lines_for_end,
    parse_likes_only, parse_footer_line, parse_end_timestamp,
    _merge_upvote_likes, _is_valid_title, extract_question_title,
)


# ============================================================
# 结尾标记
# ============================================================

class TestAnswerEndMarker(unittest.TestCase):
    """_END_PATTERN：'编辑于/发布于 + 日期' 的 OCR 结尾行。"""

    def test_full_datetime(self):
        self.assertTrue(_is_answer_end_marker("编辑于 2024-07-31 18:43"))

    def test_publish_variant(self):
        self.assertTrue(_is_answer_end_marker("发布于 2024-07-31"))

    def test_slash_format(self):
        self.assertTrue(_is_answer_end_marker("编辑于 2024/7/1"))

    def test_chinese_format(self):
        self.assertTrue(_is_answer_end_marker("发布于 2024年7月1日"))

    def test_two_digit_year(self):
        self.assertTrue(_END_PATTERN.search("编辑于 24-07-31"))

    def test_plain_text(self):
        self.assertFalse(_is_answer_end_marker("这是一个普通回答"))

    def test_bare_keyword_no_date(self):
        self.assertFalse(_is_answer_end_marker("编辑于"))

    def test_check_lines_finds_index(self):
        lines = ["回答内容", "编辑于 2024-07-31 18:43", "更多内容"]
        self.assertEqual(_check_lines_for_end(lines), (True, 1))

    def test_check_lines_no_match(self):
        self.assertEqual(_check_lines_for_end(["普通文本"]), (False, -1))


# ============================================================
# 时间戳解析
# ============================================================

class TestParseEndTimestamp(unittest.TestCase):
    """parse_end_timestamp：'编辑于 2024-07-31 18:43' → ISO 字符串。"""

    def test_full_datetime(self):
        self.assertEqual(
            parse_end_timestamp("编辑于 2024-07-31 18:43"),
            "2024-07-31T18:43")

    def test_slash_date_only(self):
        self.assertEqual(parse_end_timestamp("发布于 2024/7/1"),
                         "2024-07-01T00:00")

    def test_chinese_date_with_time(self):
        self.assertEqual(
            parse_end_timestamp("发布于 2024年7月1日 08:05"),
            "2024-07-01T08:05")

    def test_chinese_date_no_time(self):
        self.assertEqual(parse_end_timestamp("发布于 2024年7月1日"),
                         "2024-07-01T00:00")

    def test_full_width_colon(self):
        self.assertEqual(
            parse_end_timestamp("编辑于 2024-07-31 18：43"),
            "2024-07-31T18:43")

    def test_invalid_month_returns_none(self):
        self.assertIsNone(parse_end_timestamp("编辑于 2024-13-45 99:99"))

    def test_no_timestamp_returns_none(self):
        self.assertIsNone(parse_end_timestamp("普通文本"))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_end_timestamp(""))
        self.assertIsNone(parse_end_timestamp(None))


# ============================================================
# 赞同数解析（纯数字行）
# ============================================================

class TestParseLikesOnly(unittest.TestCase):
    """parse_likes_only：'赞同 640' / '640 赞同' / 万·k 单位。"""

    def test_number_after_keyword(self):
        self.assertEqual(parse_likes_only("赞同 640"), 640)

    def test_number_before_keyword(self):
        self.assertEqual(parse_likes_only("640 赞同"), 640)

    def test_ren_qualifier(self):
        self.assertEqual(parse_likes_only("640 人赞同"), 640)

    def test_wan_unit(self):
        self.assertEqual(parse_likes_only("赞同 1.2万"), 12000)

    def test_wan_unit_before(self):
        self.assertEqual(parse_likes_only("2.5万 赞同"), 25000)

    def test_k_unit(self):
        self.assertEqual(parse_likes_only("赞同 3k"), 3000)
        self.assertEqual(parse_likes_only("赞同 3K"), 3000)

    def test_comma_thousands(self):
        self.assertEqual(parse_likes_only("12,345 赞同"), 12345)

    def test_no_keyword_returns_none(self):
        self.assertIsNone(parse_likes_only("640"))

    def test_keyword_no_number_returns_none(self):
        self.assertIsNone(parse_likes_only("赞同"))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_likes_only(""))
        self.assertIsNone(parse_likes_only(None))


# ============================================================
# Footer 互动行解析（likes/comments/collects/hearts）
# ============================================================

class TestParseFooterLine(unittest.TestCase):
    """parse_footer_line：'赞同 640 条评论 12 收藏 5' → 互动 dict。"""

    def test_full_interaction_line(self):
        # 锚形态：likes 在 '赞同' 前（模式1），comments 用 '条评论' 锚前数字
        self.assertEqual(
            parse_footer_line("640 赞同 12 条评论 30 收藏"),
            {'likes': 640, 'comments': 12, 'collects': 30, 'hearts': 0})

    def test_with_hearts(self):
        self.assertEqual(
            parse_footer_line("640 赞同 12 条评论 30 收藏 8 喜欢"),
            {'likes': 640, 'comments': 12, 'collects': 30, 'hearts': 8})

    def test_dot_separated_zhihu_style(self):
        self.assertEqual(
            parse_footer_line("赞同 640 · 评论 12 · 收藏 5"),
            {'likes': 640, 'comments': 12, 'collects': 5, 'hearts': 0})

    def test_likes_only(self):
        self.assertEqual(
            parse_footer_line("赞同 640"),
            {'likes': 640, 'comments': 0, 'collects': 0, 'hearts': 0})

    def test_wan_unit_likes(self):
        self.assertEqual(
            parse_footer_line("赞同 1.2万"),
            {'likes': 12000, 'comments': 0, 'collects': 0, 'hearts': 0})

    def test_share_truncates_segment(self):
        self.assertEqual(
            parse_footer_line("赞同 640 分享"),
            {'likes': 640, 'comments': 0, 'collects': 0, 'hearts': 0})

    def test_no_zan_keyword_returns_none(self):
        self.assertIsNone(parse_footer_line("评论 12 收藏 5"))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_footer_line(""))
        self.assertIsNone(parse_footer_line(None))


# ============================================================
# 独立赞同按钮采集合并
# ============================================================

class TestMergeUpvoteLikes(unittest.TestCase):
    """_merge_upvote_likes：按钮采集的赞同数覆盖 footer 字段。"""

    def test_merge_overrides_likes(self):
        footer = {'likes': 100, 'comments': 3, 'collects': 1, 'hearts': 0}
        upvote = {'value': 999, 'raw_line': "999 赞同"}
        merged = _merge_upvote_likes(footer, upvote)
        self.assertEqual(merged['likes'], 999)
        self.assertEqual(merged['likes_source'], 'upvote_button')
        self.assertEqual(merged['raw_likes_line'], "999 赞同")
        self.assertEqual(merged['comments'], 3)  # 其他字段保留

    def test_no_upvote_returns_footer_unchanged(self):
        footer = {'likes': 100}
        self.assertIs(_merge_upvote_likes(footer, None), footer)

    def test_footer_none_creates_dict(self):
        merged = _merge_upvote_likes(None, {'value': 5, 'raw_line': "5 赞同"})
        self.assertEqual(merged['likes'], 5)


# ============================================================
# 标题有效性
# ============================================================

class TestIsValidTitle(unittest.TestCase):
    """_is_valid_title：推荐页标题噪音过滤。"""

    def test_valid_title(self):
        self.assertTrue(_is_valid_title("如何评价《三体》这部小说？"))

    def test_too_short(self):
        self.assertFalse(_is_valid_title("短"))

    def test_noise_word(self):
        self.assertFalse(_is_valid_title("飙升"))

    def test_recommend_noise(self):
        self.assertFalse(_is_valid_title("你在「话题下获得了关注"))

    def test_pure_number(self):
        self.assertFalse(_is_valid_title("12345"))
        self.assertFalse(_is_valid_title("1,234"))

    def test_metrics_line(self):
        self.assertFalse(_is_valid_title("浏览 1.2万 回答 300"))

    def test_old_question(self):
        self.assertFalse(_is_valid_title("3天前的提问"))
        self.assertFalse(_is_valid_title("12天前的提问"))

    def test_rating_line(self):
        # 实现只覆盖「分/级 + 数字」形态；'9.4分' 数字在前不命中（文档化边界）
        self.assertFalse(_is_valid_title("级94"))
        self.assertTrue(_is_valid_title("9.4分"))


# ============================================================
# 问题标题提取
# ============================================================

class TestExtractQuestionTitle(unittest.TestCase):
    """extract_question_title：从 OCR 行中提取标题（含跨行合并）。"""

    def test_single_title_line(self):
        lines = ["如何评价国产电影？", "关注", "123 关注者"]
        self.assertEqual(extract_question_title(lines), "如何评价国产电影？")

    def test_question_mark_line_kept_as_title(self):
        # 问号行在前（idx=0）不触发合并；其余候选行以换行追加
        lines = ["这是真正的标题？", "这是一个很长的叙述性内容", "关注问题"]
        self.assertEqual(
            extract_question_title(lines),
            "这是真正的标题？\n这是一个很长的叙述性内容")

    def test_split_title_merged(self):
        # 标题被 OCR 断成两行：前一行不以标点结尾且长度足够 → 拼接；
        # 合并后问号行移入 other_lines（真实行为）
        lines = ["一个跨越十年的爱情故事", "最终还是走散了？", "关注问题"]
        self.assertEqual(
            extract_question_title(lines),
            "一个跨越十年的爱情故事最终还是走散了？\n最终还是走散了？")

    def test_no_question_mark_takes_longest(self):
        lines = ["短句", "这是一个比较长的标题描述"]
        self.assertEqual(
            extract_question_title(lines), "这是一个比较长的标题描述\n短句")

    def test_all_noise_returns_empty(self):
        lines = ["关注", "邀请回答", "写回答"]
        self.assertEqual(extract_question_title(lines), "")

    def test_empty_lines_returns_empty(self):
        self.assertEqual(extract_question_title([]), "")

    def test_pure_number_lines_filtered(self):
        # '1.2万' 去单位后为纯数字 → 排除；注意 '1.2万 浏览' 整行不命中
        lines = ["1.2万", "标题在这里？", "关注问题"]
        self.assertEqual(extract_question_title(lines), "标题在这里？")


if __name__ == "__main__":
    unittest.main()
