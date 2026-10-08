"""Reader guidance stays optional and records the actual writing scheme."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config.story
from core import evolution
from story_prompt import build_clean_prompt, build_story_prompt, READER_EXPERIENCE_RULE
from workflows.base import WorkflowBase


class ReaderExperienceTest(unittest.TestCase):
    def test_guidance_is_shared_without_changing_mode_or_clean_format(self):
        with patch('config.story.READER_EXPERIENCE_ENABLE', True):
            clean, mode = build_clean_prompt('题目要求温柔日常', '参考风格。')
            self.assertEqual(mode, '纯净模式')
            self.assertNotIn('## **N**', clean)
            self.assertEqual(clean.count(READER_EXPERIENCE_RULE), 1)
            for material in ('sample', 'reference', 'recipe', 'recipe_and_reference'):
                with self.subTest(material=material), patch('config.story.STORY_MATERIAL_MODE', material):
                    message, _ = build_story_prompt('题目', '参考风格。', recipe={}, opening_auto=False)
                    self.assertEqual(message.count(READER_EXPERIENCE_RULE), 1)

    def test_disable_restores_prompt_and_retry_feedback_stays_last(self):
        for builder in (build_clean_prompt, build_story_prompt):
            with self.subTest(builder=builder.__name__):
                kwargs = {'opening_auto': False} if builder is build_story_prompt else {}
                with patch('config.story.READER_EXPERIENCE_ENABLE', False):
                    old, old_mode = builder('题目', '参考风格。', **kwargs)
                with patch('config.story.READER_EXPERIENCE_ENABLE', True):
                    new, new_mode = builder('题目', '参考风格。', **kwargs)
                    retry, _ = builder('题目', '参考风格。', feedback='请修正原创问题', **kwargs)
                self.assertEqual(new.replace(READER_EXPERIENCE_RULE, ''), old)
                self.assertEqual(new_mode, old_mode)
                self.assertGreater(retry.index('请修正原创问题'), retry.index(READER_EXPERIENCE_RULE))

    def test_clean_workflow_records_clean_and_toggle_creates_new_scheme(self):
        with tempfile.TemporaryDirectory() as folder:
            def local(*parts):
                return str(Path(folder, *parts))
            workflow = WorkflowBase()
            with patch('core.paths.data', local), patch('core.paths.program', local), \
                 patch.object(workflow, 'select_topic_clean', create=True, return_value='https://www.zhihu.com/question/123'), \
                 patch.object(workflow, 'extract_content_clean', create=True, return_value=('题目', '参考', {}, 'https://www.zhihu.com/question/123')), \
                 patch.object(workflow, 'generate_clean_with_retry', return_value=('原创正文', {'passed': True})), \
                 patch.object(workflow, 'maybe_checkin_interact'), patch.object(workflow, 'publish'), \
                 patch('core.feedback_loop.record_story_published') as ledger:
                self.assertTrue(workflow.run_clean())
                self.assertEqual(ledger.call_args.args[2]['mode'], 'clean')
                workflow.save_story_file('经典正文', index=2)
                with patch('config.story.READER_EXPERIENCE_ENABLE', False):
                    workflow.save_story_file('关闭指导正文', index=3)
                data = json.loads(Path(local('data', 'state', 'evolution.json')).read_text(encoding='utf-8'))
                first, classic, disabled = data['generations']
                self.assertEqual(first['settings']['WORKFLOW_MODE'], 'clean')
                self.assertEqual(classic['settings']['WORKFLOW_MODE'], 'classic')
                self.assertFalse(disabled['settings']['READER_EXPERIENCE_ENABLE'])
                self.assertNotEqual(first['fingerprint'], classic['fingerprint'])
                self.assertNotEqual(classic['fingerprint'], disabled['fingerprint'])
                self.assertEqual(first['settings']['SCORE_STORY_MIDDLE_CHARS'], 400)

    def test_scoring_source_changes_scheme_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder, patch('core.paths.program', lambda *p: str(Path(folder, *p))):
            score_file = Path(folder, 'story_scoring.py')
            score_file.write_text('old scoring', encoding='utf-8')
            before = evolution._code_id()
            score_file.write_text('new scoring', encoding='utf-8')
            self.assertNotEqual(before, evolution._code_id())


if __name__ == '__main__':
    unittest.main()
