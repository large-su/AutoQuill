# ============================================================
# tests/test_launcher.py — 启动器（深色标题栏 / 诊断日志）
# ============================================================

import unittest
from unittest import mock


class _Native:
    def __init__(self, handle=None):
        self.Handle = handle


class _NativeNoHandle:
    """老版本 pywebview：无 Handle 属性，走 get_handle()"""

    def get_handle(self):
        return 54321


class _NativeBroken:
    """native 结构异常：无 Handle 且 get_handle 抛错，走 FindWindowW 兜底"""

    def get_handle(self):
        raise RuntimeError("boom")


class _Evt:
    """pywebview Event 的最小替身（支持 += 订阅）"""

    def __init__(self):
        self._cbs = []

    def __iadd__(self, cb):
        self._cbs.append(cb)
        return self


class _Events:
    """模拟 window.events（shown 事件可订阅）"""

    def __init__(self):
        self.shown = _Evt()


class _Window:
    def __init__(self, native, events=None):
        self.native = native
        self.events = events or _Events()


class TestFindTitlebarHandle(unittest.TestCase):
    def test_uses_native_handle_when_available(self):
        from tools.launcher import _find_titlebar_handle
        w = _Window(_Native(handle=12345))
        self.assertEqual(_find_titlebar_handle(w), 12345)

    def test_falls_back_to_get_handle(self):
        from tools.launcher import _find_titlebar_handle
        self.assertEqual(_find_titlebar_handle(_Window(_NativeNoHandle())), 54321)

    def test_falls_back_to_findwindow(self):
        from tools.launcher import _find_titlebar_handle
        w = _Window(_NativeBroken())
        with mock.patch("ctypes.windll.user32.FindWindowW",
                        return_value=777) as m:
            self.assertEqual(_find_titlebar_handle(w), 777)
        m.assert_called_once_with(None, "AutoQuill")

    def test_zero_when_all_paths_fail(self):
        from tools.launcher import _find_titlebar_handle
        w = _Window(_NativeBroken())
        with mock.patch("ctypes.windll.user32.FindWindowW",
                        return_value=0):
            self.assertEqual(_find_titlebar_handle(w), 0)

    def test_empty_native_returns_zero(self):
        from tools.launcher import _find_titlebar_handle
        self.assertEqual(_find_titlebar_handle(None), 0)
        self.assertEqual(_find_titlebar_handle(_Window(None)), 0)


class TestApplyDarkTitlebar(unittest.TestCase):
    def test_non_windows_skipped(self):
        # 非 Windows 平台直接跳过，不抛异常
        from tools.launcher import _apply_dark_titlebar
        with mock.patch("os.name", "posix"):
            _apply_dark_titlebar(_Window(_Native(handle=12345)))
        # 无异常即通过

    def test_handle_not_found_logs_diag(self):
        # 拿不到句柄 → 写诊断日志（不静默吞掉，方便用户反馈排查）
        from tools.launcher import _apply_dark_titlebar
        w = _Window(_NativeBroken())
        with mock.patch("ctypes.windll.user32.FindWindowW",
                        return_value=0), \
             mock.patch("tools.launcher._log_diag") as diag:
            _apply_dark_titlebar(w)
        diag.assert_called_once()
        self.assertIn("句柄", diag.call_args[0][0])

    def test_dwm_failure_logs_hr(self):
        # DWM 调用失败 → 记录 HRESULT（属性 20 失败后尝试 19）
        from tools.launcher import _apply_dark_titlebar
        w = _Window(_Native(handle=42))
        with mock.patch("ctypes.windll.dwmapi.DwmSetWindowAttribute",
                        return_value=-2147024891) as dwm, \
             mock.patch("tools.launcher._log_diag") as diag:
            _apply_dark_titlebar(w)
        self.assertEqual(dwm.call_count, 2)  # 属性 20 和 19 都试了
        diag.assert_called_once()
        self.assertIn("HRESULT", diag.call_args[0][0])

    def test_success_attempts_attr_20_only(self):
        from tools.launcher import _apply_dark_titlebar
        w = _Window(_Native(handle=42))
        with mock.patch("ctypes.windll.dwmapi.DwmSetWindowAttribute",
                        return_value=0) as dwm, \
             mock.patch("tools.launcher._log_diag") as diag:
            _apply_dark_titlebar(w)
        self.assertEqual(dwm.call_count, 1)
        self.assertEqual(dwm.call_args[0][1], 20)  # 属性值 20
        diag.assert_not_called()

    def test_shown_event_schedules_retry(self):
        # 窗口显示事件后延迟补设一次（覆盖显示过程重置属性的竞态）
        from tools.launcher import _apply_dark_titlebar
        evt = _Evt()
        w = _Window(_Native(handle=42), events=_Events())
        with mock.patch("ctypes.windll.dwmapi.DwmSetWindowAttribute",
                        return_value=0), \
             mock.patch("threading.Timer") as timer:
            _apply_dark_titlebar(w)
            self.assertEqual(len(w.events.shown._cbs), 1)
            w.events.shown._cbs[0]()  # 触发窗口显示后的补设回调
            timer.assert_called_once_with(0.3, mock.ANY)
            self.assertEqual(timer.return_value.start.call_count, 1)


class TestLogDiag(unittest.TestCase):
    def test_writes_to_data_root_logs(self):
        from tools.launcher import _log_diag
        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("tools.launcher.data_root",
                            return_value=Path(tmp)):
                _log_diag("测试诊断")
            path = os.path.join(tmp, "logs", "launcher.log")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                self.assertIn("测试诊断", f.read())

    def test_write_failure_silent(self):
        from tools.launcher import _log_diag
        with mock.patch("tools.launcher.data_root",
                        side_effect=Exception("boom")):
            _log_diag("不会抛出")  # 无异常即通过


if __name__ == "__main__":
    unittest.main()
