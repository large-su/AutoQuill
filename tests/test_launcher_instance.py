# -*- coding: utf-8 -*-
"""单实例回归（2026-09-20 用户口径）。

现象：连点两次启动 → 右下角叠出两个托盘图标。根因是"服务已在跑"分支
（以及"启动中再点一次"）会再开一个窗口 + 再建一个 TrayController，而关窗
只是 Hide —— 每多启动一次就多留一个托盘图标，且那个进程既不拥有服务、
也不知道退出该由谁负责。

口径：
  · 已经在跑（含"正在启动中"）→ 新进程只负责把那个窗口显示出来，然后退出；
  · 没有在跑 → 正常启动；
  · 真退出仍然只有托盘菜单「退出 AutoQuill」（关窗 = 最小化到托盘，默认不变）。

本文件守住五件事：控制通道命令语义、实例文件读写、通知重试、
"重复启动绝不再建窗口/托盘"、以及 main() 的守卫位置（--service 子进程不受影响）。

运行：python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import launcher


def _quiet_second_instance(**kwargs):
    """跑 second_instance 并吞掉它给用户看的 print（返回 (rc, 输出)）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = launcher.second_instance(**kwargs)
    return rc, buf.getvalue()


class _FakeWindow:
    """窗口替身：记录 show()/Activate() 调用。"""

    def __init__(self, fail=False):
        self.shown = 0
        self.activated = 0
        self.fail = fail
        self.native = self

    def show(self):
        if self.fail:
            raise RuntimeError("show failed")
        self.shown += 1

    def Activate(self):
        self.activated += 1


class ControlCommandTest(unittest.TestCase):
    def setUp(self):
        launcher._ACTIVE_WINDOW = None
        launcher._WINDOW_MODE = "starting"
        launcher._SHOW_PENDING.clear()

    def tearDown(self):
        launcher._ACTIVE_WINDOW = None
        launcher._WINDOW_MODE = "starting"
        launcher._SHOW_PENDING.clear()

    def test_ping_reports_pid(self):
        reply = launcher.handle_control_command("PING")
        self.assertTrue(reply.startswith("OK"))
        self.assertIn(str(os.getpid()), reply)

    def test_show_while_starting_is_pending(self):
        reply = launcher.handle_control_command("SHOW")
        self.assertEqual(reply, launcher.CONTROL_PENDING)
        # 请求要记下来：窗口一建出来就得露面（不能吞掉）
        self.assertTrue(launcher._SHOW_PENDING.is_set())

    def test_show_in_browser_mode_reports_no_window(self):
        launcher._WINDOW_MODE = "browser"
        self.assertEqual(launcher.handle_control_command("SHOW"),
                         launcher.CONTROL_NO_WINDOW)

    def test_show_with_window_shows_and_activates(self):
        win = _FakeWindow()
        launcher._ACTIVE_WINDOW = win
        launcher._WINDOW_MODE = "window"
        self.assertEqual(launcher.handle_control_command("show"),
                         launcher.CONTROL_OK)
        self.assertEqual(win.shown, 1)
        self.assertEqual(win.activated, 1)

    def test_show_failure_is_reported_not_raised(self):
        launcher._ACTIVE_WINDOW = _FakeWindow(fail=True)
        launcher._WINDOW_MODE = "window"
        self.assertEqual(launcher.handle_control_command("SHOW"),
                         launcher.CONTROL_NO_WINDOW)

    def test_unknown_command(self):
        self.assertTrue(launcher.handle_control_command("DROP TABLE")
                        .startswith("ERR"))

    def test_empty_command(self):
        self.assertTrue(launcher.handle_control_command("").startswith("ERR"))


class ControlChannelSocketTest(unittest.TestCase):
    """真 socket 往返（127.0.0.1，端口系统分配，零外部依赖）。"""

    def setUp(self):
        launcher._ACTIVE_WINDOW = None
        launcher._WINDOW_MODE = "starting"
        self.port = launcher.start_control_server()
        self.assertGreater(self.port, 0)

    def tearDown(self):
        launcher._ACTIVE_WINDOW = None
        launcher._WINDOW_MODE = "starting"
        launcher._SHOW_PENDING.clear()

    def test_round_trip_ping(self):
        reply = launcher.send_instance_command("PING", port=self.port)
        self.assertTrue(reply and reply.startswith("OK"), reply)

    def test_round_trip_show_uses_active_window(self):
        win = _FakeWindow()
        launcher._ACTIVE_WINDOW = win
        launcher._WINDOW_MODE = "window"
        reply = launcher.send_instance_command("SHOW", port=self.port)
        self.assertEqual(reply, launcher.CONTROL_OK)
        self.assertEqual(win.shown, 1)

    def test_no_listener_returns_none(self):
        # 没人监听 → None（调用方据此判断"通知不上"）
        # 用一个刚关闭的端口，别用"服务端口 +1"——本机 32xx 段有回显服务，
        # 会把命令原样返回（实测踩过）
        import socket
        probe = socket.socket()
        probe.bind((launcher.CONTROL_HOST, 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.assertIsNone(launcher.send_instance_command("PING", port=dead_port,
                                                         timeout=0.3))

    def test_foreign_reply_is_not_treated_as_notified(self):
        # 端口被别的本机服务占用（回显/陌生服务）→ 回复里没有 AutoQuill → 当没通知上
        with mock.patch.object(launcher, "send_instance_command",
                               return_value="PING"):
            self.assertIsNone(launcher.notify_running_instance(timeout=0.2,
                                                               interval=0.01))


class InstanceFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch("tools.launcher.data_root",
                             return_value=Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_write_read_clear_round_trip(self):
        self.assertTrue(launcher.write_instance(54321, pid=4321))
        inst = launcher.read_instance()
        self.assertEqual(inst["port"], 54321)
        self.assertEqual(inst["pid"], 4321)
        launcher.clear_instance(54321)
        self.assertIsNone(launcher.read_instance())

    def test_clear_skips_when_port_taken_over(self):
        launcher.write_instance(1111)
        launcher.clear_instance(2222)          # 不是我的端口 → 不动
        self.assertIsNotNone(launcher.read_instance())

    def test_broken_file_is_tolerated(self):
        path = launcher.instance_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(launcher.read_instance())
        launcher.clear_instance()              # 不抛

    def test_illegal_port_rejected(self):
        path = launcher.instance_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "port": 0}), encoding="utf-8")
        self.assertIsNone(launcher.read_instance())


