# -*- coding: utf-8 -*-
"""一键更新：脱离主程序的**执行者**（换装 → 重启 → 清理）。

为什么要独立进程：Windows 上运行中的 exe 不能被替换。所以流程必须是
「主程序退出 → 本进程换装 → 重启新版本」。本进程由主程序以
DETACHED_PROCESS 方式拉起，**父进程死掉不影响它**。

两种运行方式（同一套代码）：
  · 冻结态：AutoQuill.exe --apply-update --pid <父pid>
  · 源码态：python tools/apply_update.py --pid <父pid>   （测试用）

安全底线（顺序不可换）：
  1. 校验安装包 sha256 —— 不通过直接失败，**绝不执行**；
  2. 等父进程真的退出（含超时兜底），避免"文件被占用"的假失败；
  3. 静默安装到**原安装目录**（/DIR），完整日志落 apply.log；
  4. 只有安装返回 0 才重启；失败则保留安装包并写 failed 状态（可手动兜底）。

用法：
  python tools/apply_update.py --pid 1234 [--dry-run] [--wait 60]
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import update_stage as stage          # noqa: E402
from core import updater                        # noqa: E402

LOG_MAX_BYTES = 512 * 1024          # 日志上限：超过就截断重来，别无限长


def log(msg):
    """写 apply.log（子进程没有任何界面，日志是唯一的交代）。

    ★ 刻意不往 stdout 打印：换装进程由主程序以 DETACHED_PROCESS 拉起，
      没有终端；即便有终端也常是 GBK 代码页，中文/符号一打印就可能抛
      UnicodeEncodeError（真机踩到）。日志写文件最稳，且随时可查。
    """
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        path = stage.stage_file().with_name("apply.log")
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.unlink()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:               # noqa: BLE001 日志失败不该影响更新
        pass


def process_alive(pid):
    """进程是否还活着（Windows 用 tasklist，避免引入额外依赖）。"""
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                             capture_output=True, text=True, timeout=15)
        text = (out.stdout or "") + (out.stderr or "")
        return str(int(pid)) in text
    except Exception as exc:        # noqa: BLE001
        log("查询进程状态失败（按已退出处理）：%s" % exc)
        return False


def wait_for_exit(pid, timeout=60):
    """等主程序退出；超时就强制结束（用户已经点过确认，不能卡住不装）。"""
    deadline = time.time() + max(5, int(timeout))
    while time.time() < deadline:
        if not process_alive(pid):
            log("主程序（pid=%s）已退出" % pid)
            return True
        time.sleep(1.5)
    log("等待 %ss 仍未退出，尝试结束它" % timeout)
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, timeout=20)
    except Exception as exc:        # noqa: BLE001
        log("taskkill 失败：%s" % exc)
    time.sleep(2)
    return not process_alive(pid)


def run_installer(installer, install_dir, dry_run=False):
    """静默换装；返回 (ok, 说明)。

    ★ 不捕获安装器的 stdout/stderr：Inno 的输出是 **GBK**，按 UTF-8 解码会抛
      UnicodeDecodeError（真机踩到：读取线程直接崩）。安装器的完整过程本来
      就写进 /LOG 指向的 apply.log，这里只看返回码即可。
    """
    args = updater.installer_args(installer, install_dir, stage.log_file())
    cmd = [str(installer)] + args
    log("执行安装：%s" % " ".join(cmd))
    if dry_run:
        log("[dry-run] 不真的执行安装（安装目录=%s）" % (install_dir or "未探测到"))
        return True, "dry-run：已通过校验，未执行安装"
    try:
        # CREATE_NO_WINDOW：这是个 windowed 程序拉起的子进程，不给它开控制台窗口，
        # 否则换装时会闪黑框（2026-09-29 用户反馈的"黑框反复弹出"）。
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:        # noqa: BLE001
        return False, "启动安装器失败：%s" % exc
    if r.returncode != 0:
        return False, "安装器返回码 %s（详见 %s）" % (r.returncode, stage.log_file())
    log("安装完成（返回码 0）")
    return True, "安装完成"


def relaunch(install_dir, override=""):
    """重启新版本（安装目录下的 AutoQuill.exe）。

    override 仅供自测：指向任意程序（如 notepad.exe）验证「重启」这一步真的执行了，
    而不必真的再拉起一个 AutoQuill。
    """
    if override:
        exe = Path(override)
        if not exe.exists():
            return False, "指定的重启程序不存在：%s" % exe
        try:
            subprocess.Popen([str(exe)],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            log("已重新启动（自测）：%s" % exe)
            return True, "已重启"
        except Exception as exc:        # noqa: BLE001
            return False, "重启失败：%s" % exc
    if not install_dir:
        return False, "没探测到安装目录，跳过重启"
    exe = Path(install_dir) / "AutoQuill.exe"
    if not exe.exists():
        return False, "安装目录里没有 AutoQuill.exe：%s" % exe
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        log("已重新启动：%s" % exe)
        return True, "已重启"
    except Exception as exc:        # noqa: BLE001
        return False, "重启失败：%s（请手动打开 %s）" % (exc, exe)


def cleanup(installer):
    """删掉安装包（用户要求：装完把包删掉）。失败也不吵——那只是占点空间。"""
    try:
        p = Path(installer)
        if p.exists():
            p.unlink()
            log("已删除安装包：%s" % p.name)
    except Exception as exc:        # noqa: BLE001
        log("删除安装包失败（不影响使用）：%s" % exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0, help="主程序 pid（等它退出）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做校验与准备，不执行安装、不重启")
    ap.add_argument("--wait", type=int, default=600,
                    help="等主程序退出的秒数（默认 600；旧值是 60，太短）")
    ap.add_argument("--installer", default="",
                    help="覆盖安装包路径（**仅供自测**：用假安装器验证整条链路，"
                         "不真装）")
    ap.add_argument("--relaunch-exe", default="",
                    help="装完要重启的程序（**仅供自测**：如 notepad.exe）")
    args = ap.parse_args()

    state = stage.load()
    installer = Path(args.installer) if args.installer else \
        Path(state.get("installer") or "")
    version = str(state.get("version") or "")
    expected = str(state.get("sha256") or "")
    install_dir = str(state.get("install_dir") or "") or (updater.resolve_install_dir() or "")

    log("=== 开始应用更新：%s → %s（dry_run=%s）==="
        % (state.get("current") or "?", version, args.dry_run))

    # ① 校验（顺序第一，绝不能把没验过的包交出去执行）
    if not installer.exists():
        return stage_fail("安装包不存在：%s" % installer)
    got = updater.verify(installer, expected)
    if not got.get("ok"):
        return stage_fail(got.get("error") or "校验失败")
    log("校验通过（sha256=%s…）" % (got["sha256"][:16]))

    stage.update(stage=stage.STAGE_APPLYING, error="", install_dir=install_dir)

    # ② 等主程序退出
    if args.pid and not args.dry_run:
        if not wait_for_exit(args.pid, args.wait):
            return stage_fail("主程序没有退出，无法替换文件（可手动运行安装包）")

    # ③ 静默换装
    ok, why = run_installer(installer, install_dir, dry_run=args.dry_run)
    if not ok:
        return stage_fail(why)

    if args.dry_run:
        stage.update(stage=stage.STAGE_STAGED, error="")
        log("dry-run 结束：校验与参数都正确，未安装、未重启、未删除安装包")
        return 0

    # ④ 重启 + ⑤ 清理
    relaunched, relaunch_why = relaunch(
        install_dir, override=args.relaunch_exe)
    if not args.installer:                  # 自测（假安装器）时不删真包
        cleanup(installer)
    stage.update(stage=stage.STAGE_DONE,
                 error="" if relaunched else relaunch_why,
                 bytes=0)
    log("=== 更新完成：%s（%s）===" % (version, relaunch_why))
    return 0


def stage_fail(error):
    stage.mark_failed(error)
    log("失败：%s" % error)
    log("安装包已保留，可手动运行：%s" % (stage.load().get("installer") or ""))
    return 1


if __name__ == "__main__":
    sys.exit(main())
