import unittest
from unittest.mock import patch

from config import story as story_config
from workflows.base import WorkflowBase


class CleanWorkflowContentGateTests(unittest.TestCase):
    def test_batch_valid_rewrite_can_replace_content_rejected_draft(self):
        from core.story_text import validate_story_format
        good = ('我推开门时，他正把我的箱子搬到门外。\n\n'
                + '\n\n'.join('## **%d**\n\n' % i
                    + '\n\n'.join(['他把钥匙放在桌上，问我愿不愿意一起离开。',
                                     '我拿起钥匙，让他先把那封信读完。'] * 18)
                                  for i in range(1, 9)))
        bad = good + '\n\n写作计划：接下来按要求设计故事章节。'
        old_score, valid, _ = validate_story_format(bad)
        self.assertFalse(valid)
        self.assertTrue(validate_story_format(good)[1])
        material = {'index': 1, 'title': '题目', 'answer': '参考', 'format_score': old_score}
        workflow = WorkflowBase()
        accepted = []
        with patch.object(workflow, '_generate_web', return_value=good), \
             patch.object(workflow, 'save_story_file', return_value='clean.md'), \
             patch('web_drivers.reset_driver'):
            self.assertEqual(workflow._batch_retry_web_serial([material], accepted), 1)
        self.assertEqual(accepted[0]['story'], good)

    def test_content_gate_runs_even_when_originality_audit_disabled(self):
        workflow = WorkflowBase()
        bad = '她推开门。\n写作计划：按用户要求安排故事章节。\n他回来了。'
        good = '她推开门。\n他把伞递过来，两人一同走进雨里。'
        with patch.object(story_config, 'CLEAN_AUDIT_ENABLE', False), \
             patch.object(workflow, 'generate_clean_story', side_effect=[bad, good]) as generate:
            text, audit = workflow.generate_clean_with_retry('题目', '参考', max_attempts=2)
        self.assertEqual(text, good)
        self.assertTrue(audit['passed'])
        self.assertIn('非故事内容', generate.call_args_list[1].kwargs['feedback'])
        self.assertIn('完整故事正文', generate.call_args_list[1].kwargs['feedback'])

    def test_rejected_content_never_reaches_llm_originality_audit(self):
        from core import originality
        workflow = WorkflowBase()
        bad = '门开了。\n用户要求我写一篇故事，要先分析字数。\n灯灭了。'
        with patch.object(story_config, 'CLEAN_AUDIT_ENABLE', True), \
             patch.object(workflow, 'generate_clean_story', return_value=bad), \
             patch.object(originality, 'audit_originality') as audit_llm:
            text, audit = workflow.generate_clean_with_retry('题目', '参考', max_attempts=1)
        self.assertEqual(text, bad)  # 原稿留给本地复核，passed=False阻止进入草稿发布。
        self.assertFalse(audit['passed'])
        self.assertEqual(audit['verdict'], '非故事内容')
        audit_llm.assert_not_called()
