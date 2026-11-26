"""Composing stays bounded; revision receives the actual full draft."""
import unittest
from unittest.mock import patch

from core.story_length import body_chars
from story_prompt import build_clean_prompt, build_story_prompt, build_revision_prompt


class RevisionPromptTests(unittest.TestCase):
    def test_full_draft_and_repair_are_present_without_reference_reinjection(self):
        draft = '开头甲。' + '中段乙。' * 700 + '结尾丙。'
        with patch('story_prompt._resolve_meta_content', side_effect=AssertionError):
            prompt, mode = build_story_prompt(
                '第三人称，完整结局', '参考不应再次注入', recipe={'hook': '旧配方不再注入'},
                feedback='补足两人的相互选择', previous_story=draft)
        self.assertIn(draft, prompt)
        self.assertGreater(prompt.index('## 发布前自检'), prompt.index(draft) + len(draft))
        self.assertIn('补足两人的相互选择', prompt)
        self.assertIn('第三人称，完整结局', prompt)
        self.assertNotIn('参考不应再次注入', prompt)
        self.assertNotIn('旧配方不再注入', prompt)
        self.assertNotIn('全新的故事', prompt)
        self.assertIn('保留符合题意的人物姓名', prompt)
        self.assertIn('修订', mode)

    def test_length_repair_uses_measured_body_and_target(self):
        draft = '## **1**\n\n' + '花' * 6200
        prompt, _ = build_revision_prompt('4000到5000字', draft, '压缩重复情节')
        self.assertEqual(body_chars(draft), 6200)
        self.assertIn('本稿正文实测6200字符', prompt)
        self.assertIn('目标约4500字符', prompt)
        self.assertIn('约压缩1700字符', prompt)

    def test_under_length_requires_new_action_instead_of_duplicate_story(self):
        prompt, _ = build_revision_prompt('4000到5000字', '花' * 3900, '补齐结局')
        self.assertIn('至少补足100字符', prompt)
        self.assertIn('不重复叙述已有情节', prompt)

    def test_clean_revision_preserves_paragraph_freedom(self):
        draft = '她送还了钥匙。\n\n他把门留着。'
        prompt, mode = build_clean_prompt('日常故事', '不再发送的参考',
                                          feedback=['补足相处'], previous_story=draft)
        self.assertIn(draft, prompt)
        self.assertIn('不强加章节数量', prompt)
        self.assertNotIn('## **N**', prompt)
        self.assertNotIn('不再发送的参考', prompt)
        self.assertIn('纯净', mode)

    def test_legacy_contract_keeps_its_format_rules_during_revision(self):
        from story_prompt import FORMAT_SELF_CHECK_RULE
        with patch('config.story.READER_CONTRACT_ENABLE', False):
            prompt, _ = build_story_prompt('故事', previous_story='完整原稿', feedback='修正时间')
        self.assertIn(FORMAT_SELF_CHECK_RULE, prompt)
        self.assertNotIn('各节篇幅按本题总预算分配', prompt)

    def test_compact_contract_has_one_format_authority_in_all_material_paths(self):
        for material in ('sample', 'reference', 'recipe', 'recipe_and_reference'):
            with self.subTest(material=material), \
                 patch('config.story.READER_CONTRACT_ENABLE', True), \
                 patch('config.story.STORY_MATERIAL_MODE', material):
                prompt, _ = build_story_prompt('写4000到5000字的故事', '自创参考。',
                                               recipe={'hook': '一个具体问题'}, opening_auto=False)
            self.assertEqual(prompt.count('## 发布前自检'), 1)
            self.assertNotIn('每节 600-800 字', prompt)
            self.assertNotIn('每节不少于', prompt)
            self.assertNotIn('5000-7000', prompt)
            self.assertIn('第一节接着推进', prompt)
            self.assertIn('谁知道什么', prompt)
            # Fixed tiny material catches accidental re-stacking of the old
            # repeated rules, independently of production reference length.
            self.assertLess(len(prompt), 4200)


if __name__ == '__main__':
    unittest.main()
