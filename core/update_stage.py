# ============================================================
# core/update_stage.py — 一键更新的**状态落盘**（谁都能读，谁都别猜）
#
# 为什么单独一层：更新是「跨进程」的事——主程序下载、子进程换装、主程序下次
# 启动还要能知道上次成没成。三者之间**唯一的共享物就是这个文件**，
# 所以它必须是稳定的、可读的、坏不掉的（坏了就退回 idle，绝不让程序起不来）。
#
# 状态机：
#   idle → downloading → staged → applying → done
#                          ↑          ↓
#                        failed ←─────┘        （任何一步失败都落在 failed，附原因）
#
# 依赖方向：只依赖 core.paths 与 core.updater（都是 core 内部），不认识 webui。
# ============================================================

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from core import updater as _updater

log = logging.getLogger(__name__)

STAGE_IDLE = "idle"
STAGE_DOWNLOADING = "downloading"
STAGE_STAGED = "staged"          # 已下载并校验通过，等用户点「重启并安装」
STAGE_APPLYING = "applying"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

# 界面文案：状态 → 人话（UI 只渲染，不自己编）
STAGE_TEXT = {
    STAGE_IDLE: "未开始",
    STAGE_DOWNLOADING: "正在下载…",
    STAGE_STAGED: "已下载并校验通过，等待安装",
    STAGE_APPLYING: "正在安装并重启…",
    STAGE_DONE: "更新完成",
    STAGE_FAILED: "更新失败",
}

_FIELDS = ("stage", "version", "current", "installer", "sha256", "sha_sources",
           "size", "bytes", "at", "updated_at", "error", "install_dir",
           "log", "notes", "page_url", "attempts")


def stage_file() -> Path:
    return _updater.update_dir() / "stage.json"


def log_file() -> Path:
    return _updater.update_dir() / "apply.log"


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def blank():
    return {"stage": STAGE_IDLE, "version": "", "current": "", "installer": "",
            "sha256": "", "sha_sources": [], "size": 0, "bytes": 0,
            "at": "", "updated_at": "", "error": "", "install_dir": "",
            "log": str(log_file()), "notes": "", "page_url": "", "attempts": 0}


def load() -> dict:
    """读状态；缺失/损坏一律回退 blank（坏文件绝不能让程序起不来）。"""
    path = stage_file()
    if not path.exists():
        return blank()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:                # noqa: BLE001
        log.warning("更新状态读取失败（按未开始处理）：%s", exc)
        return blank()
    if not isinstance(raw, dict):
        return blank()
    out = blank()
    for k in _FIELDS:
        if k in raw:
            out[k] = raw[k]
    if out["stage"] not in STAGE_TEXT:
        out["stage"] = STAGE_IDLE
    return out


def save(state: dict) -> bool:
    """原子写状态。任何失败只记日志——更新状态写不进去不该影响主流程。"""
    payload = blank()
    payload.update(state or {})
    payload["updated_at"] = _now()
    if not payload.get("at"):
        payload["at"] = payload["updated_at"]
    path = stage_file()
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:                # noqa: BLE001
        log.warning("更新状态写入失败：%s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def update(**changes) -> dict:
    """读-改-写（调用方不必自己 load）。"""
    state = load()
    state.update(changes)
    save(state)
    return state


def mark_failed(error, **changes) -> dict:
    return update(stage=STAGE_FAILED, error=str(error or "未知原因")[:400], **changes)


def staged_installer(version=""):
    """已暂存的安装包路径（校验通过才算）；不属于 staged 态返回 None。"""
    state = load()
    if state.get("stage") != STAGE_STAGED:
        return None
    path = Path(state.get("installer") or "")
    if not path.exists():
        return None
    if version and str(state.get("version")) != str(version):
        return None
    return path


def clear_download_artifacts(keep_installer=True):
    """清掉 .part 等中间产物；keep_installer=False 时连安装包一起删。"""
    d = _updater.update_dir()
    removed = []
    try:
        for p in d.glob("*.part"):
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
        if not keep_installer:
            for p in d.glob("%s*%s" % (_updater.SETUP_PREFIX, _updater.SETUP_SUFFIX)):
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    pass
    except Exception as exc:                # noqa: BLE001
        log.debug("清理更新临时文件失败：%s", exc)
    return removed


def tail_log(lines=20):
    """子进程 apply 日志的尾部（失败时给用户看原因）。"""
    path = Path(load().get("log") or log_file())
    if not path.exists():
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-max(1, int(lines)):])
    except OSError:
        return ""
