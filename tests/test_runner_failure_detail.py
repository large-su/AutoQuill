"""Workflow failures must remain actionable in runner/automation status."""
import unittest
from unittest.mock import Mock, patch

from webui.run_manager import TaskRunner, _RunSpec


class RunnerFailureDetailTest(unittest.TestCase):
    def test_exhausted_candidates_mentions_story_filter_without_lowering_gates(self):
        from workflows.zhihu import ZhihuWorkflow
        workflow = ZhihuWorkflow()
        with patch.object(workflow, '_browser'), \
                patch.object(workflow, '_extract_auto_parallel', return_value=(None, 2)), \
                patch('config.story.MAX_TOPIC_RETRY', 0), \
                patch('config.story.QUESTION_SELECT_MODE', 'auto'), \
                patch('config.story.QUESTION_SOURCE', 'recommend'):
            with self.assertRaises(RuntimeError) as caught:
                workflow.extract_content()
        self.assertIn('故事适配', str(caught.exception))
        self.assertNotIn('可调低', str(caught.exception))

    def test_failed_round_keeps_original_reason_in_terminal_status(self):
        for mode in ('single', 'clean'):
            with self.subTest(mode=mode):
                workflow = Mock()
                getattr(workflow, 'run_' + mode).side_effect = RuntimeError(
                    '提取失败：9 批候选均未通过故事适配筛选')
                runner = TaskRunner()
                with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow), \
                        patch('config.LLM_MODE', 'api'), \
                        patch('webui.run_manager._llm_configured', return_value=True), \
                        patch('web_drivers.reset_driver'), \
                        patch('web_drivers.browser_pool.close_shared_browser'):
                    runner._run_task(_RunSpec(mode=mode))
                self.assertEqual(runner.status()['state'], 'error')
                self.assertIn('9 批候选均未通过故事适配筛选', runner.status()['message'])
                from automation import executor
                with patch('webui.run_manager.runner', runner), \
                        patch.object(runner, 'start'), \
                        patch.object(executor, '_browser_busy', return_value=[]), \
                        patch('web_drivers.browser_pool.profile_in_use', return_value=False), \
                        patch.object(executor, '_checkin_context', return_value={}), \
                        patch.object(executor, '_set_checkin_context'):
                    result = executor._full_chain({'type': 'full_chain', 'params': {'mode': mode}})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['message'], runner.status()['message'])
                self.assertEqual(result['units'], 0)

    def test_successful_later_round_still_succeeds(self):
        for mode in ('single', 'clean'):
            with self.subTest(mode=mode):
                workflow = Mock()
                getattr(workflow, 'run_' + mode).side_effect = [RuntimeError('提取失败'), True]
                with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow), \
                        patch('time.sleep'):
                    self.assertTrue(TaskRunner()._dispatch(_RunSpec(mode=mode, rounds=2)))

    def test_false_result_still_fails_without_inventing_reason(self):
        workflow = Mock()
        workflow.run_single.return_value = False
        with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow):
            self.assertFalse(TaskRunner()._dispatch(_RunSpec(mode='single')))
