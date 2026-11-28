import json
import unittest
from unittest.mock import patch

import config
from config import story as story_config
from core import story_editor as editor
from workflows.base import WorkflowBase


class ReviewEvidenceTests(unittest.TestCase):
    story = '星期一，他把唯一一把钥匙交给了我。\n星期二，他说钥匙一直在他手上，从未交给别人。'

    def result(self, quotes=None):
        return {'verdict': 'revise', 'issues': [{
            'kind': 'causality', 'quotes': quotes or self.story.splitlines(),
            'problem': '钥匙去向矛盾', 'fix': '交代取回钥匙的行动'}]}

    def test_verified_contradiction_becomes_actionable_feedback(self):
        review = editor.parse_review(json.dumps(self.result()), self.story)
        self.assertEqual(review['status'], 'revise')
        feedback = editor.revision_feedback(review)
        self.assertIn('交代取回钥匙的行动', feedback)
        self.assertIn(self.story.splitlines()[1], feedback)
        self.assertIn('完整故事正文', feedback)

    def test_fabricated_evidence_does_not_become_revision(self):
        result = self.result(['星期三，钥匙被她扔进了河里。', self.story.splitlines()[0]])
        review = editor.parse_review(json.dumps(result), self.story)
        self.assertEqual(review['status'], 'unavailable')
        self.assertFalse(review['passed'])

    def test_contradiction_requires_two_distinct_passages(self):
        for quotes in [[self.story.splitlines()[0]], [self.story.splitlines()[0]] * 2]:
            self.assertEqual(editor.parse_review(json.dumps(self.result(quotes)), self.story)['status'], 'unavailable')

    def test_pass_is_consistent_with_empty_findings(self):
        self.assertTrue(editor.parse_review('{"verdict":"pass","issues":[]}', self.story)['passed'])
        result = self.result(); result['verdict'] = 'pass'
        self.assertFalse(editor.parse_review(json.dumps(result), self.story)['passed'])
        self.assertFalse(editor.parse_review('{"verdict":"revise","issues":[]}', self.story)['passed'])

    def test_whitespace_only_evidence_variation_is_allowed(self):
        result = self.result([q.replace('，', '，\n') for q in self.story.splitlines()])
        self.assertEqual(editor.parse_review(json.dumps(result), self.story)['status'], 'revise')


