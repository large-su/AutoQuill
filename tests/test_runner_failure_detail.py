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
                        patch.object(runner, 'start', return_value=None), \
                        patch.object(executor, '_browser_busy', return_value=[]), \
                        patch('web_drivers.browser_pool.profile_in_use', return_value=False), \
                        patch.object(executor, '_checkin_context', return_value={}):
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

    def test_unavailable_model_and_cancellation_stop_both_round_loops(self):
        from web_drivers.browser_pool import WebModelUnavailable, WorkflowCancelled

        for mode in ('single', 'clean'):
            for error in (WebModelUnavailable, WorkflowCancelled):
                with self.subTest(mode=mode, error=error.__name__):
                    workflow = Mock()
                    run_round = getattr(workflow, 'run_' + mode)
                    run_round.side_effect = [error('停止当前任务'), True]
                    with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow), \
                            patch('time.sleep'):
                        with self.assertRaises(error):
                            TaskRunner()._dispatch(_RunSpec(mode=mode, rounds=2))
                    run_round.assert_called_once()

    def test_false_result_still_fails_without_inventing_reason(self):
        workflow = Mock()
        workflow.last_failure_reason = ''
        workflow.run_single.return_value = False
        with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow):
            self.assertFalse(TaskRunner()._dispatch(_RunSpec(mode='single')))

    def test_story_review_failure_reaches_automation_status(self):
        workflow = Mock()
        workflow.last_failure_reason = '全文编辑复核未完成：编辑复核的原文证据无法核对'
        workflow.run_single.return_value = False
        runner = TaskRunner()
        with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow), \
                patch('config.LLM_MODE', 'api'), \
                patch('webui.run_manager._llm_configured', return_value=True), \
                patch('web_drivers.reset_driver'), \
                patch('web_drivers.browser_pool.close_shared_browser'):
            runner._run_task(_RunSpec(mode='single'))
        self.assertEqual(runner.status()['state'], 'error')
        self.assertIn('编辑复核的原文证据无法核对', runner.status()['message'])
        from automation import executor
        with patch('webui.run_manager.runner', runner), \
                patch.object(runner, 'start', return_value=None), \
                patch.object(executor, '_browser_busy', return_value=[]), \
                patch('web_drivers.browser_pool.profile_in_use', return_value=False), \
                patch.object(executor, '_checkin_context', return_value={}):
            result = executor._full_chain({'type': 'full_chain'})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('编辑复核的原文证据无法核对', result['message'])

    def test_clean_audit_failure_reaches_automation_status(self):
        workflow = Mock()
        workflow.last_failure_reason = '编辑复核未通过：原文证据无法核对'
        workflow.run_clean.return_value = False
        runner = TaskRunner()
        with patch('workflows.zhihu.ZhihuWorkflow', return_value=workflow), \
                patch('config.LLM_MODE', 'api'), \
                patch('webui.run_manager._llm_configured', return_value=True), \
                patch('web_drivers.reset_driver'), \
                patch('web_drivers.browser_pool.close_shared_browser'):
            runner._run_task(_RunSpec(mode='clean'))
        self.assertEqual(runner.status()['state'], 'error')
        self.assertIn('原文证据无法核对', runner.status()['message'])
        from automation import executor
        with patch('webui.run_manager.runner', runner), \
                patch.object(runner, 'start', return_value=None), \
                patch.object(executor, '_browser_busy', return_value=[]), \
                patch('web_drivers.browser_pool.profile_in_use', return_value=False), \
                patch.object(executor, '_checkin_context', return_value={}):
            result = executor._full_chain({'type': 'full_chain', 'params': {'mode': 'clean'}})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('原文证据无法核对', result['message'])

    def test_real_clean_audit_reason_is_preserved(self):
        from workflows.base import WorkflowBase

        workflow = WorkflowBase()
        audit = {'passed': False, 'verdict': '编辑复核未通过',
                 'reasons': ['编辑复核的原文证据无法核对']}
        with patch.object(workflow, 'select_topic_clean', return_value='unused', create=True), \
                patch.object(workflow, 'extract_content_clean',
                             return_value=('题', '', {}, 'unused'), create=True), \
                patch.object(workflow, 'maybe_checkin_interact'), \
                patch.object(workflow, 'generate_clean_with_retry',
                             return_value=('正文', audit)), \
                patch.object(workflow, 'save_story_file', return_value='saved.md'):
            self.assertFalse(workflow.run_clean())
        self.assertEqual(workflow.last_failure_reason,
                         '编辑复核未通过：编辑复核的原文证据无法核对')

    def test_real_classic_review_reason_is_preserved_for_runner(self):
        from workflows.base import WorkflowBase
        from core import story_editor

        workflow = WorkflowBase()
        story = '她把信放回桌上，决定明天再来。' * 50
        with patch.object(workflow, 'select_topic', return_value='unused'), \
                patch.object(workflow, 'extract_content', return_value=('题', '', {}, 'unused')), \
                patch.object(workflow, 'maybe_checkin_interact'), \
                patch.object(workflow, 'generate_story', return_value=story), \
                patch('core.story_text.validate_story_format', return_value=(10, True, {})), \
                patch('core.story_editor.review_story',
                      return_value=story_editor.unavailable('编辑复核的原文证据无法核对')), \
                patch.object(workflow, 'save_story_file', return_value='saved.md'):
            self.assertFalse(workflow.run_single())
        self.assertEqual(workflow.last_failure_reason,
                         '全文编辑复核未完成：编辑复核的原文证据无法核对')
