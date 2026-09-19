# -*- coding: utf-8 -*-
"""M4 启动器设置回归：关窗行为 / 开机自启（含注册表后端的替身）。

用户口径 → 断言映射：
  - 关窗默认「最小化到托盘」，可在设置里切换 → 默认值 + 保存/读取测试；
  - 开机自启默认关闭、只在 Windows 写 HKCU（不碰系统级） → 命令拼接 + 后端替身测试；
  - 配置写坏不能让启动器行为失控 → 坏 JSON/怪值回退默认。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import launcher_config as lc
from core import paths


class _FakeRegistry:
    """注册表替身：测试不碰真实 HKCU。"""

    def __init__(self):
        self.values = {}
        self.deleted = []

    def get(self, name):
        return self.values.get(name)

    def set(self, name, command):
        self.values[name] = command

    def delete(self, name):
        self.deleted.append(name)
        self.values.pop(name, None)


class LauncherConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_launcher_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_defaults_when_file_missing(self):
        cfg = lc.load()
        self.assertTrue(cfg["close_to_tray"])      # 默认关窗→托盘
        self.assertFalse(cfg["autostart"])         # 默认不开机自启
        self.assertFalse(cfg["tray_hint_shown"])

    def test_save_then_load_roundtrip(self):
        lc.save({"close_to_tray": False, "autostart": True})
        cfg = lc.load()
        self.assertFalse(cfg["close_to_tray"])
        self.assertTrue(cfg["autostart"])
        # 落盘路径就是 DATA_ROOT/config/launcher.json（设置页与启动器共用）
        self.assertTrue(str(lc.path()).startswith(str(self.tmp)))
        self.assertTrue(Path(lc.path()).exists())

    def test_partial_save_keeps_other_keys(self):
        lc.save({"close_to_tray": False})
        lc.save({"autostart": True})
        cfg = lc.load()
        self.assertFalse(cfg["close_to_tray"])
        self.assertTrue(cfg["autostart"])

    def test_broken_json_falls_back_to_defaults(self):
        Path(lc.path()).parent.mkdir(parents=True, exist_ok=True)
        Path(lc.path()).write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(lc.load(), lc.DEFAULTS)
        self.assertEqual(lc.load(), lc.DEFAULTS)

    def test_stringy_values_are_parsed_not_truthy(self):
        Path(lc.path()).parent.mkdir(parents=True, exist_ok=True)
        Path(lc.path()).write_text(json.dumps(
            {"close_to_tray": "0", "autostart": "no"}), encoding="utf-8")
        cfg = lc.load()
        self.assertFalse(cfg["close_to_tray"])     # "0" ≠ True
        self.assertFalse(cfg["autostart"])
        self.assertTrue(lc.as_bool("yes", False))
        self.assertTrue(lc.as_bool("  TRUE ", False))
        self.assertEqual(lc.as_bool("???", True), True)   # 认不出 → 用默认


class AutostartTest(unittest.TestCase):
    def test_command_frozen_and_source(self):
        frozen = lc.autostart_command(frozen=True, exe=r"C:\App\AutoQuill.exe")
        self.assertEqual(frozen, '"C:\\App\\AutoQuill.exe" --tray')
        src = lc.autostart_command(frozen=False,
                                   launcher=r"D:\Code\AutoQuill\tools\launcher.py",
                                   pythonw=r"D:\Code\AutoQuill\.venv\Scripts\pythonw.exe")
        self.assertIn("pythonw.exe", src)
        self.assertIn("launcher.py", src)
        self.assertTrue(src.endswith("--tray"))    # 开机不弹窗，直接驻留托盘

    def test_apply_autostart_writes_and_deletes(self):
        reg = _FakeRegistry()
        with mock.patch.object(lc, "autostart_supported", lambda: True):
            ok, msg = lc.apply_autostart(True, backend=reg)
            self.assertTrue(ok, msg)
            self.assertIn("--tray", reg.values["AutoQuill"])
            enabled, cmd = lc.autostart_state(backend=reg)
            self.assertTrue(enabled)
            self.assertEqual(cmd, reg.values["AutoQuill"])
            ok, msg = lc.apply_autostart(False, backend=reg)
            self.assertTrue(ok, msg)
            self.assertEqual(reg.values, {})
            self.assertIn("AutoQuill", reg.deleted)

    def test_unsupported_platform_reports_reason(self):
        with mock.patch.object(lc, "autostart_supported", lambda: False):
            ok, msg = lc.apply_autostart(True, backend=_FakeRegistry())
        self.assertFalse(ok)
        self.assertIn("不支持", msg)

    def test_registry_error_is_reported_not_raised(self):
        class _Boom(_FakeRegistry):
            def set(self, name, command):
                raise OSError("拒绝访问")

        with mock.patch.object(lc, "autostart_supported", lambda: True):
            ok, msg = lc.apply_autostart(True, backend=_Boom())
        self.assertFalse(ok)
        self.assertIn("拒绝访问", msg)

    def test_ensure_consistent_heals_missing_entry(self):
        reg = _FakeRegistry()
        with mock.patch.object(lc, "autostart_supported", lambda: True):
            # 设置里开着自启但注册表被清掉 → 启动时补写
            warn = lc.ensure_autostart_consistent({"autostart": True}, backend=reg)
            self.assertIsNone(warn)
            self.assertIn("AutoQuill", reg.values)
            reg.values.clear()
            # 设置里关着 → 不动注册表
            self.assertIsNone(lc.ensure_autostart_consistent({"autostart": False},
                                                             backend=reg))
            self.assertEqual(reg.values, {})


class LauncherApiTest(unittest.TestCase):
    """/api/launcher/settings：设置页读写（自启走替身，不碰真实注册表）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_launcher_api_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.reg = _FakeRegistry()
        self._sup = mock.patch.object(lc, "autostart_supported", lambda: True)
        self._sup.start()
        # 走真实 apply_autostart 逻辑，只把注册表后端换成替身
        real_apply = lc.apply_autostart
        self._apply = mock.patch.object(
            lc, "apply_autostart",
            lambda enabled, backend=None: real_apply(enabled, backend=self.reg))
        self._apply.start()
        from fastapi.testclient import TestClient
        from webui.server import app
        self.client = TestClient(app)

    def tearDown(self):
        self._apply.stop()
        self._sup.stop()
        self._p.stop()

    def test_get_returns_settings_and_capability(self):
        d = self.client.get("/api/launcher/settings").json()
        self.assertTrue(d["settings"]["close_to_tray"])
        self.assertFalse(d["settings"]["autostart"])
        self.assertTrue(d["autostart_supported"])
        self.assertIn("--tray", d["autostart_command"])
        self.assertIn("launcher.json", d["config_path"])

    def test_post_close_to_tray_persists(self):
        d = self.client.post("/api/launcher/settings",
                             json={"close_to_tray": False}).json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["settings"]["close_to_tray"])
        self.assertFalse(self.client.get("/api/launcher/settings")
                         .json()["settings"]["close_to_tray"])

    def test_post_autostart_updates_settings_and_registry(self):
        d = self.client.post("/api/launcher/settings",
                             json={"autostart": True}).json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["settings"]["autostart"])
        self.assertIn("AutoQuill", self.reg.values)
        d2 = self.client.post("/api/launcher/settings",
                              json={"autostart": False}).json()
        self.assertFalse(d2["settings"]["autostart"])
        self.assertEqual(self.reg.values, {})



if __name__ == "__main__":
    unittest.main()
