"""Account checks bind the provider and preserve ambiguous/error outcomes."""
import unittest
from unittest.mock import MagicMock, patch

from web_drivers import account_status as accounts, browser_pool as pool


class AccountStatusTests(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock(url="https://www.doubao.com/chat/")
        self.context = MagicMock()
        self.context.cookies.return_value = []
        with accounts._lock:
            self.old_states = dict(accounts._states)
            accounts._states.clear()

    def tearDown(self):
        with accounts._lock:
            accounts._states.clear()
            accounts._states.update(self.old_states)

    def inspect(self, facts, name="Doubao"):
        with patch.object(pool, "safe_evaluate", return_value=facts):
            return accounts.inspect_account(name, self.context, self.page)["status"]

    def test_guest_composer_and_tracking_cookie_are_not_login(self):
        self.context.cookies.return_value = [{"name": "tracking", "value": "private", "domain": ".doubao.com"}]
        self.assertEqual(self.inspect({"composer": True}), "unknown")
        self.assertEqual(self.inspect({"composer": True, "login": True}), "logged_out")

    def test_valid_auth_cookie_needs_composer_and_no_login_prompt(self):
        self.context.cookies.return_value = [{"name": "sessionid", "value": "private", "domain": ".doubao.com"}]
        self.assertEqual(self.inspect({"composer": True}), "ready")
        self.assertEqual(self.inspect({"composer": False}), "unknown")
        self.assertEqual(self.inspect({"composer": True, "login": True}), "logged_out")
        self.context.cookies.return_value[0]["domain"] = ".evil-doubao.com"
        self.assertEqual(self.inspect({"composer": True}), "unknown")

    def test_restriction_and_challenge_take_priority_over_credentials(self):
        self.assertEqual(self.inspect({"composer": True, "credential": True, "restricted": True}), "restricted")
        self.assertEqual(self.inspect({"composer": True, "credential": True, "challenge": True}), "challenge")

    def test_only_exact_https_origins_are_trusted(self):
        for url in ("http://www.doubao.com/chat/", "https://www.doubao.com.evil.test/chat/", "about:blank"):
            self.page.url = url
            self.assertEqual(self.inspect({"composer": True, "credential": True}), "unknown")

    def test_deepseek_banned_route_is_not_logout(self):
        self.page.url = "https://chat.deepseek.com/banned"
        self.assertEqual(self.inspect({}, "DeepSeek"), "restricted")

    def test_busy_does_not_launch_or_claim_login(self):
        with patch.object(pool, "profile_in_use", return_value=True), patch.object(pool, "create_browser") as create:
            self.assertEqual(accounts.check_account("Doubao")["status"], "busy")
            with self.assertRaises(pool.WebLoginCheckError):
                accounts.logged_in("Doubao")
        create.assert_not_called()

    def test_login_opens_explicit_provider_and_saves_only_after_ready(self):
        browser = MagicMock()
        browser.__enter__.return_value = browser
        browser.context.new_page.return_value = self.page
        ready = {"status": "ready", "message": "ready"}
        with patch.object(pool, "profile_in_use", return_value=False), \
                patch.object(pool, "live_browsers", return_value=0), \
                patch.object(pool, "create_browser", return_value=browser), \
                patch.object(accounts, "inspect_account", return_value=ready):
            self.assertEqual(accounts.login_account("Doubao", timeout=2), (True, "ready"))
        self.assertEqual(self.page.goto.call_args.args[0], "https://www.doubao.com/chat/")
        browser.save_storage_state.assert_called_once()

    def test_restricted_login_stops_and_does_not_save_success(self):
        browser = MagicMock()
        browser.__enter__.return_value = browser
        browser.context.new_page.return_value = self.page
        with patch.object(pool, "profile_in_use", return_value=False), \
                patch.object(pool, "live_browsers", return_value=0), \
                patch.object(pool, "create_browser", return_value=browser), \
                patch.object(accounts, "inspect_account", return_value={"status": "restricted", "message": "restricted"}):
            self.assertEqual(accounts.login_account("Doubao", timeout=2), (False, "restricted"))
        browser.save_storage_state.assert_not_called()

    def test_unavailable_browser_is_unknown_not_logout(self):
        with patch.object(pool, "profile_in_use", return_value=False), \
                patch.object(pool, "live_browsers", return_value=0), \
                patch.object(pool, "create_browser", side_effect=RuntimeError("PRIVATE")):
            result = accounts.check_account("Doubao")
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("PRIVATE", str(result))

    def test_boolean_adapter_preserves_restricted_and_unknown(self):
        for status, error in (("restricted", pool.WebAccountRestricted), ("unknown", pool.WebLoginCheckError), ("challenge", pool.WebLoginCheckError)):
            with self.subTest(status=status), patch.object(accounts, "check_account", return_value={"status": status, "message": status}):
                with self.assertRaises(error):
                    accounts.logged_in("Doubao")

    def test_doubao_card_recovery_cannot_swallow_account_restriction(self):
        from web_drivers.doubao import DoubaoDriver
        driver = DoubaoDriver({})
        with patch.object(driver, "_extract_reply_text", side_effect=pool.WebAccountRestricted("受限")):
            with self.assertRaises(pool.WebAccountRestricted):
                driver._recover_card_delivery()

    def test_state_is_per_provider_and_contains_no_credentials(self):
        accounts.remember_status("Doubao", "ready")
        accounts.remember_status("DeepSeek", "restricted")
        self.assertEqual(accounts.get_account_status("Doubao")["status"], "ready")
        self.assertEqual(accounts.get_account_status("DeepSeek")["status"], "restricted")
        self.assertEqual(set(accounts.get_account_status("Kimi")), {"provider", "status", "message", "checked_at"})


if __name__ == "__main__":
    unittest.main()
