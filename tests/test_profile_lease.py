# -*- coding: utf-8 -*-
"""profile 生命周期租约 + 浏览器启动健壮性（2026-09-26 安装版事故回归）。

事故链（真机日志实证）：
  1. 自动化常驻后，同一个 user-data-dir 被多个 Chromium 实例并发使用（任务 +
     网页版登录检查 + 知乎登录态检查 + 登录引导）→ 第二个实例必然以
     exitCode=21「profile in use」失败；
  2. 失败路径的「清理残留进程」按命令行匹配 taskkill，把**正在干活**的浏览器
     一起杀掉 → 任务中断、cookie 来不及落盘（会话 cookie 丢失 = 知乎判定登出）
     → 用户看到「隔天/重启后就要重新登录」，还会形成「越杀越起不来」的死循环；
  3. 启动彻底失败时没有 stop() Playwright 驱动 → sync_playwright() 在该线程里
     留下一个跑着的事件循环，之后任何检查都报
     「It looks like you are using Playwright Sync API inside the asyncio loop」，
     把 FastAPI 线程池的 worker 逐个毒化。
"""
import unittest
from unittest import mock

import web_drivers.browser_pool as bp


class ProfileLeaseTest(unittest.TestCase):

    def tearDown(self):
        while bp.profile_in_use():      # 别把租约漏给下一个用例
            bp.release_profile()
        while bp.live_browsers() > 0:
            bp.note_browser_closed()

    def test_acquire_release_and_peek(self):
        self.assertFalse(bp.profile_in_use())
        self.assertTrue(bp.acquire_profile(0, purpose="t"))
        self.assertTrue(bp.profile_in_use())
        self.assertFalse(bp.acquire_profile(0))     # 第二个申请者拿不到
        bp.release_profile()
        self.assertFalse(bp.profile_in_use())
        bp.release_profile()                        # 幂等：没持有也不炸
        self.assertFalse(bp.profile_in_use())

    def test_live_counter(self):
        self.assertEqual(bp.live_browsers(), 0)
        bp.note_browser_opened()
        bp.note_browser_opened()
        self.assertEqual(bp.live_browsers(), 2)
        bp.note_browser_closed()
        bp.note_browser_closed()
        bp.note_browser_closed()                    # 多减不出负数
        self.assertEqual(bp.live_browsers(), 0)


class _FakeDriver:
    """假 Playwright 驱动：记录 stop 是否被调用 / 是否让启动失败。"""

    def __init__(self, fail_launch=False):
        self.stopped = False
        self.fail_launch = fail_launch
        self.chromium = self

    def launch_persistent_context(self, **kw):
        if self.fail_launch:
            raise RuntimeError(
                ": Target page, context or browser has been closed")
        page = mock.MagicMock()
        return mock.MagicMock(pages=[page])

    def stop(self):
        self.stopped = True


