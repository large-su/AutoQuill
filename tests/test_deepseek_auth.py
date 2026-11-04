"""Authentication evidence is terminal, bounded and never contains credentials."""
import unittest
from unittest import mock

from web_drivers.browser_pool import WebLoginRequired
from web_drivers.deepseek_auth import DeepSeekAuthObserver


class DeepSeekAuthTest(unittest.TestCase):
    def setUp(self):
        self.page = mock.Mock()
        self.observer = DeepSeekAuthObserver(self.page)

    def emit(self, payload=None, status=200, content_type="application/json",
             url="https://chat.deepseek.com/api/v0/chat/completion"):
        response = mock.Mock(status=status, url=url)
        response.headers = {"content-type": content_type}
        response.json.return_value = payload
        request = mock.Mock()
        request.response.return_value = response
        event, callback = self.page.on.call_args.args
        self.assertEqual(event, "requestfinished")
        callback(request)
        return response

    def test_invalid_token_raises_before_page_redirect_and_redacts_payload(self):
        with self.assertLogs("web_drivers.deepseek_auth", "WARNING") as logs:
            self.emit({"code": 40003, "msg": "SECRET", "token": "SECRET"},
                      url="https://chat.deepseek.com/api/v0/chat/completion?token=SECRET")
        with self.assertRaises(WebLoginRequired) as caught:
            self.observer.raise_if_rejected()
        self.assertIn("40003", str(caught.exception))
        self.assertNotIn("SECRET", " ".join(logs.output) + str(caught.exception))
        self.assertIn("/api/v0/chat/completion", " ".join(logs.output))

    def test_nested_business_missing_token_is_terminal(self):
        self.emit({"code": 0, "data": {"biz_code": 40002, "biz_msg": "private"}})
        with self.assertRaises(WebLoginRequired):
            self.observer.raise_if_rejected()

    def test_unauthorized_status_does_not_require_json(self):
        response = self.emit(status=401, content_type="text/html")
        response.json.assert_not_called()
        with self.assertRaises(WebLoginRequired):
            self.observer.raise_if_rejected()

    def test_network_challenge_rate_limit_and_server_error_are_not_logout(self):
        for status in (200, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.emit({"code": 0}, status=status)
                self.observer.raise_if_rejected()

    def test_stream_is_not_read_and_unrelated_origins_are_ignored(self):
        stream = self.emit(content_type="text/event-stream", payload={"code": 40003})
        stream.json.assert_not_called()
        for url in ("https://chat.deepseek.com.evil.test/api/v0/chat/completion",
                    "http://chat.deepseek.com/api/v0/chat/completion",
                    "https://chat.deepseek.com:8443/api/v0/chat/completion",
                    "https://www.zhihu.com/api/v0/chat/completion",
                    "https://chat.deepseek.com/static/example.json"):
            response = self.emit({"code": 40003}, url=url)
            response.json.assert_not_called()
        self.observer.raise_if_rejected()

    def test_unreadable_response_does_not_become_logout(self):
        response = mock.Mock(url="https://chat.deepseek.com/api/v0/users/current", status=200)
        response.headers = {"content-type": "application/json"}
        response.json.side_effect = ValueError("SECRET")
        self.page.on.call_args.args[1](mock.Mock(response=mock.Mock(return_value=response)))
        self.observer.raise_if_rejected()

    def test_rejection_logs_once_and_a_new_page_starts_clean(self):
        with self.assertLogs("web_drivers.deepseek_auth", "WARNING") as logs:
            self.emit({"code": 40003})
            self.emit({"code": 40003})
        self.assertEqual(len(logs.output), 1)
        DeepSeekAuthObserver(mock.Mock()).raise_if_rejected()


if __name__ == "__main__":
    unittest.main()
