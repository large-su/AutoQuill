"""Regression checks for prompt DeepSeek logout detection."""
import json
import shutil
import subprocess
import unittest
from unittest import mock

from web_drivers import deepseek
from web_drivers.browser_pool import WebLoginRequired


class DeepSeekSessionExpiryTest(unittest.TestCase):
    def _driver(self, url):
        driver = deepseek.DeepSeekDriver({
            "max_wait": 600,
            "poll_interval": 4,
            "stable_count": 2,
        })
        page = mock.MagicMock()
        page.url = url
        driver._auth_observer = mock.MagicMock()
        driver._page_instance = mock.MagicMock(return_value=page)
        return driver, page

    def test_wait_complete_stops_immediately_on_sign_in_page(self):
        driver, page = self._driver("https://chat.deepseek.com/sign_in")
        with self.assertRaises(WebLoginRequired):
            driver.wait_complete(max_wait=600)
        page.wait_for_timeout.assert_not_called()

    def test_read_result_does_not_return_partial_reply_after_logout(self):
        driver, _ = self._driver("https://chat.deepseek.com/sign_in")
        with self.assertRaises(WebLoginRequired):
            driver.read_result()

    def test_other_page_is_not_classified_as_logout(self):
        driver, page = self._driver("https://chat.deepseek.com/")
        driver._raise_if_logged_out(page)
        driver._auth_observer.raise_if_rejected.assert_called_once_with()

    def test_new_page_does_not_inherit_old_auth_rejection(self):
        driver = deepseek.DeepSeekDriver({})
        old = mock.Mock()
        old.raise_if_rejected.side_effect = WebLoginRequired("previous login")
        driver._auth_observer = old
        driver._page = mock.Mock(url="https://chat.deepseek.com/")
        driver._page.is_closed.return_value = False
        driver._raise_if_logged_out()
        old.raise_if_rejected.assert_not_called()
        self.assertIs(driver._auth_observer.page, driver._page)

    @unittest.skipUnless(shutil.which("node"), "Node is required for login JS regression")
    def test_login_state_requires_inner_token_value(self):
        cases = [None, 'null', 'undefined', '{}', '{broken',
                 '{"value":null}', '{"value":""}', '{"value":"  "}',
                 '{"value":123}', '{"value":"fixture-token"}']
        script = """
        const {js,cases} = JSON.parse(require('fs').readFileSync(0,'utf8'));
        global.location = {origin:'https://chat.deepseek.com',pathname:'/'};
        global.document = {querySelectorAll:()=>[{offsetWidth:100,offsetHeight:20}]};
        const check = eval('(' + js + ')');
        console.log(JSON.stringify(cases.map(value=>{
          global.localStorage={getItem:()=>value}; return check().ready;
        })));
        """
        result = subprocess.run([shutil.which("node"), "-e", script],
                                input=json.dumps({"js":deepseek._LOGIN_STATE_JS,"cases":cases}),
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), [False] * 9 + [True])


if __name__ == "__main__":
    unittest.main()