class SecondInstanceTest(unittest.TestCase):
    """重复启动的三种结果：唤起成功 / 启动中 / 浏览器回退 / 通知不上。"""

    def setUp(self):
        launcher._SHOW_PENDING.clear()

    def test_ok_reply_prints_and_exits_zero(self):
        with mock.patch.object(launcher, "read_instance",
                               return_value={"pid": 999, "port": 1}), \
             mock.patch.object(launcher, "notify_running_instance",
                               return_value=launcher.CONTROL_OK) as notify, \
             mock.patch.object(launcher, "open_window") as ow, \
             mock.patch.object(launcher, "start_service") as svc:
            rc, out = _quiet_second_instance()
        self.assertEqual(rc, 0)
        self.assertIn("已为你显示它的窗口", out)
        notify.assert_called_once()
        self.assertTrue(notify.call_args[1].get("show", True))
        # ★ 核心：重复启动绝不再建窗口 / 不再起服务（多一个窗口 = 多一个托盘图标）
        ow.assert_not_called()
        svc.assert_not_called()

    def test_pending_reply_leaves_window_to_first_instance(self):
        with mock.patch.object(launcher, "read_instance", return_value=None), \
             mock.patch.object(launcher, "notify_running_instance",
                               return_value=launcher.CONTROL_PENDING), \
             mock.patch.object(launcher, "open_window") as ow:
            rc, out = _quiet_second_instance()
        self.assertEqual(rc, 0)
        self.assertIn("正在启动中", out)
        ow.assert_not_called()

    def test_browser_fallback_opens_browser(self):
        with mock.patch.object(launcher, "read_instance", return_value=None), \
             mock.patch.object(launcher, "notify_running_instance",
                               return_value=launcher.CONTROL_NO_WINDOW), \
             mock.patch.object(launcher.webbrowser, "open") as op, \
             mock.patch.object(launcher, "open_window") as ow:
            rc, _ = _quiet_second_instance()
        self.assertEqual(rc, 0)
        op.assert_called_once_with(launcher.BASE_URL)
        ow.assert_not_called()

    def test_no_reply_still_does_not_build_second_window(self):
        with mock.patch.object(launcher, "read_instance", return_value=None), \
             mock.patch.object(launcher, "notify_running_instance",
                               return_value=None), \
             mock.patch.object(launcher, "open_window") as ow:
            rc, out = _quiet_second_instance()
        self.assertEqual(rc, 0)
        self.assertIn("托盘", out)          # 提示用户去托盘找窗口
        ow.assert_not_called()

    def test_tray_autostart_duplicate_pings_only(self):
        # 开机自启的静默启动撞上已有实例：只探活，不抢焦点弹窗
        with mock.patch.object(launcher, "read_instance", return_value=None), \
             mock.patch.object(launcher, "notify_running_instance",
                               return_value=launcher.CONTROL_OK) as notify:
            rc, _ = _quiet_second_instance(start_hidden=True)
        self.assertEqual(rc, 0)
        self.assertFalse(notify.call_args[1].get("show", True))

    def test_notify_retries_until_timeout(self):
        calls = {"n": 0}

        def _flaky(*a, **k):
            calls["n"] += 1
            return launcher.CONTROL_OK if calls["n"] >= 3 else None

        with mock.patch.object(launcher, "send_instance_command",
                               side_effect=_flaky):
            reply = launcher.notify_running_instance(show=True, timeout=2.0,
                                                     interval=0.01)
        self.assertEqual(reply, launcher.CONTROL_OK)
        self.assertEqual(calls["n"], 3)


