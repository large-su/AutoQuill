import json
import unittest

from core.story_text import (
    StoryContentError,
    ensure_story_content,
    story_content_issues,
    validate_story_format,
)


class StoryContentGuardTests(unittest.TestCase):
    def test_english_apostrophes_do_not_hide_metadata_or_break_dialogue(self):
        self.assertTrue(story_content_issues("I don't know.\n写作计划：先构思章节。\nIt's late."))
        self.assertEqual(story_content_issues("她举起纸条：\n「I'm a writer. 用户要求我写一篇故事。」\n我点头。"), [])

    def test_structured_model_payload_is_not_story_body(self):
        for payload in ('{"story":"她回来了。"}', '["她回来了。"]'):
            with self.subTest(payload=payload):
                self.assertEqual(story_content_issues(payload)[0]['code'], 'structured_payload')

    def test_embedded_editor_json_is_rejected_at_any_position(self):
        payload = json.dumps({'facts': {'people': ['小满']}, 'verdict': 'pass', 'issues': []},
                             ensure_ascii=False, indent=2)
        for prefix, suffix in (('', '\n她走了。'), ('她进门。\n', '\n她走了。'),
                               ('她进门。', '她走了。'), ('她进门。\n', '')):
            with self.subTest(prefix=prefix, suffix=suffix):
                text = prefix + payload + suffix
                issue = next(i for i in story_content_issues(text) if i['code'] == 'editor_payload')
                self.assertEqual(issue['offset'], len(prefix))
                with self.assertRaises(StoryContentError):
                    ensure_story_content(text)

    def test_in_world_json_and_quoted_editor_example_are_allowed(self):
        examples = (
            '她打开终端。\n{"name":"小满","count":3}\n门开了。',
            '她看着屏幕。\n{"verdict":"已回家","issues":[]}\n他松了口气。',
            '她举起纸条：\n「{"facts":{},"verdict":"pass","issues":[]}」\n他把纸条收好。',
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(story_content_issues(text), [])

    def test_malformed_editor_shape_is_not_a_keyword_veto(self):
        for value in ('{"verdict":[],"issues":[]}', '{"verdict":"pass","issues":{}}'):
            self.assertEqual(story_content_issues('屏幕亮了。\n' + value + '\n她关上门。'), [])

    def test_editor_schema_is_detected_independently_of_first_key(self):
        for verdict in ('pass', 'PASS', ' revise '):
            text = '她进门。\n' + json.dumps({'trace': 'x', 'verdict': verdict, 'issues': []}) + '\n他走来。'
            self.assertIn('editor_payload', {i['code'] for i in story_content_issues(text)})

    def test_markdown_meta_headings_and_closing_control_tags(self):
        for line in ('### **写作计划**', '</analysis>', '<|im_start|>assistant'):
            with self.subTest(line=line):
                self.assertTrue(story_content_issues('她走了。\n' + line + '\n门关上了。'))

    def test_known_delivery_tail_is_rejected_from_raw_text(self):
        text = "她推开门，看见满院灯火。\n\n需要我帮你写这个故事的番外吗？\n好的，帮我写一下"
        issues = story_content_issues(text)
        self.assertIn("assistant_tail", {item["code"] for item in issues})
        with self.assertRaises(StoryContentError) as raised:
            ensure_story_content(text)
        self.assertTrue(raised.exception.issues)

    def test_plan_at_start_and_middle_is_rejected(self):
        for text in (
            "我将严格遵循章节、字数和文风要求创作故事。\n她走进雨里。",
            "她走进雨里。\n让我先分析字数，再决定章节安排。\n她回头。",
        ):
            self.assertTrue(story_content_issues(text))

    def test_dialogue_and_ai_subject_are_allowed(self):
        text = "「用户说 AI 写作计划很荒唐。」她笑了。\n她说：「回头见。」"
        self.assertEqual(story_content_issues(text), [])
        self.assertIs(ensure_story_content(text), text)

    def test_unfinished_speech_prompt_is_rejected(self):
        issue = story_content_issues("夜色沉下来。\n她说：")[0]
        self.assertEqual(issue["code"], "unfinished_dialogue")

    def test_explicit_model_tags_and_plans_are_rejected_anywhere(self):
        for text in (
            "<think>用户要求我写一篇故事。",
            "她推开门。\n<analysis>先核对字数。\n她回头。",
            "她推开门。\n<|assistant|>写作计划：先列大纲。",
            "她推开门。\n用户要求我写一篇故事。",
            "她推开门。\n根据用户要求写一篇故事，并满足字数和格式。",
        ):
            self.assertTrue(story_content_issues(text), text)

    def test_story_like_labels_and_cross_line_dialogue_are_allowed(self):
        for text in (
            "系统：任务完成",
            "用户：你好",
            "如需修改数据，请提供替代数据源。",
            "她说：\n「写作计划要改到明天，AI 才肯放人。\n用户要求也先别管。」\n门外响起了雨声。",
            "「用户要求我写一篇故事，系统却说这只是游戏任务。」\n她合上终端。",
        ):
            self.assertEqual(story_content_issues(text), [], text)

    def test_unclosed_quote_does_not_hide_later_meta_content(self):
        issues = story_content_issues("「她的话断在半空。\n<think>先分析字数。")
        self.assertIn("meta_heading", {item["code"] for item in issues})

    def test_empty_body_is_rejected_by_the_publish_guard(self):
        for text in (None, "", " \n\t "):
            issues = story_content_issues(text)
            self.assertIn("empty_body", {item["code"] for item in issues})
            with self.assertRaises(StoryContentError):
                ensure_story_content(text)

    def test_only_a_final_speech_prompt_is_unfinished(self):
        self.assertEqual(
            story_content_issues("她说：\n「回家吧。」"), [],
        )
        issues = story_content_issues("夜色沉下来。\n我回：")
        self.assertIn("unfinished_dialogue", {item["code"] for item in issues})

    def test_validation_hard_veto_keeps_triplet_and_details(self):
        score, valid, details = validate_story_format("她走进雨里。\n写作计划：先分析字数。")
        self.assertIsInstance(score, int)
        self.assertEqual(score, 0)  # 非正文不能以高格式分阻止批量流程采用合格重写稿。
        self.assertFalse(valid)
        self.assertIn("内容", details)


if __name__ == "__main__":
    unittest.main()
