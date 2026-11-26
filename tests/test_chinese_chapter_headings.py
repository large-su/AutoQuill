import unittest

from core.story_text import count_chapter_headings, validate_story_format


class ChineseMarkdownChapterHeadingTest(unittest.TestCase):
    def _story(self, headings, intro="窗外的雨忽然停了。"):
        paragraph = "她把钥匙放回桌上，决定先把眼前的事情说清楚。"
        body = "\n\n".join([paragraph] * 30)
        blocks = [heading + "\n\n" + body for heading in headings]
        return intro + "\n\n" + "\n\n".join(blocks)

    def test_six_pure_chinese_markdown_headings_count(self):
        text = self._story([f"## **{n}**" for n in "一二三四五六"])
        self.assertEqual(count_chapter_headings(text), 6)

    def test_chinese_markdown_heading_first_line_lacks_intro(self):
        text = self._story([f"## **{n}**" for n in "一二三四五六"], intro="")
        score, valid, details = validate_story_format(text.lstrip())
        self.assertFalse(valid)
        self.assertIn("引言", details)

    def test_chinese_markdown_story_valid_with_chapters(self):
        text = self._story([f"## **{n}**" for n in "一二三四五六"])
        score, valid, details = validate_story_format(text)
        self.assertTrue(valid, details)
        self.assertNotIn("章节", details)

    def test_prose_and_non_pure_markdown_lines_are_not_headings(self):
        text = "## **一** 这是正文里的短句\n\n三个人站在门口。\n\n## **一**"
        self.assertEqual(count_chapter_headings(text), 1)

    def test_existing_arabic_and_chinese_number_forms_remain_supported(self):
        text = "\n".join(["## **1**", "## **2**", "第3章", "4", "## **四**"])
        self.assertEqual(count_chapter_headings(text), 5)

    def test_existing_arabic_markdown_subtitle_is_not_regressed(self):
        self.assertEqual(count_chapter_headings('## **1** 重逢\n\n## **2** 雨停了'), 2)


if __name__ == "__main__":
    unittest.main()
