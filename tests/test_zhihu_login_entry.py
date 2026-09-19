# -*- coding: utf-8 -*-
"""知乎登录入口回归（2026-09-19）。

背景（用户提问）：装好之后在界面里找不到登录知乎的入口（只有首启引导里有），
而且「有 cookie」被当成「已登录」——服务端把会话登出后软件一路绿灯，直到
看板/草稿箱刷新失败。本次新增：设置弹窗「知乎账号」区块（检查登录状态 / 重新
登录知乎）+ 真实检查端点 /api/setup/zhihu-check。
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from applications.zhihu_story import browser_adapter as ba
from webui import browser_tasks
from webui import server


class _FakePage:
    def __init__(self, url):
        self.url = url
        self.goto_calls = []

    def goto(self, url, **kw):
        self.goto_calls.append(url)


class _FakeBrowser:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VerifyZhihuLoginTest(unittest.TestCase):
    def _run_with_page(self, page):
        with mock.patch.object(ba, "ZhihuBrowser",
                               lambda headless=True: _FakeBrowser(page)), \
                mock.patch.object(ba.time, "sleep", lambda s: None):
            return ba.verify_zhihu_login(headless=True)

    def test_signin_page_means_logged_out(self):
        page = _FakePage("https://www.zhihu.com/signin?next=%2F")
        ok, detail = self._run_with_page(page)
        self.assertFalse(ok)
        self.assertIn("登出", detail)
        self.assertEqual(page.goto_calls, ["https://www.zhihu.com/"])

    def test_normal_page_means_logged_in(self):
        page = _FakePage("https://www.zhihu.com/")
        ok, detail = self._run_with_page(page)
        self.assertTrue(ok)
        self.assertIn("有效", detail)

    def test_exception_is_reported_not_raised(self):
        with mock.patch.object(ba, "ZhihuBrowser",
                               side_effect=RuntimeError("edge 启动失败")):
            ok, detail = ba.verify_zhihu_login()
        self.assertFalse(ok)
        self.assertIn("检查失败", detail)


class ZhihuCheckEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def setUp(self):
        browser_tasks.clear_zhihu_login_stale()

    def tearDown(self):
        browser_tasks.clear_zhihu_login_stale()

    def test_logged_out_marks_stale(self):
        with mock.patch.object(browser_tasks, "browser_busy", return_value=[]), \
                mock.patch.object(ba, "verify_zhihu_login",
                                  return_value=(False, "已登出（页面跳登录页）")):
            r = self.client.post("/api/setup/zhihu-check")
        d = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertFalse(d["logged_in"])
        self.assertTrue(browser_tasks.zhihu_login_stale()["stale"])
        self.assertIn("登出", browser_tasks.zhihu_login_stale()["reason"])

    def test_logged_in_clears_stale(self):
        browser_tasks.mark_zhihu_login_stale("上一轮的失效")
        with mock.patch.object(browser_tasks, "browser_busy", return_value=[]), \
                mock.patch.object(ba, "verify_zhihu_login",
                                  return_value=(True, "登录态有效")):
            d = self.client.post("/api/setup/zhihu-check").json()
        self.assertTrue(d["logged_in"])
        self.assertFalse(browser_tasks.zhihu_login_stale()["stale"])

    def test_busy_returns_without_browser(self):
        with mock.patch.object(browser_tasks, "browser_busy",
                               return_value=["看板刷新"]), \
                mock.patch.object(ba, "verify_zhihu_login",
                                  side_effect=AssertionError("不该启动浏览器")):
            d = self.client.post("/api/setup/zhihu-check").json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["status"], "busy")
        self.assertIn("看板刷新", d["message"])


class LoginEntryUiTest(unittest.TestCase):
    def _src(self, rel):
        with open(rel, encoding="utf-8") as f:
            return f.read()

    def test_settings_modal_has_zhihu_account_block(self):
        html = self._src("webui/static/index.html")
        for needle in ("zhihuLoginState", "btnZhihuCheck", "btnZhihuRelogin",
                       "知乎账号"):
            self.assertIn(needle, html, needle)
        js = self._src("webui/static/app.js")
        for needle in ("/api/setup/zhihu-check", "/api/setup/zhihu-login",
                       "refreshZhihuLoginState()", "checkZhihuLogin",
                       "reloginZhihu"):
            self.assertIn(needle, js, needle)
        # 打开设置弹窗时刷新一次状态（否则永远显示「检测中…」）
        idx = js.index('$("btnSetup").addEventListener')
        self.assertIn("refreshZhihuLoginState()", js[idx:idx + 400])

    def test_setup_status_exposes_stale_flag(self):
        src = self._src("webui/api_setup.py")
        self.assertIn("zhihu_login_stale", src)
        self.assertIn("/api/setup/zhihu-check", src)


if __name__ == "__main__":
    unittest.main()
