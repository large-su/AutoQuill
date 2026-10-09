"""Learning guidance can be withdrawn independently and traced to a scheme."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import evolution
from story_prompt import (
    CRAFT_LEARNING_RULE, READER_EXPERIENCE_RULE,
    build_clean_prompt, build_story_prompt,
)


class CraftLearningTest(unittest.TestCase):
    def test_all_material_modes_and_clean_allow_exact_prompt_rollback(self):
        recipe = {'genre': '日常', 'hook': '当场选择'}
        for mode in ('sample', 'reference', 'recipe', 'recipe_and_reference', 'clean'):
            with self.subTest(mode=mode), \
                 patch('config.story.STORY_MATERIAL_MODE', mode), \
                 patch('config.story.READER_EXPERIENCE_ENABLE', True):
                builder = build_clean_prompt if mode == 'clean' else build_story_prompt
                kwargs = {} if mode == 'clean' else {'recipe': recipe, 'opening_auto': False}
                with patch('config.story.CRAFT_LEARNING_ENABLE', False):
                    baseline, old_mode = builder('温柔日常，第三人称，不要反转', '原创风格样本。', **kwargs)
                with patch('config.story.CRAFT_LEARNING_ENABLE', True):
                    learned, new_mode = builder('温柔日常，第三人称，不要反转', '原创风格样本。', **kwargs)
                    retry, _ = builder('温柔日常，第三人称，不要反转', '原创风格样本。', feedback='请修正上次的问题', **kwargs)
                self.assertEqual(old_mode, new_mode)
                self.assertEqual(learned.count(CRAFT_LEARNING_RULE), 1)
                self.assertEqual(learned.replace(CRAFT_LEARNING_RULE, ''), baseline)
                self.assertEqual(learned.count(READER_EXPERIENCE_RULE), 1)
                self.assertGreater(retry.index('请修正上次的问题'), retry.index(CRAFT_LEARNING_RULE))
                if mode == 'clean':
                    self.assertNotIn('## **N**', learned)

    def test_guidance_is_independent_of_previous_experiment(self):
        with patch('config.story.READER_EXPERIENCE_ENABLE', False), \
             patch('config.story.CRAFT_LEARNING_ENABLE', True):
            message, _ = build_clean_prompt('题目', '')
        self.assertIn(CRAFT_LEARNING_RULE, message)
        self.assertNotIn(READER_EXPERIENCE_RULE, message)

    def test_generation_scheme_records_toggle_without_private_notes(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch('core.paths.data', lambda *p: str(Path(folder, *p))), \
             patch('core.paths.program', lambda *p: str(Path(folder, *p))):
            settings = {'CRAFT_LEARNING_ENABLE': False, 'api_key': 'secret', 'reading_notes': 'private'}
            a = evolution.record_generation('baseline.md', '旧稿', settings)
            settings['CRAFT_LEARNING_ENABLE'] = True
            b = evolution.record_generation('learned.md', '新稿', settings)
            self.assertNotEqual(a['fingerprint'], b['fingerprint'])
            records = json.loads(Path(folder, 'data', 'state', 'evolution.json').read_text(encoding='utf-8'))['generations']
            self.assertFalse(records[0]['settings']['CRAFT_LEARNING_ENABLE'])
            self.assertTrue(records[1]['settings']['CRAFT_LEARNING_ENABLE'])
            self.assertNotIn('api_key', records[1]['settings'])
            self.assertNotIn('reading_notes', records[1]['settings'])


if __name__ == '__main__':
    unittest.main()
