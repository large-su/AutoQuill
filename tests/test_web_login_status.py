"""网页登录状态的异常分类：无法检测不等同于已登出。"""

import copy
import time
import unittest
from unittest import mock

from web_drivers.browser_pool import WebLoginCheckError, WorkflowCancelled
from webui import api_setup as setup_mod
from webui import run_manager


class TestWebLoginStatus(unittest.TestCase):
    def setUp(self):
        with setup_mod._web_llm_cache_lock:
            self._old_cache = copy.deepcopy(setup_mod._web_llm_cache)
            self._old_check_errors = copy.deepcopy(
                setup_mod._web_llm_check_errors)
            setup_mod._web_llm_cache.clear()
            setup_mod._web_llm_check_errors.clear()
        self._old_thread = setup_mod._login_thread
        self._old_kind = setup_mod._login_kind
        self._old_error = setup_mod._login_error
        self._old_guide = setup_mod.runner.guide_needed
        setup_mod._login_thread = None
        setup_mod._login_kind = ""
        setup_mod._login_error = ""

    def tearDown(self):
        thread = setup_mod._login_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        setup_mod._login_thread = self._old_thread
        setup_mod._login_kind = self._old_kind
        setup_mod._login_error = self._old_error
        setup_mod.runner.guide_needed = self._old_guide
        with setup_mod._web_llm_cache_lock:
            setup_mod._web_llm_cache.clear()
            setup_mod._web_llm_cache.update(self._old_cache)
            setup_mod._web_llm_check_errors.clear()
            setup_mod._web_llm_check_errors.update(self._old_check_errors)

    def test_wrapper_keeps_check_error_visible(self):
        import web_drivers

        with mock.patch.object(
                web_drivers, "_current_driver_module",
                return_value=mock.Mock(
                    web_llm_logged_in=mock.Mock(
                        side_effect=WebLoginCheckError("browser unavailable")))):
            with self.assertRaises(WebLoginCheckError):
                web_drivers.web_llm_logged_in()

        with mock.patch.object(
                web_drivers, "_current_driver_module",
                return_value=mock.Mock(
                    web_llm_logged_in=mock.Mock(
                        side_effect=WorkflowCancelled("cancelled")))):
            with self.assertRaises(WorkflowCancelled):
                web_drivers.web_llm_logged_in()

        with mock.patch(
                "importlib.import_module",
                return_value=mock.Mock(
                    web_llm_logged_in=mock.Mock(
                        side_effect=WebLoginCheckError("browser unavailable")))):
            with self.assertRaises(WebLoginCheckError):
                setup_mod._web_llm_logged_in_for("DeepSeek")

    def test_check_error_preserves_last_login_fact_and_is_short_cached(self):
        key = "DeepSeek"
        original = {"ts": 0.0, "ok": True}
        setup_mod._web_llm_cache[key] = original.copy()
        with mock.patch.object(setup_mod, "_browser_lock_free", return_value=True):
            with mock.patch.object(
                    setup_mod, "_web_llm_logged_in_for",
                    side_effect=WebLoginCheckError("browser unavailable")) as check:
                self.assertTrue(setup_mod._web_llm_logged_in_cached(key))
                self.assertTrue(setup_mod._web_llm_logged_in_cached(key))
        self.assertEqual(check.call_count, 1)
        self.assertEqual(setup_mod._web_llm_cache[key], original)
        self.assertEqual(
            setup_mod._web_llm_check_error(key),
            "网页登录状态暂时无法检测，请稍后重试。")

    def test_slow_check_error_is_cached_from_check_completion(self):
        with mock.patch.object(setup_mod, "_browser_lock_free", return_value=True):
            with mock.patch.object(
                    setup_mod, "_web_llm_logged_in_for",
                    side_effect=WebLoginCheckError("browser unavailable")):
                with mock.patch.object(
                        setup_mod.time, "time", side_effect=[100.0, 128.0, 128.0]):
                    self.assertFalse(setup_mod._web_llm_logged_in_cached("DeepSeek"))
                    self.assertEqual(
                        setup_mod._web_llm_check_error("DeepSeek"),
                        "网页登录状态暂时无法检测，请稍后重试。")

    def test_successful_recheck_clears_only_its_driver_error(self):
        now = time.time()
        setup_mod._web_llm_check_errors.update({
            "DeepSeek": {"ts": 0.0},
            "Doubao": {"ts": now},
        })
        with mock.patch.object(setup_mod, "_browser_lock_free", return_value=True):
            with mock.patch.object(
                    setup_mod, "_web_llm_logged_in_for", return_value=True):
                self.assertTrue(setup_mod._web_llm_logged_in_cached("DeepSeek"))
        self.assertIsNone(setup_mod._web_llm_check_error("DeepSeek"))
        self.assertEqual(
            setup_mod._web_llm_check_error("Doubao"),
            "网页登录状态暂时无法检测，请稍后重试。")

    def test_login_failure_keeps_kind_after_thread_finishes(self):
        with mock.patch(
                "applications.zhihu_story.browser_adapter.EDGE_PATH",
                "C:/fake/msedge.exe"):
            setup_mod._start_login_thread(
                "deepseek", lambda: (False, "窗口已关闭"), "网页版登录")
        setup_mod._login_thread.join(timeout=2)
        self.assertFalse(setup_mod._login_thread.is_alive())
        self.assertEqual(setup_mod._login_kind, "deepseek")
        self.assertEqual(setup_mod._login_error, "窗口已关闭")

    def test_status_exposes_check_diagnostic_without_changing_bool(self):
        with mock.patch(
                "applications.zhihu_story.browser_adapter.EDGE_PATH",
                "C:/fake/msedge.exe"):
            with mock.patch.object(
                    setup_mod, "_web_llm_logged_in_cached", return_value=False):
                with mock.patch.object(
                        setup_mod, "_web_llm_check_error",
                        return_value="网页登录状态暂时无法检测，请稍后重试。"):
                    status = setup_mod.api_setup_status()
        self.assertFalse(status["web_llm_logged_in"])
        self.assertEqual(
            status["web_llm_check_error"],
            "网页登录状态暂时无法检测，请稍后重试。")

    def test_successful_login_updates_only_current_driver_fact(self):
        now = time.time()
        setup_mod._web_llm_check_errors.update({
            "DeepSeek": {"ts": now},
            "Doubao": {"ts": now},
        })
        with mock.patch(
                "applications.zhihu_story.browser_adapter.EDGE_PATH",
                "C:/fake/msedge.exe"):
            setup_mod._start_login_thread(
                "deepseek", lambda: (True, "ok"), "网页版登录",
                web_driver="DeepSeek")
        setup_mod._login_thread.join(timeout=2)
        self.assertTrue(setup_mod._web_llm_cache["DeepSeek"]["ok"])
        self.assertNotIn("DeepSeek", setup_mod._web_llm_check_errors)
        self.assertIn("Doubao", setup_mod._web_llm_check_errors)

    def test_task_runner_check_error_does_not_request_login_guide(self):
        import config

        runner = run_manager.TaskRunner()
        spec = run_manager._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "web"):
            with mock.patch("web_drivers.reset_driver"):
                with mock.patch(
                        "web_drivers.web_llm_logged_in",
                        side_effect=WebLoginCheckError("browser unavailable")):
                    with mock.patch.object(runner, "_dispatch") as dispatch:
                        runner._run_task(spec)
        self.assertEqual(runner.state, "error")
        self.assertIsNone(runner.guide_needed)
        self.assertIn("暂不可用", runner.message)
        dispatch.assert_not_called()

