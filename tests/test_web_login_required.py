"""A real DeepSeek sign-out must stop the run instead of falling back."""

import unittest
from unittest import mock


class TestWebLoginRequiredPropagation(unittest.TestCase):
    def test_story_generation_does_not_fall_back_to_api(self):
        from web_drivers.browser_pool import WebLoginRequired
        from workflows.workflow_generation import GenerationMixin

        workflow = GenerationMixin()
        with mock.patch("config.LLM_MODE", "web"), \
                mock.patch.object(workflow, "_generate_web",
                                  side_effect=WebLoginRequired("signed out")), \
                mock.patch.object(workflow, "_generate_api") as api:
            with self.assertRaises(WebLoginRequired):
                workflow.generate_story("题目", "参考")
        api.assert_not_called()

    def test_question_screening_does_not_silently_keep_candidates(self):
        from web_drivers.browser_pool import WebLoginRequired
        import story_scoring

        candidates = [{"index": 1, "title": "题目", "answer": "回答"}]
        with mock.patch("config.LLM_MODE", "web"), \
                mock.patch("web_drivers.get_driver", return_value=mock.Mock(
                    generate=mock.Mock(side_effect=WebLoginRequired("signed out")))):
            with self.assertRaises(WebLoginRequired):
                story_scoring.screen_question_pool(candidates)

    def test_clean_generation_does_not_hide_runtime_logout(self):
        from web_drivers.browser_pool import WebLoginRequired
        from workflows.workflow_generation import GenerationMixin

        workflow = GenerationMixin()
        with mock.patch("config.LLM_MODE", "web"), \
                mock.patch("web_drivers.get_driver", return_value=mock.Mock(
                    generate=mock.Mock(side_effect=WebLoginRequired("signed out")))):
            with self.assertRaises(WebLoginRequired):
                workflow.generate_clean_story("题目", "参考")

    def test_runner_marks_runtime_logout_as_login_guide(self):
        from web_drivers.browser_pool import WebLoginRequired
        from webui.run_manager import TaskRunner, _RunSpec

        runner = TaskRunner()
        with mock.patch("config.LLM_MODE", "api"), \
                mock.patch("webui.run_manager._llm_configured", return_value=True), \
                mock.patch.object(runner, "_dispatch",
                                  side_effect=WebLoginRequired("signed out")), \
                mock.patch("web_drivers.reset_driver"), \
                mock.patch("web_drivers.browser_pool.close_shared_browser"):
            runner._run_task(_RunSpec(mode="single"))
        status = runner.status()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["guide_needed"], "deepseek_login")
        self.assertIn("重新登录", status["message"])

    def test_runner_preserves_restriction_instead_of_relogin_guide(self):
        from web_drivers.browser_pool import WebAccountRestricted
        from webui.run_manager import TaskRunner, _RunSpec

        for preflight in (True, False):
            runner = TaskRunner()
            with self.subTest(preflight=preflight), \
                    mock.patch("config.LLM_MODE", "web" if preflight else "api"), \
                    mock.patch("webui.run_manager._llm_configured", return_value=True), \
                    mock.patch("web_drivers.web_llm_logged_in", side_effect=WebAccountRestricted("账号受限")), \
                    mock.patch.object(runner, "_dispatch", side_effect=WebAccountRestricted("账号受限")), \
                    mock.patch("web_drivers.reset_driver"), \
                    mock.patch("web_drivers.browser_pool.close_shared_browser"):
                runner._run_task(_RunSpec(mode="single"))
            status = runner.status()
            self.assertEqual(status["guide_needed"], "web_account_restricted")
            self.assertIn("账号受限", status["message"])
            self.assertNotIn("重新登录", status["message"])

    def test_real_runner_dispatch_stops_multiround_workflow_after_logout(self):
        from web_drivers.browser_pool import WebLoginRequired
        from webui.run_manager import TaskRunner, _RunSpec

        for mode in ("single", "clean"):
            with self.subTest(mode=mode):
                workflow = mock.Mock()
                getattr(workflow, "run_" + mode).side_effect = WebLoginRequired(
                    "DeepSeek 网页登录已失效")
                runner = TaskRunner()
                with mock.patch("config.LLM_MODE", "api"), \
                        mock.patch("webui.run_manager._llm_configured", return_value=True), \
                        mock.patch("workflows.zhihu.ZhihuWorkflow", return_value=workflow), \
                        mock.patch("web_drivers.reset_driver"), \
                        mock.patch("web_drivers.browser_pool.close_shared_browser"):
                    runner._run_task(_RunSpec(mode=mode, rounds=3))
                self.assertEqual(getattr(workflow, "run_" + mode).call_count, 1)
                self.assertEqual(runner.status()["guide_needed"], "deepseek_login")

    def test_parallel_poll_checks_logout_before_marking_done(self):
        from web_drivers.browser_pool import WebLoginRequired
        from web_drivers.parallel import ParallelWebRunner, SlotState

        driver = mock.Mock()
        driver.config = {"stable_count": 2, "max_wait": 600}
        driver._raise_if_logged_out.side_effect = WebLoginRequired("signed out")
        slot = SlotState(0, driver)
        slot.status = SlotState.GENERATING
        with self.assertRaises(WebLoginRequired):
            ParallelWebRunner(num_slots=1)._poll(slot)
        driver._stop_button_present.assert_not_called()

    def test_parallel_collect_propagates_logout(self):
        from web_drivers.browser_pool import WebLoginRequired
        from web_drivers.parallel import ParallelWebRunner, SlotState

        driver = mock.Mock()
        driver.read_result.side_effect = WebLoginRequired("signed out")
        with self.assertRaises(WebLoginRequired):
            ParallelWebRunner(num_slots=1)._collect(SlotState(0, driver))

    def test_batch_parallel_aborts_and_tears_down_on_logout(self):
        from web_drivers.browser_pool import WebLoginRequired
        from workflows.workflow_batch import BatchGenerationMixin

        workflow = BatchGenerationMixin()
        runner = mock.Mock()
        runner.run.side_effect = WebLoginRequired("signed out")
        materials = [{"index": 1, "title": "题目", "answer": "参考"}]
        with mock.patch("web_drivers.parallel.ParallelWebRunner",
                        return_value=runner), \
                mock.patch("llm_api.build_story_prompt",
                           return_value=("prompt", "mode")), \
                mock.patch("llm_api._load_author_profile_or_none",
                           return_value=None), \
                mock.patch("web_drivers.reset_driver"):
            with self.assertRaises(WebLoginRequired):
                workflow._batch_generate_web_parallel(
                    materials, {"parallel_tabs": 2,
                                "consecutive_fail_threshold": 2,
                                "scan_interval": 0})
        runner.teardown.assert_called_once_with()

    def test_parallel_setup_registers_logout_page_for_teardown(self):
        from web_drivers.browser_pool import WebLoginRequired
        from web_drivers.parallel import ParallelWebRunner

        driver = mock.Mock()
        driver.new_chat.side_effect = WebLoginRequired("signed out")
        with mock.patch("web_drivers.create_driver", return_value=driver):
            runner = ParallelWebRunner(num_slots=1)
            with self.assertRaises(WebLoginRequired):
                runner.setup()
            runner.teardown()
        driver.close_session.assert_called_once_with()

    def test_parallel_reset_and_replacement_stop_on_logout(self):
        from web_drivers.browser_pool import WebLoginRequired
        from web_drivers.parallel import ParallelWebRunner, SlotState

        for method in ("_do_reset", "_replace_slot"):
            with self.subTest(method=method):
                driver = mock.Mock()
                driver.new_chat.side_effect = WebLoginRequired("signed out")
                runner = ParallelWebRunner(num_slots=1)
                slot = SlotState(0, driver)
                with mock.patch("web_drivers.create_driver", return_value=driver):
                    with self.assertRaises(WebLoginRequired):
                        getattr(runner, method)(slot)
                driver.close_session.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
