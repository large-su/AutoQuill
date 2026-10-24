"""v5.5 reader-contract prompt selection and no-extra-call regression."""
import unittest
from unittest.mock import patch

from story_prompt import (
    build_clean_prompt,
    build_story_prompt,
)


class ReaderContractTest(unittest.TestCase):
    TITLE = '温柔日常：两个人把旧误会说开，完整结局，不要反转'
    REFERENCE = '参考正文只学习语感，不借人物或情节。'
    RECIPE = {'genre': '日常', 'hook': '面对旧误会', 'tone': '温柔'}
    AUTHOR = {
        'author': '保留作者',
        'signature': {'style': '保留作者文本，不可被模板吞掉。'},
    }

    def test_classic_paths_replace_hard_formula_with_title_driven_contract(self):
        for material in ('sample', 'reference', 'recipe', 'recipe_and_reference'):
            with self.subTest(material=material), \
                 patch('config.story.STORY_MATERIAL_MODE', material), \
                 patch('config.story.READER_CONTRACT_ENABLE', True):
                message, _ = build_story_prompt(
                    self.TITLE, self.REFERENCE, recipe=self.RECIPE,
                    meta_knowledge='只供本篇使用的元知识。',
                    author_profile=self.AUTHOR,
                    opening_auto=False,
                )
                self.assertIn('这篇故事要让读者看到哪个具体问题被解决', message)
                self.assertNotIn('开篇前 ~1000 字只铺垫', message)
                self.assertNotIn('至少 2 层套娃真相', message)
                self.assertNotIn('一次打脸四部曲', message)
                self.assertNotIn('留钩非句号', message)
                self.assertNotIn('每节末尾留钩子', message)
                self.assertIn(self.TITLE, message)
                self.assertIn('只供本篇使用的元知识。', message)
                self.assertIn('保留作者文本，不可被模板吞掉。', message)
                if material != 'recipe':
                    self.assertIn(self.REFERENCE, message)

    def test_disable_selects_original_template_verbatim(self):
        for material, legacy_marker in (
            ('sample', '开篇前 ~1000 字只铺垫'),
            ('reference', '开篇前 ~1000 字只铺垫'),
            ('recipe', '至少 2 层套娃真相'),
            ('recipe_and_reference', '至少 2 层套娃真相'),
        ):
            with self.subTest(material=material), \
                 patch('config.story.STORY_MATERIAL_MODE', material), \
                 patch('config.story.READER_CONTRACT_ENABLE', False):
                message, _ = build_story_prompt(
                    self.TITLE, self.REFERENCE, recipe=self.RECIPE,
                    opening_auto=False,
                )
                self.assertIn(legacy_marker, message)
                self.assertNotIn('这篇故事要让读者看到哪个具体问题被解决', message)

    def test_clean_does_not_gain_duplicate_guidance_and_retry_stays_last(self):
        with patch('config.story.READER_CONTRACT_ENABLE', True):
            message, mode = build_clean_prompt(
                '悬疑故事要完整结局', self.REFERENCE, feedback='修正原创问题')
        self.assertEqual(mode, '纯净模式')
        self.assertNotIn('## 阅读承诺（按题意取舍）', message)
        self.assertNotIn('## **N**', message)
        self.assertGreater(message.index('修正原创问题'), message.index('## 读者体验'))

    def test_clean_disable_is_exactly_the_previous_prompt(self):
        with patch('config.story.READER_CONTRACT_ENABLE', False):
            baseline, _ = build_clean_prompt(self.TITLE, self.REFERENCE)
        with patch('config.story.READER_CONTRACT_ENABLE', True):
            enabled, _ = build_clean_prompt(self.TITLE, self.REFERENCE)
        self.assertEqual(enabled, baseline)

    def test_prompt_builder_does_not_call_a_model(self):
        # Both builders are pure prompt assembly; this boundary prevents the
        # new contract from adding planning or rewrite calls.
        with patch('story_prompt._reference_paragraph_stats', return_value=None), \
             patch('llm_client.call_llm_non_streaming', side_effect=AssertionError), \
             patch('web_drivers.get_driver', side_effect=AssertionError):
            build_story_prompt(self.TITLE, self.REFERENCE, recipe=self.RECIPE,
                               opening_auto=False)
            build_clean_prompt(self.TITLE, self.REFERENCE)


if __name__ == '__main__':
    unittest.main()