class ReviewChannelTests(unittest.TestCase):
    def setUp(self):
        for obj, key, value in [(story_config, 'STORY_EDITOR_ENABLE', True), (config, 'LLM_MODE', 'web')]:
            active = patch.object(obj, key, value); active.start(); self.addCleanup(active.stop)

    def test_entire_middle_and_ending_reach_one_request(self):
        story = '门开了。' + '她走上前去。' * 500 + '她把钥匙还给他，两人约好明天再见。'
        with patch('web_drivers.generate_isolated', return_value='{"verdict":"pass","issues":[]}') as request:
            result = editor.review_story('温暖的完整故事', story)
        self.assertTrue(result['passed'])
        request.assert_called_once()
        self.assertIn(story, request.call_args.args[0])

    def test_web_review_separates_writer_context_without_changing_other_requests(self):
        from story_scoring import _web_llm_generate
        from unittest.mock import Mock
        writer = Mock()
        writer.generate.return_value = 'reply'
        with patch('web_drivers.generate_isolated', return_value='{"verdict":"pass","issues":[]}') as isolated, \
             patch('web_drivers.get_driver', return_value=writer):
            self.assertTrue(editor.review_story('题', '她开门收下了花。')['passed'])
            isolated.assert_called_once()
            writer.generate.assert_not_called()
            self.assertEqual(_web_llm_generate('普通评分'), 'reply')
            writer.generate.assert_called_once_with('普通评分')

    def test_over_budget_is_not_silently_truncated(self):
        with patch('web_drivers.generate_isolated') as request:
            result = editor.review_story('题', '门开了。' * 7000)
        self.assertEqual(result['status'], 'unavailable')
        request.assert_not_called()

    def test_content_gate_runs_before_editor_request(self):
        with patch('web_drivers.generate_isolated') as request:
            result = editor.review_story('题', '门开了。\n写作计划：按用户要求安排故事章节。')
        self.assertFalse(result['passed']); request.assert_not_called()

    def test_missing_result_stops_instead_of_claiming_pass(self):
        with patch('web_drivers.generate_isolated', return_value=None) as request:
            result = editor.review_story('题', '她开门收下了花。')
        self.assertEqual(result['status'], 'unavailable')
        request.assert_called_once()

    def test_unconfirmed_web_send_keeps_its_reason_without_resending(self):
        from web_drivers.browser_pool import WebPromptNotConfirmed
        with patch('web_drivers.generate_isolated') as generate:
            generate.side_effect = WebPromptNotConfirmed('Yuanbao 发送按钮点击未确认')
            result = editor.review_story('题', '她开门收下了花。')
        self.assertFalse(result['passed'])
        self.assertIn('发送按钮点击未确认', result['reason'])
        generate.assert_called_once()

    def test_web_rechecks_full_draft_with_validation_feedback(self):
        example = ReviewEvidenceTests()
        invalid = example.result(['星期三，钥匙被她扔进了河里。', example.story.splitlines()[0]])
        replies = [json.dumps(invalid), json.dumps(example.result())]
        with patch('web_drivers.generate_isolated', side_effect=replies) as request:
            result = editor.review_story('题', example.story)
        self.assertEqual(result['status'], 'revise')
        self.assertEqual(request.call_count, 2)
        second = request.call_args
        self.assertIn(example.story, second.args[0])
        self.assertIn('原文证据无法核对', second.args[0])
        self.assertNotIn('钥匙被她扔进了河里', second.args[0])

    def test_two_invalid_web_reviews_still_block_draft(self):
        with patch('web_drivers.generate_isolated', return_value='结果不符合约定') as request:
            result = editor.review_story('题', '她开门收下了花。')
        self.assertFalse(result['passed'])
        self.assertIn('编辑复核结果无法解析', result['reason'])
        self.assertEqual(request.call_count, 2)

    def test_explicit_range_uses_local_count_before_model_call(self):
        with patch('web_drivers.generate_isolated') as request:
            review = editor.review_story('请写1000到1200字的故事', '她开门收下了花。' * 200)
        self.assertEqual(review['status'], 'revise')
        self.assertIn('1000～1200', review['issues'][0]['fix'])
        request.assert_not_called()

    def test_length_range_ignores_markdown_headings_and_whitespace(self):
        text = '## **1**\n\n' + '花' * 1100 + '\n\n'
        self.assertIsNone(editor.length_review('1000～1200字', text))
        self.assertIsNone(editor.length_review('讲一个2000年前的故事', text))

    def test_length_uses_the_same_markdown_chapter_recognition_as_format(self):
        from core.story_length import body_chars
        self.assertEqual(body_chars('## **一**\n\n花开了。\n\n## **二**\n\n她来了。'), 8)
        self.assertEqual(body_chars('## 一起回家\n\n花开了。'), 10)
        self.assertEqual(body_chars('## **1** 她来了。'), 11)

    def test_disabled_review_preserves_previous_no_request_behavior(self):
        with patch.object(story_config, 'STORY_EDITOR_ENABLE', False), \
             patch('web_drivers.generate_isolated') as request:
            result = editor.review_story('题', '她开门收下了花。')
        self.assertTrue(result['passed']); request.assert_not_called()

    def test_question_budget_keeps_reference_unchanged_in_all_material_modes(self):
        from story_prompt import build_story_prompt
        reference = '参考对白：每节600-800字，这句话属于原文，不是本题要求。'
        for material in ('sample', 'reference', 'recipe', 'recipe_and_reference'):
            with self.subTest(material=material), patch.object(story_config, 'STORY_MATERIAL_MODE', material):
                prompt, _ = build_story_prompt('写4000～5000字完整故事', reference,
                                               recipe={'genre': '日常'}, opening_auto=False)
            if material != 'recipe':
                self.assertIn(reference, prompt)
            self.assertIn('本题总篇幅', prompt)
            self.assertIn('各节篇幅按本题总预算分配', prompt)

    def test_cancel_propagates(self):
        from web_drivers.browser_pool import WorkflowCancelled
        with patch('web_drivers.generate_isolated', side_effect=WorkflowCancelled('停止')):
            with self.assertRaises(WorkflowCancelled): editor.review_story('题', '她开门收下了花。')


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        mode = patch.object(config, 'LLM_MODE', 'api'); mode.start(); self.addCleanup(mode.stop)
        self.workflow = WorkflowBase()
        self.story = '她把信放回桌上，决定明天再来。' * 50
        self.bad = {'status': 'revise', 'passed': False, 'issues': [{
            'kind': 'relationship', 'quotes': ['她把信放回桌上，决定明天再来。'],
            'problem': '人物突然和好', 'fix': '补足相处和回应'}], 'reason': ''}
        self.good = {'status': 'passed', 'passed': True, 'issues': [], 'reason': ''}

    def test_classic_revision_uses_existing_budget_and_feedback(self):
        with patch.object(self.workflow, 'generate_story', return_value=self.story) as generate, \
             patch('core.story_text.validate_story_format', return_value=(10, True, {})), \
             patch('core.story_editor.review_story', side_effect=[self.bad, self.good]):
            story, ok = self.workflow.generate_story_with_retry('题', '', max_attempts=2)
        self.assertTrue(ok)
        self.assertEqual(generate.call_count, 2)
        self.assertIn('补足相处和回应', generate.call_args.kwargs['feedback'])

    def test_unavailable_review_keeps_local_draft_and_stops_requests(self):
        with patch.object(self.workflow, 'generate_story', return_value=self.story) as generate, \
             patch('core.story_text.validate_story_format', return_value=(10, True, {})), \
             patch('core.story_editor.review_story', return_value=editor.unavailable('无结果')):
            story, ok = self.workflow.generate_story_with_retry('题', '', max_attempts=3)
        self.assertFalse(ok); self.assertEqual(story, self.story); generate.assert_called_once()

    def test_clean_editor_still_runs_when_originality_is_disabled(self):
        with patch.object(self.workflow, 'generate_clean_story', return_value=self.story) as generate, \
             patch.object(story_config, 'CLEAN_AUDIT_ENABLE', False), \
             patch('core.story_editor.review_story', side_effect=[self.bad, self.good]):
            story, audit = self.workflow.generate_clean_with_retry('题', '', max_attempts=2)
        self.assertTrue(audit['passed'])
        self.assertEqual(audit['editor_review']['status'], 'passed')
        self.assertIn('补足相处和回应', generate.call_args.kwargs['feedback'])

    def test_batch_rejects_editor_failed_draft_even_if_format_passes(self):
        def generated(materials, *args):
            materials[0].update(story=self.story, ok=False)
        material = {'title': '题', 'answer': '', 'url': 'https://www.zhihu.com/question/1'}
        with patch.object(story_config, 'BATCH_QUALITY_FIRST', False), \
             patch.object(story_config, 'STORY_EDITOR_ENABLE', True), \
             patch.object(story_config, 'ENABLE_PARAGRAPH_ANALYSIS', False), \
             patch.object(self.workflow, 'collect_materials_batch', return_value=[material]), \
             patch.object(self.workflow, '_ai_screen_questions', side_effect=lambda items, count: items), \
             patch.object(self.workflow, '_batch_generate_api_quality', side_effect=generated) as quality, \
             patch.object(self.workflow, '_batch_generate_api') as legacy, \
             patch('core.story_text.validate_story_format', return_value=(10, True, {})), \
             patch.object(self.workflow, 'publish') as publish:
            result = self.workflow.run_batch(1, 1)
        self.assertEqual(result, 0); quality.assert_called_once()
        legacy.assert_not_called(); publish.assert_not_called()

    def test_clean_unavailable_stops_with_failed_audit(self):
        with patch.object(self.workflow, 'generate_clean_story', return_value=self.story) as generate, \
             patch.object(story_config, 'CLEAN_AUDIT_ENABLE', False), \
             patch('core.story_editor.review_story', return_value=editor.unavailable('无结果')):
            story, audit = self.workflow.generate_clean_with_retry('题', '', max_attempts=3)
        self.assertFalse(audit['passed']); generate.assert_called_once()


if __name__ == '__main__': unittest.main()
