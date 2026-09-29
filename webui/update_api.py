# ============================================================
# webui/update_api.py — 一键更新的接口层（编排，不实现细节）
#
# 分工：
#   core/updater.py      纯逻辑（解析 / 下载 / 校验 / 安装参数）
#   core/update_stage.py 状态落盘（跨进程共享的唯一真相）
#   tools/apply_update.py 独立子进程（换装 / 重启 / 清理）
#   本文件               把它们串起来，暴露给界面
#
# 依赖方向：webui → core；core 不认识 webui。
# ★ 绝不在这里做业务判断（版本比较、校验规则都在 core，可单测）。
# ============================================================

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from core import update_stage as stage
from core import updater

log = logging.getLogger(__name__)
router = APIRouter()

_dl = {"running": False, "bytes": 0, "total": 0, "error": "", "version": ""}
_dl_lock = threading.Lock()


def _friendly(exc):
    """底层报错 → 用户能据此行动的一句话（与检查更新同一风格）。"""
    text = "%s" % (exc,)
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "下载超时（网络较慢或代理不稳），可稍后重试"
    if "connection" in low or "ssl" in low or "proxy" in low:
        return "网络连接失败（检查网络或代理后重试）"
    if "no space" in low or "errno 28" in low:
        return "磁盘空间不足，请清理后重试"
    if "permission" in low or "access is denied" in low:
        return "没有写入权限（可能被杀毒软件拦截），请重试或手动下载"
    return text[:200]


def _current_release():
    """拿最新 release 的结构化信息（复用检查更新的多通道兜底，避免新写一套）。"""
    from webui import api_setup
    got = api_setup.api_update_check() or {}
    return got