class MainGuardTest(unittest.TestCase):
    """守卫必须早于建窗口/起服务；--service 子进程不受影响。"""

    def test_duplicate_launch_returns_second_instance_without_starting_service(self):
        with mock.patch.object(launcher, "acquire_single_instance",
                               return_value=False), \
             mock.patch.object(launcher, "second_instance",
                               return_value=0) as sec, \
             mock.patch.object(launcher, "start_control_server") as ctrl, \
             mock.patch.object(launcher, "start_service") as svc, \
             mock.patch.object(launcher, "open_window") as ow:
            rc = launcher.main()
        self.assertEqual(rc, 0)
        sec.assert_called_once()
        ctrl.assert_not_called()
        svc.assert_not_called()
        ow.assert_not_called()

    def test_service_mode_bypasses_guard(self):
        import main as main_module
        with mock.patch.object(launcher, "acquire_single_instance") as acq, \
             mock.patch.object(launcher, "migrate_legacy_data"), \
             mock.patch.object(main_module, "main", return_value=None), \
             mock.patch.object(sys, "argv", ["AutoQuill.exe", "--service"]):
            rc = launcher.main()
        self.assertEqual(rc, 0)
        acq.assert_not_called()      # 服务子进程不是"第二次启动"

    def test_guard_runs_before_window_creation_in_source(self):
        import inspect
        src = inspect.getsource(launcher.main)
        self.assertLess(src.index("acquire_single_instance()"),
                        src.index("start_service("))
        self.assertLess(src.index("acquire_single_instance()"),
                        src.index("open_window("))
        # 实例文件与退出清理：进程退出时删掉自己的登记
        self.assertIn("atexit.register(clear_instance", src)


class PresentWindowTest(unittest.TestCase):
    """唤起窗口 = 显示 + 取消最小化 + 抢焦点（托盘不可用时关窗会最小化到任务栏）。"""

    def test_show_restore_activate(self):
        calls = []

        class Win:
            native = None

            def show(self):
                calls.append("show")

            def restore(self):
                calls.append("restore")

        self.assertTrue(launcher._present_window(Win()))
        self.assertEqual(calls, ["show", "restore"])

    def test_old_pywebview_without_restore_is_fine(self):
        win = _FakeWindow()          # 只有 show()/Activate()，没有 restore()
        self.assertTrue(launcher._present_window(win))
        self.assertEqual(win.shown, 1)

    def test_no_window_returns_false(self):
        self.assertFalse(launcher._present_window(None))

    def test_tray_show_window_uses_same_helper(self):
        import inspect
        src = inspect.getsource(launcher.TrayController.show_window)
        self.assertIn("_present_window", src)


class QuitWatcherTest(unittest.TestCase):
    """退出入口：托盘菜单「退出 AutoQuill」+ 控制台按钮，且与托盘可用性解耦。"""

    class _Tray:
        def __init__(self):
            self._stopped = threading.Event()
            self.quit_calls = 0

        def quit(self):
            self.quit_calls += 1
            self._stopped.set()

    def test_console_quit_request_triggers_quit(self):
        tray = self._Tray()
        fake_cfg = mock.Mock()
        fake_cfg.load.return_value = {"quit_requested_at": "2026-09-20 10:00:00"}
        with mock.patch.object(launcher, "launcher_config", fake_cfg):
            thread = launcher._start_quit_watcher(tray, interval=0.01)
            self.assertIsNotNone(thread)
            thread.join(timeout=2.0)
        self.assertEqual(tray.quit_calls, 1)

    def test_no_request_no_quit(self):
        tray = self._Tray()
        fake_cfg = mock.Mock()
        fake_cfg.load.return_value = {"quit_requested_at": ""}
        with mock.patch.object(launcher, "launcher_config", fake_cfg):
            launcher._start_quit_watcher(tray, interval=0.01)
            time.sleep(0.1)
        self.assertEqual(tray.quit_calls, 0)
        tray._stopped.set()

    def test_missing_launcher_config_is_safe(self):
        with mock.patch.object(launcher, "launcher_config", None):
            self.assertIsNone(launcher._start_quit_watcher(self._Tray()))

    def test_watcher_started_outside_tray_attach(self):
        # 托盘建不起来（attach() 返回 False）时，控制台的退出按钮仍须有效
        import inspect
        src = inspect.getsource(launcher.open_window)
        self.assertIn("_start_quit_watcher(tray)", src)
        self.assertLess(src.index("_start_quit_watcher(tray)"),
                        src.index("if tray.attach()"))

    def test_poll_loop_only_refreshes(self):
        import inspect
        src = inspect.getsource(launcher.TrayController._poll_loop)
        self.assertIn("_refresh", src)
        self.assertNotIn("quit_requested_at", src)


class WindowPendingShowTest(unittest.TestCase):
    def test_start_hidden_window_shows_when_pending(self):
        import inspect
        src = inspect.getsource(launcher.open_window)
        self.assertIn("_SHOW_PENDING.is_set()", src)
        self.assertIn("window.show()", src)
        # 浏览器回退要把状态改成 browser，重复启动才不会误判"有窗口可唤"
        self.assertIn('_WINDOW_MODE"] = "browser"', src)


if __name__ == "__main__":
    unittest.main()