class BrowserStartRobustnessTest(unittest.TestCase):

    def tearDown(self):
        while bp.profile_in_use():
            bp.release_profile()
        while bp.live_browsers() > 0:
            bp.note_browser_closed()

    def _browser(self):
        import tempfile
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        b = ZhihuBrowser.__new__(ZhihuBrowser)      # 不跑 __init__（避开真实路径）
        b.user_data_dir = tempfile.mkdtemp(prefix="aq_lease_")   # 真实存在的临时目录
        b.storage_state = b.user_data_dir + "/state.json"
        b.headless = True
        b.context = None
        b.page = None
        b._lease_held = False
        b.lease_timeout = 0
        return b

    def test_second_instance_is_refused_not_raced(self):
        """租约被占：直接抛 ProfileBusy，绝不去并发启动第二个 Chromium。"""
        bp.acquire_profile(0, purpose="holder")
        b = self._browser()
        with mock.patch(
                "applications.zhihu_story.browser_session.EDGE_PATH", "msedge.exe"):
            with self.assertRaises(bp.ProfileBusy):
                b.start()
        self.assertFalse(getattr(b, "_lease_held", False))

    def test_failure_stops_driver_and_releases_lease(self):
        """启动彻底失败：必须停掉 Playwright 驱动（否则毒化所在线程）+ 释放租约。"""
        driver = _FakeDriver(fail_launch=True)
        fake_pw = mock.MagicMock()
        fake_pw.start.return_value = driver
        b = self._browser()
        with mock.patch("playwright.sync_api.sync_playwright",
                        return_value=fake_pw), \
                mock.patch("applications.zhihu_story.browser_session.EDGE_PATH",
                           "msedge.exe"), \
                mock.patch("applications.zhihu_story.browser_session._kill_stale_profile_processes"), \
                mock.patch("applications.zhihu_story.browser_session.time.sleep"):
            with self.assertRaises(RuntimeError):
                b.start()
        self.assertTrue(driver.stopped, "失败路径必须 stop() 驱动")
        self.assertFalse(bp.profile_in_use(), "失败后必须释放租约")
        self.assertEqual(bp.live_browsers(), 0)

    def test_close_saves_login_state_and_releases(self):
        """关闭时：登录态写回快照（会话 cookie 丢了的兜底）+ 释放租约。"""
        bp.acquire_profile(0, purpose="t")
        bp.note_browser_opened()
        b = self._browser()
        b._lease_held = True
        ctx = mock.MagicMock()
        b.context = ctx
        b.is_logged_in = lambda: True
        b.save_storage_state = mock.MagicMock()
        b.close()
        b.save_storage_state.assert_called_once()
        ctx.close.assert_called_once()
        self.assertFalse(bp.profile_in_use())
        self.assertEqual(bp.live_browsers(), 0)

    def test_no_kill_while_own_browser_alive(self):
        """有自己人活着时绝不 taskkill（旧实现会把正在干活的浏览器杀掉）。"""
        from applications.zhihu_story import browser_session as bs
        bp.note_browser_opened()
        try:
            with mock.patch("desktop_utils.run_process_silent") as run:
                bs._kill_stale_profile_processes("X:/tmp/profile")
            run.assert_not_called()
        finally:
            bp.note_browser_closed()


class BusyIsNotLoggedOutTest(unittest.TestCase):
    """忙 ≠ 未登录：忙碌时的检查绝不能把登录态标成失效，也不该硬起第二个实例。"""

    def tearDown(self):
        while bp.profile_in_use():
            bp.release_profile()

    def test_verify_zhihu_login_returns_none_when_busy(self):
        from applications.zhihu_story.browser_adapter import verify_zhihu_login
        bp.acquire_profile(0, purpose="task")
        logged_in, detail = verify_zhihu_login(headless=True)
        self.assertIsNone(logged_in, "忙时应返回 None（未判定），而不是 False（失效）")
        self.assertIn("暂缓", detail)

    def test_check_endpoint_does_not_mark_stale_when_busy(self):
        from webui import api_setup
        from webui.browser_tasks import (
            clear_zhihu_login_stale, zhihu_login_stale,
        )
        clear_zhihu_login_stale()
        with mock.patch(
                "applications.zhihu_story.browser_adapter.verify_zhihu_login",
                lambda headless=True: (None, "检查暂缓：浏览器正被任务占用")):
            r = api_setup.api_setup_zhihu_check()
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("status"), "busy")
        self.assertFalse(zhihu_login_stale().get("stale"),
                         "忙不该把登录态标成失效（旧行为会误导用户去重登）")

    def test_automation_defers_instead_of_failing_when_busy(self):
        """自动化遇到 profile 被占：记 BrowserBusy 顺延，不计失败、不触发熔断。"""
        from automation import executor
        bp.acquire_profile(0, purpose="login-guide")
        try:
            with mock.patch("webui.browser_tasks.browser_busy", lambda: []):
                with self.assertRaises(executor.BrowserBusy):
                    executor._full_chain({"type": "full_chain", "params": {}})
        finally:
            bp.release_profile()


if __name__ == "__main__":
    unittest.main()