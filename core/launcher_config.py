# -*- coding: utf-8 -*-
"""启动器设置：关窗行为 / 开机自启（启动器进程与控制台服务共用的唯一来源）。

为什么单独一份配置：这两件事的执行者是**启动器进程**（窗口与托盘在那边），
而改设置的人在**控制台设置页**（服务进程里）。两侧都读写同一份
`DATA_ROOT/config/launcher.json`：服务端保存后，启动器在**每次关窗/启动时**
重新读取即生效，不需要重启程序，也不需要两边互相通知。

约定（与项目其它配置一致）：读失败/坏值一律回退默认——启动链路不能因为
一份写坏的配置就崩掉或行为失控。
"""

import json
import logging
import os
import sys
import tempfile

from core import paths

log = logging.getLogger(__name__)

# 关窗 = 最小化到托盘（用户口径：默认开启，可在设置里切换成「直接退出」）
DEFAULTS = {
    "close_to_tray": True,
    "autostart": False,
    # 首次「藏进托盘」时提示一次（Win11 会把新图标收进折叠区，不提示用户会以为程序没了）
    "tray_hint_shown": False,
}

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "AutoQuill"


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def as_bool(value, default):
    """宽容的布尔解析：手改坏的配置（"0"/"no"/数字）不该被当成 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    return default


def path():
    """配置文件路径（源码态 = 项目根，安装态 = %APPDATA%/AutoQuill）。"""
    return paths.data("config", "launcher.json")


def load():
    """读取设置；文件缺失/损坏/值类型不对 → 用默认值补齐（永不抛）。"""
    cfg = dict(DEFAULTS)
    try:
        with open(path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key, default in DEFAULTS.items():
                val = raw.get(key, default)
                cfg[key] = as_bool(val, default) if isinstance(default, bool) \
                    else val
    except FileNotFoundError:
        pass
    except Exception as exc:      # noqa: BLE001
        log.warning("启动器设置读取失败，按默认值处理：%s", exc)
    return cfg


def save(patch):
    """合并保存（原子写：先写临时文件再替换，避免半个 JSON）。返回最新设置。"""
    cfg = load()
    if isinstance(patch, dict):
        for key in DEFAULTS:
            if key in patch:
                val = patch[key]
                cfg[key] = as_bool(val, cfg.get(key)) \
                    if isinstance(DEFAULTS[key], bool) else val
    target = path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except Exception as exc:      # noqa: BLE001
        log.warning("启动器设置保存失败：%s", exc)
    return cfg


# ------------------------------------------------------------
# 开机自启（Windows：HKCU 的 Run 键，不写系统级、不需要管理员）
# ------------------------------------------------------------

def autostart_command(frozen=None, exe=None, launcher=None, pythonw=None):
    """拼开机自启的命令行（纯函数，便于单测）。

    - 安装态：`"<AutoQuill.exe>" --tray`（启动即静默驻留托盘，不弹窗打断用户）；
    - 源码态：`"<pythonw.exe>" "<tools/launcher.py>" --tray`——用 pythonw 避免
      开机时闪一个黑框；pythonw 不存在时退回当前解释器（功能优先于观感）。
    """
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        exe = exe or sys.executable
        return '"%s" --tray' % exe
    root = paths.DATA_ROOT
    launcher = launcher or os.path.join(root, "tools", "launcher.py")
    if pythonw is None:
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        pythonw = cand if os.path.exists(cand) else sys.executable
    return '"%s" "%s" --tray' % (pythonw, launcher)


class WinRegBackend:
    """真实注册表后端（只在 Windows 且有 winreg 时可用）。"""

    def get(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return value or ""
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def set(self, name, command):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)

    def delete(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def autostart_supported():
    """当前平台/运行方式是否支持开机自启。"""
    if os.name != "nt":
        return False
    try:
        import winreg  # noqa: F401
        return True
    except Exception:      # noqa: BLE001
        return False


def autostart_state(backend=None):
    """返回 (是否已开启, 注册表里当前的命令行)。读不到就是未开启。"""
    backend = backend or WinRegBackend()
    try:
        value = backend.get(_RUN_VALUE)
    except Exception as exc:      # noqa: BLE001
        log.warning("读取开机自启失败：%s", exc)
        return False, ""
    return bool(value), value or ""


def apply_autostart(enabled, backend=None):
    """写入/删除自启项。返回 (ok, message)：失败不抛，交给调用方提示用户。"""
    if not autostart_supported():
        return False, "当前环境不支持开机自启（仅 Windows 支持）"
    backend = backend or WinRegBackend()
    try:
        if enabled:
            cmd = autostart_command()
            backend.set(_RUN_VALUE, cmd)
            return True, "已设置开机自启：%s" % cmd
        backend.delete(_RUN_VALUE)
        return True, "已关闭开机自启"
    except Exception as exc:      # noqa: BLE001
        log.warning("设置开机自启失败：%s", exc)
        return False, "设置开机自启失败：%s" % exc


def ensure_autostart_consistent(cfg=None, backend=None):
    """启动时自愈：设置里开着自启但注册表丢了（换目录/被杀软清了）→ 补写。"""
    cfg = cfg or load()
    if not cfg.get("autostart"):
        return None
    ok, msg = apply_autostart(True, backend=backend)
    return msg if not ok else None
