"""Focused tests for independent web-provider selection and account routes."""
import unittest
from unittest import mock

from fastapi import HTTPException

from webui import api_settings, api_setup


class WebProviderRoutesTest(unittest.TestCase):
    def setUp(self):
        self.providers = {
            "DeepSeek": {"url": "https://chat.deepseek.com"},
            "Doubao": {"url": "https://www.doubao.com"},
            "Kimi": {"url": "https://kimi.moonshot.cn"},
        }

    def test_switch_unlogged_provider_persists_without_login_precheck(self):
        calls = []
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch("config.WEB_DRIVER_NAME", "DeepSeek"), \
                mock.patch.object(api_settings.runner, "status", return_value={
                    "state": "idle", "worker_alive": False}), \
                mock.patch("webui.browser_tasks.browser_busy", return_value=[]), \
                mock.patch("web_drivers.browser_pool.profile_in_use", return_value=False), \
                mock.patch("web_drivers.browser_pool.live_browsers", return_value=0), \
                mock.patch("web_drivers.reset_driver", side_effect=lambda: calls.append("reset")), \
                mock.patch("config.set_runtime_web_driver",
                           side_effect=lambda name: calls.append(name) or {"web_driver": name}):
            result = api_settings.api_set_web_driver(api_settings._WebDriverSpec(name="Doubao"))
        self.assertEqual(result["effective"]["web_driver"], "Doubao")
        self.assertEqual(calls, ["reset", "Doubao"])

    def test_login_routes_to_named_provider(self):
        flow = {}
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch("config.WEB_DRIVER_NAME", "DeepSeek"), \
                mock.patch("applications.zhihu_story.browser_adapter.EDGE_PATH", "edge.exe"), \
                mock.patch.object(api_setup.runner, "status", return_value={
                    "state": "idle", "worker_alive": False}), \
                mock.patch("webui.browser_tasks.browser_busy", return_value=[]), \
                mock.patch("web_drivers.browser_pool.profile_in_use", return_value=False), \
                mock.patch("web_drivers.browser_pool.live_browsers", return_value=0), \
                mock.patch("web_drivers.account_status.login_account",
                           return_value=(True, "ok")) as login, \
                mock.patch.object(api_setup, "_start_login_thread",
                                  side_effect=lambda *a, **kw: (
                                      flow.update(call=(a, kw)), a[1]())[0]):
            result = api_setup.api_setup_web_login(
                api_setup._WebLoginSpec(name="Doubao"))
        self.assertEqual(result["driver"], "Doubao")
        args, kwargs = flow["call"]
        self.assertEqual(kwargs["web_driver"], "Doubao")
        login.assert_called_once_with("Doubao", timeout=300)

    def test_login_without_body_uses_current_provider(self):
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch("config.WEB_DRIVER_NAME", "DeepSeek"), \
                mock.patch("applications.zhihu_story.browser_adapter.EDGE_PATH", "edge.exe"), \
                mock.patch.object(api_setup.runner, "status", return_value={
                    "state": "idle", "worker_alive": False}), \
                mock.patch("webui.browser_tasks.browser_busy", return_value=[]), \
                mock.patch("web_drivers.browser_pool.profile_in_use", return_value=False), \
                mock.patch("web_drivers.browser_pool.live_browsers", return_value=0), \
                mock.patch("webui.api_setup._start_login_thread") as start:
            result = api_setup.api_setup_web_login()
        self.assertEqual(result["driver"], "DeepSeek")
        self.assertEqual(start.call_args.kwargs["web_driver"], "DeepSeek")

    def test_check_kimi_does_not_change_current_provider(self):
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch("config.WEB_DRIVER_NAME", "DeepSeek"), \
                mock.patch.object(api_setup.runner, "status", return_value={
                    "state": "idle", "worker_alive": False}), \
                mock.patch("webui.browser_tasks.browser_busy", return_value=[]), \
                mock.patch("web_drivers.account_status.check_account",
                           return_value={"status": "ready", "message": "ok",
                                          "checked_at": 1}) as check:
            result = api_setup.api_setup_web_account_check(
                api_setup._WebAccountSpec(name="Kimi"))
        self.assertEqual(result["provider"], "Kimi")
        self.assertEqual(result["status"], "ready")
        check.assert_called_once_with("Kimi")

    def test_switch_busy_returns_409(self):
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch("config.WEB_DRIVER_NAME", "DeepSeek"), \
                mock.patch.object(api_settings.runner, "status", return_value={
                    "state": "running", "worker_alive": True}):
            with self.assertRaises(HTTPException) as caught:
                api_settings.api_set_web_driver(
                    api_settings._WebDriverSpec(name="Doubao"))
        self.assertEqual(caught.exception.status_code, 409)

    def test_status_uses_shared_account_cache(self):
        with mock.patch("web_drivers.account_status.get_account_status",
                        return_value={"provider": "DeepSeek", "status": "ready",
                                       "message": "cached", "checked_at": 2}):
            result = api_setup._web_account_status_for("DeepSeek")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["message"], "cached")

    def test_independent_check_syncs_only_terminal_boolean(self):
        api_setup._web_llm_cache.pop("Kimi", None)
        with mock.patch.object(api_setup.runner, "status", return_value={
                    "state": "idle", "worker_alive": False}), \
                mock.patch("webui.browser_tasks.browser_busy", return_value=[]), \
                mock.patch("web_drivers.account_status.get_account_status",
                           return_value={"provider": "Kimi", "status": "unknown",
                                          "message": "暂无法确认", "checked_at": 1}), \
                mock.patch("web_drivers.account_status.check_account",
                           return_value={"provider": "Kimi", "status": "unknown",
                                         "message": "暂无法确认", "checked_at": 2}):
            api_setup._web_llm_cache["Kimi"] = {"ts": 1, "ok": True}
            result = api_setup.api_setup_web_account_check(
                api_setup._WebAccountSpec(name="Kimi"))
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(api_setup._web_llm_cache["Kimi"]["ok"])


if __name__ == "__main__":
    unittest.main()