def _fetch_release_payload(timeout=10):
    """直接取最新 release 的**完整 JSON**（资产、digest、更新说明都在里面）。

    检查更新那条链路（api_setup）只回版本号与页面链接，够界面提示用；
    要下载就必须知道资产直链与校验和，所以这里单独取一次。
    取不到返回 None —— 调用方退回「按命名规则拼 URL」（确定性很强：
    资产名由 build_release.py 固定生成）。
    """
    try:
        import requests
        r = requests.get(
            "https://api.github.com/repos/%s/releases/latest" % updater.REPO,
            timeout=timeout,
            headers={"User-Agent": "AutoQuill",
                     "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return r.json()
    except Exception as exc:                # noqa: BLE001
        log.warning("更新：取 release 详情失败（退回按命名规则拼直链）：%s", exc)
        return None


def _download_worker(plan):
    """后台线程：下载 → 取期望校验和 → 校验 → 落 staged 状态。"""
    try:
        stage.update(stage=stage.STAGE_DOWNLOADING, version=plan.version,
                     current=plan.current, installer=plan.dest,
                     size=plan.installer_size, bytes=0, error="",
                     sha_sources=list(plan.sha_sources), notes=plan.notes,
                     page_url=plan.page_url,
                     install_dir=updater.resolve_install_dir() or "")

        def on_progress(read, total):
            with _dl_lock:
                _dl["bytes"] = read
                _dl["total"] = total or plan.installer_size
            # 每 ~2MB 落一次盘，界面刷新能看到进度，又不至于写爆磁盘
            if read % (2 * 1024 * 1024) < updater.CHUNK:
                stage.update(bytes=read, size=total or plan.installer_size)

        got = updater.download(plan.installer_url, plan.dest,
                               progress=on_progress,
                               expected_size=plan.installer_size)
        if not got.get("ok"):
            raise RuntimeError(got.get("error") or "下载失败")
        stage.update(bytes=got.get("bytes") or 0)

        # 期望校验和：检查阶段没拿到就在这里补一次（双源交叉验证）
        expected = plan.sha256
        sources = list(plan.sha_sources or ())
        if not expected:
            looked = updater.fetch_expected_sha256(plan.release_info or {})
            if not looked.get("ok"):
                raise RuntimeError(looked.get("error") or "拿不到校验和")
            expected, sources = looked["sha256"], list(looked.get("sources") or ())

        checked = updater.verify(plan.dest, expected)
        if not checked.get("ok"):
            # 校验不过就地销毁：绝不让一个来路不明的包留在磁盘上
            try:
                Path(plan.dest).unlink()
            except OSError:
                pass
            raise RuntimeError(checked.get("error") or "校验失败")

        stage.update(stage=stage.STAGE_STAGED, sha256=checked["sha256"],
                     sha_sources=sources, error="",
                     size=plan.installer_size or got.get("bytes") or 0,
                     bytes=got.get("bytes") or 0)
        log.info("更新：%s 已下载并通过校验，等待用户确认安装", plan.version)
    except Exception as exc:                # noqa: BLE001
        log.warning("更新下载失败：%s", exc)
        stage.update(stage=stage.STAGE_FAILED, error=_friendly(exc))
        with _dl_lock:
            _dl["error"] = _friendly(exc)
    finally:
        with _dl_lock:
            _dl["running"] = False


def _release_payload_for(version):
    """构造一个「只含已知信息」的 release 载荷，供补取校验和使用。

    检查阶段的 /api/update/check 只给了版本号与页面链接；这里按既定命名规则
    推出资产 URL（与 build_release.py 的产物名一致），再去取 .sha256。
    """
    base = "https://github.com/%s/releases/download/%s/" % (updater.REPO,
                                                            "v" + version)
    name = updater.setup_asset_name(version)
    return {
        "tag_name": "v" + version,
        "assets": [
            {"name": name, "browser_download_url": base + name},
            {"name": name + ".sha256",
             "browser_download_url": base + name + ".sha256"},
        ],
    }


@router.get("/api/update/status")
def api_update_status():
    """当前更新状态（界面每次刷新都读它）。

    * 若上次停在 applying（换装没走完就重启了），这里会判成失败并给出原因——
      不能让用户"点了更新却什么都没发生"（2026-09-29 事故后加的交代）。
    """
    state = stage.load()
    if state.get("stage") == stage.STAGE_APPLYING:
        stage.mark_failed(
            "上次自动更新没有完成（换装进程可能被中断）。"
            "安装包已保留，可手动安装。")
        state = stage.load()
    info = dict(state)
    info["stage_text"] = stage.STAGE_TEXT.get(state.get("stage"), "")
    installer = Path(state.get("installer") or "")
    info["installer_exists"] = bool(state.get("installer") and installer.exists())
    info["log_tail"] = stage.tail_log(20) if state.get("stage") == stage.STAGE_FAILED else ""
    with _dl_lock:
        info["download"] = dict(_dl)
    info["support_auto_install"] = bool(updater.resolve_install_dir())
    return info


@router.post("/api/update/download")
def api_update_download():
    """下载最新版并校验（P1：下载完成后由用户点「重启并安装」）。"""
    with _dl_lock:
        if _dl["running"]:
            return {"ok": False, "message": "正在下载中，请稍候"}
    checked = _current_release()
    if checked.get("error"):
        return {"ok": False, "message": checked["error"]}
    if not checked.get("has_update"):
        return {"ok": False, "message": "当前已是最新版本（%s）"
                                        % checked.get("current")}

    # 优先用完整 release JSON（含资产直链/digest/更新说明）；取不到再按
    # 命名规则拼直链（资产名由 build_release.py 固定生成，确定性很强）
    payload = _fetch_release_payload() or _release_payload_for(checked["latest"])
    plan = updater.build_plan(payload, checked.get("current") or "")
    if not plan.version:
        return {"ok": False, "message": "解析发布信息失败"}
    if plan.error:
        # 拿不到校验和：明确拒绝（宁可不更新，也不装来路不明的东西）
        return {"ok": False, "message": "已取消更新：%s" % plan.error}
    plan.release_info = updater.parse_release(payload)
    plan.notes = plan.notes or checked.get("notes") or ""
    plan.page_url = plan.page_url or checked.get("url") or ""

    stage.clear_download_artifacts(keep_installer=True)
    with _dl_lock:
        _dl.update(running=True, bytes=0, total=plan.installer_size,
                   error="", version=plan.version)
    threading.Thread(target=_download_worker, args=(plan,), daemon=True).start()
    return {"ok": True, "version": plan.version,
            "size": plan.installer_size,
            "message": "开始下载 %s（%.1f MB）" % (plan.version,
                                                  plan.installer_size / 1e6)}


@router.post("/api/update/apply")
def api_update_apply(dry_run: bool = False):
    """拉起隐藏的换装宿主，并**真正请求本程序退出**，由宿主装完重启。

    ★ 2026-09-29 线上事故的两个根因（都在这一个函数里）：
      1. 换装进程曾经是 `AutoQuill.exe --apply-update` —— 但冻结态入口是
         **launcher**（先接管并开窗口），于是它把这次调用当成「正常启动」：
         开窗口 → 抢单实例 → 失败退出，黑框不停弹；主程序却一直没退出。
         → 现在换装宿主是 **PowerShell**（CREATE_NO_WINDOW 隐藏），绝不自我循环。
      2. 这里只 spawn 了子进程，**从来没请求退出**——而本程序的退出要由启动器做
         （`/api/launcher/quit` 写标志、启动器 5 秒轮询到才退）。
         → 现在 spawn 之后**立刻写退出请求**，启动器随即退出，宿主才开始安装。
    """
    state = stage.load()
    if state.get("stage") != stage.STAGE_STAGED:
        return {"ok": False, "message": "还没有可安装的版本（先下载并通过校验）"}
    installer = Path(state.get("installer") or "")
    if not installer.exists():
        stage.mark_failed("安装包不见了：%s" % installer)
        return {"ok": False, "message": "安装包不见了，请重新下载"}

    install_dir = str(state.get("install_dir") or "")
    cmd = updater.powershell_apply_command(
        installer, _current_pid(), install_dir, stage.log_file(),
        relaunch_exe=_relaunch_exe(install_dir))
    if dry_run:
        # 演练：不等待、不安装（宿主只记日志），用于验证命令行拼装
        cmd = updater.powershell_apply_command(
            installer, 0, install_dir, stage.log_file(), relaunch_exe="")
    try:
        subprocess.Popen(
            cmd, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as exc:                # noqa: BLE001
        stage.mark_failed("启动换装宿主失败：%s" % exc)
        return {"ok": False, "message": "启动换装宿主失败：%s" % exc}

    stage.update(stage=stage.STAGE_APPLYING, error="")
    if not dry_run:
        _request_app_quit()
    log.info("更新：已拉起换装宿主（dry_run=%s），已请求本程序退出", dry_run)
    return {"ok": True, "dry_run": bool(dry_run),
            "message": ("演练：宿主已就绪（不会安装）" if dry_run
                        else "更新已开始：程序将自动退出并重启")}


def _relaunch_exe(install_dir):
    """装完要重启的程序路径（冻结态才有意义；源码态返回空）。"""
    if not getattr(sys, "frozen", False):
        return ""
    if install_dir:
        cand = Path(install_dir) / "AutoQuill.exe"
        if cand.exists():
            return str(cand)
    return str(sys.executable)


def _request_app_quit():
    """请求本程序退出：写启动器的退出标志（启动器最多 5 秒轮询到就真退出）。

    这是**唯一**能让「窗口 + 服务 + 自动化」一起干净退出的入口——
    直接 os._exit 会让服务进程/浏览器变成孤儿。
    """
    try:
        from core import launcher_config
        stamp = launcher_config.request_quit()
        log.info("更新：已请求退出（%s）", stamp)
        return True
    except Exception as exc:                # noqa: BLE001
        log.warning("更新：请求退出失败（%s），换装宿主会等待超时后强制结束", exc)
        return False


def _current_pid():
    return os.getpid()


@router.post("/api/update/restart")
def api_update_restart():
    """只重启（更新完但没自动起来时的兜底，或用户手动要求重启）。"""
    if not getattr(sys, "frozen", False):
        return {"ok": False, "message": "源码态不支持自动重启"}
    exe = Path(sys.executable)
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception as exc:                # noqa: BLE001
        return {"ok": False, "message": "重启失败：%s" % exc}
    time.sleep(0.3)
    return {"ok": True, "message": "正在重启…"}


@router.post("/api/update/cleanup")
def api_update_cleanup():
    """手动清理暂存的安装包（用户改主意不更新了）。"""
    removed = stage.clear_download_artifacts(keep_installer=False)
    state = stage.load()
    if state.get("stage") in (stage.STAGE_STAGED, stage.STAGE_FAILED):
        stage.update(stage=stage.STAGE_IDLE, error="", bytes=0)
    return {"ok": True, "removed": removed,
            "message": "已清理 %d 个文件" % len(removed)}
