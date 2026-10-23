"""Login checks must distinguish browser failures from a signed-out account."""
import unittest
from unittest import mock

from web_drivers import browser_pool, deepseek


class DeepSeekLoginRecoveryTest(unittest.TestCase):
    def check(self, state=None, navigation_error=None, launch_error=None):
        page = mock.MagicMock()
        page.url = "https://chat.deepseek.com/"
        page.evaluate.return_value = state
        if navigation_error:
            page.goto.side_effect = navigation_error
        browser = mock.MagicMock()
        browser.context.cookies.return_value = []
        browser.context.new_page.return_value = page
        browser.__enter__.return_value = browser
        if launch_error:
            browser.__enter__.side_effect = launch_error
        with mock.patch.object(browser_pool, "profile_in_use", return_value=False), \
                mock.patch.object(browser_pool, "live_browsers", return_value=0), \
                mock.patch.object(browser_pool, "create_browser", return_value=browser), \
                mock.patch.object(deepseek.time, "monotonic", side_effect=[0, 0, 12, 12]):
            return deepseek.web_llm_logged_in()

    def test_ready_page_does_not_require_tracking_cookies(self):
        self.assertTrue(self.check({"ready": True, "signed_out": False}))

    def test_explicit_signin_page_is_signed_out(self):
        self.assertFalse(self.check({"ready": False, "signed_out": True}))

    def test_dom_ready_after_navigation_timeout_is_still_logged_in(self):
        from playwright.sync_api import TimeoutError
        self.assertTrue(self.check({"ready": True, "signed_out": False},
                                   navigation_error=TimeoutError("domcontentloaded timed out")))

    def test_incomplete_page_is_not_a_logout(self):
        with self.assertRaises(browser_pool.WebLoginCheckError):
            self.check({"ready": False, "signed_out": False})

    def test_browser_start_failure_is_not_a_logout(self):
        with self.assertRaises(browser_pool.WebLoginCheckError):
            self.check(launch_error=RuntimeError("profile startup failed"))

    def test_cancellation_is_not_a_logout(self):
        with self.assertRaises(browser_pool.WorkflowCancelled):
            self.check(launch_error=browser_pool.WorkflowCancelled("stopped"))


if __name__ == "__main__":
    unittest.main()
