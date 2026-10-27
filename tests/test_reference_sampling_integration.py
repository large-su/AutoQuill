"""Bounded reference coverage must not alter other workflows or call models."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution
from core.story_text import sample_reference_sections
from story_prompt import build_clean_prompt, build_story_prompt
from workflows.base import WorkflowBase


class ReferenceSamplingIntegrationTest(unittest.TestCase):
    reference = '开头标记。' + '甲乙丙丁。\n\n' * 900 + '末段标记。'

    def prompt(self, enabled, mode='sample', reference=None):
        with patch('config.story.STORY_MATERIAL_MODE', mode), \
             patch('config.story.REFERENCE_NARRATIVE_SAMPLE_ENABLE', enabled):
            return build_story_prompt(
                '日常故事，完整结局', self.reference if reference is None else reference,
                recipe={'genre': '日常'}, feedback='保留这条重试反馈', opening_auto=False)[0]

    def test_default_sampling_covers_end_without_extra_model_call(self):
        with patch('llm_client.call_llm_non_streaming', side_effect=AssertionError), \
             patch('web_drivers.get_driver', side_effect=AssertionError):
            prompt = self.prompt(True)
        self.assertIn('开头标记。', prompt)
        self.assertIn('末段标记。', prompt)
        self.assertIn('末段不等于完整结局', prompt)
        self.assertIn('不得推测省略情节', prompt)
        self.assertIn('严禁借鉴情节', prompt)
        self.assertGreater(prompt.index('保留这条重试反馈'), prompt.index('末段标记。'))

    def test_disabled_keeps_old_head_sample_and_instruction(self):
        prompt = self.prompt(False)
        self.assertIn(sample_reference_sections(self.reference), prompt)
        self.assertNotIn('末段标记。', prompt)
        self.assertIn('高赞回答开头——重点学习其"开头引入"的手法', prompt)
        self.assertNotIn('末段不等于完整结局', prompt)

    def test_other_paths_and_empty_reference_are_identical(self):
        for mode in ('recipe', 'reference', 'recipe_and_reference'):
            with self.subTest(mode=mode):
                self.assertEqual(self.prompt(True, mode), self.prompt(False, mode))
        self.assertEqual(self.prompt(True, reference=''), self.prompt(False, reference=''))
        with patch('config.story.REFERENCE_NARRATIVE_SAMPLE_ENABLE', True):
            new = build_clean_prompt('题目', self.reference, feedback='重试')
        with patch('config.story.REFERENCE_NARRATIVE_SAMPLE_ENABLE', False):
            self.assertEqual(new, build_clean_prompt('题目', self.reference, feedback='重试'))

    def test_saved_configuration_and_trial_episodes(self):
        with tempfile.TemporaryDirectory() as root:
            local = lambda *parts: str(Path(root, *parts))
            with patch('core.paths.data', local), patch('core.paths.program', local):
                workflow = WorkflowBase()
                for index, enabled in enumerate((True, False, True), 1):
                    with patch('config.story.REFERENCE_NARRATIVE_SAMPLE_ENABLE', enabled):
                        workflow.save_story_file('本轮独立原创正文 ' + str(index), index=index)
                data = json.loads(Path(local('data', 'state', 'evolution.json')).read_text(encoding='utf-8'))
        generations = data['generations']
        self.assertEqual([g['settings']['REFERENCE_NARRATIVE_SAMPLE_ENABLE'] for g in generations],
                         [True, False, True])
        self.assertEqual(len({g['episode'] for g in generations}), 3)
        self.assertEqual(generations[0]['fingerprint'], generations[2]['fingerprint'])
        self.assertNotEqual(generations[0]['fingerprint'], generations[1]['fingerprint'])
        self.assertNotIn('REFERENCE_NARRATIVE_SAMPLE_ENABLE', evolution._settings({'LLM_MODE': 'web'}))

    def test_sampler_code_is_in_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'core', 'reference_sample.py')
            source.parent.mkdir()
            source.write_text('first', encoding='utf-8')
            with patch('core.paths.program', lambda *parts: str(Path(root, *parts))):
                first = evolution._code_id()
                source.write_text('second', encoding='utf-8')
                self.assertNotEqual(first, evolution._code_id())

    def test_pre_sampler_fingerprint_is_unchanged(self):
        from tools.evolution_history import current_writing_code_id
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'story_prompt.py').write_text('old prompt', encoding='utf-8')
            expected = hashlib.sha256()
            for name in sorted(set(evolution._FILES) - {'core/reference_sample.py'}):
                content = b'old prompt' if name == 'story_prompt.py' else b'<missing>'
                expected.update(name.encode() + b'\0' + content + b'\0')
            with patch('core.paths.program', lambda *parts: str(Path(root, *parts))):
                self.assertEqual(expected.hexdigest(), evolution._code_id())
            self.assertEqual(expected.hexdigest(), current_writing_code_id(Path(root)))


if __name__ == '__main__':
    unittest.main()
