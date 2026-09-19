# -*- coding: utf-8 -*-
"""启动器设置 API（M4：关窗行为 / 开机自启）。

分工：这里的读写都落在 `core/launcher_config`（DATA_ROOT/config/launcher.json），
**启动器进程**在每次关窗/启动时重新读取即生效——服务端不需要通知启动器，
也就不存在「设置完了但没生效」的中间态。

自启要写注册表（HKCU 的 Run 键），失败不抛 500 而是回 ok=false + 原因：
用户在设置页看到的应该是一句能读懂的话（例如"仅 Windows 支持"），
而不是一个 HTTP 报错。
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from core import launcher_config

log = logging.getLogger(__name__)

router = APIRouter()


class _LauncherBody(BaseModel):
    close_to_tray: bool | None = None
    autostart: bool | None = None


def _snapshot(message=""):
    cfg = launcher_config.load()
    enabled, command = launcher_config.autostart_state()
    return {
        "settings": cfg,
        "autostart_enabled": enabled,
        "autostart_installed_command": command,
        "autostart_command": launcher_config.autostart_command(),
        "autostart_supported": launcher_config.autostart_supported(),
        "config_path": launcher_config.path(),
        "message": message,
    }


@router.get("/api/launcher/settings")
def api_launcher_settings():
    """当前启动器设置（设置页打开时调用）。"""
    return _snapshot()


@router.post("/api/launcher/settings")
def api_launcher_settings_save(body: _LauncherBody):
    """保存设置。close_to_tray 立即生效；autostart 会同步写/删注册表。"""
    patch = {}
    message = ""
    ok = True
    if body.close_to_tray is not None:
        patch["close_to_tray"] = bool(body.close_to_tray)
    if body.autostart is not None:
        ok, message = launcher_config.apply_autostart(bool(body.autostart))
        if ok:
            patch["autostart"] = bool(body.autostart)
        else:
            log.warning("开机自启切换失败：%s", message)
    if patch:
        launcher_config.save(patch)
    data = _snapshot(message)
    data["ok"] = ok
    return data
