#!/usr/bin/env python3
"""AutoQuill 一键启动器（轻量版）

双击即可启动 Web 控制台：
  1. 检查 Python 环境与运行依赖（fastapi / uvicorn / playwright）
  2. 若 8787 端口已有服务在跑 → 直接打开浏览器（复用）
  3. 否则后台启动 `python main.py --web`（无黑窗、日志写 logs/webui.log）
  4. 等待服务就绪后自动打开浏览器
  5. 保持运行；关闭本窗口 / Ctrl+C / 进程被杀 → 服务进程随之清理（Job Object）

使用要求：启动器 exe 必须放在 AutoQuill 项目根目录（与 main.py 同级）。
纯标准库实现，便于 PyInstaller 打出体积很小的 exe。
"""

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
SERVICE_ARGS = ["main.py", "--web"]
READY_TIMEOUT = 30  # 服务就绪等待上限（秒）
DEPCHECK_TIMEOUT = 60  # 依赖检查超时（秒）——playwright 导入较慢


def project_root():
    """项目根目录：打包后 = exe 所在目录；开发时 = tools/ 的上级。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _run_quiet(cmd, timeout=20):
    """执行命令并吞掉输出，失败返回 None。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_python():
    """找一个能用的 Python 解释器，返回 (命令列表, 可执行文件路径)。"""
    candidates = [
        ("python", ["python"]),
        ("py -3（Python 启动器）", ["py", "-3"]),
        ("python3", ["python3"]),
    ]
    for name, cmd in candidates:
        r = _run_quiet(cmd + ["-c", "import sys; print(sys.executable)"])
        if r is not None and r.returncode == 0:
            return cmd, (r.stdout.strip() or name)
    return None, None


def check_deps(python_cmd):
    """确认运行依赖齐全。"""
    r = _run_quiet(python_cmd + ["-c", "import fastapi, uvicorn, playwright"], timeout=DEPCHECK_TIMEOUT)
    return r is not None and r.returncode == 0


def service_alive(timeout=2):
    """8787 端口是否有 AutoQuill 服务在响应。"""
    try:
        with urllib.request.urlopen(BASE_URL + "/api/status", timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def _assign_kill_on_close_job(proc):
    """Windows Job Object：本进程退出（含被强杀）时自动终止子进程，杜绝孤儿进程。

    关闭启动器窗口 = 杀掉服务进程，这是「关窗口即停止」的机制保证。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            return None
        kernel32.AssignProcessToJobObject(job, int(proc._handle))
        return job  # 句柄存活着，本进程退出时系统自动触发 kill-on-close
    except Exception:
        return None


def start_service(python_cmd, root):
    """后台启动 Web 控制台服务（无窗口），日志追加到 logs/webui.log。"""
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = open(log_dir / "webui.log", "ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        python_cmd + SERVICE_ARGS,
        cwd=str(root),
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    _assign_kill_on_close_job(proc)
    return proc


def _read_log(path):
    """读取服务日志，兼容 utf-8 / gbk 两种编码。"""
    if not path.exists():
        return ""
    data = path.read_bytes()
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _print_tail(path, lines=30):
    text = _read_log(path).splitlines()
    if not text:
        print("（无日志内容）")
        return
    print("\n".join(text[-lines:]))


def _pause():
    try:
        input("\n按回车退出…")
    except EOFError:
        pass


def _set_title(title):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def main():
    if sys.stdout and not sys.stdout.isatty():
        sys.stdout.reconfigure(line_buffering=True)  # 管道/重定向时也让输出即时可见
    root = project_root()
    _set_title("AutoQuill 启动器")
    print("=" * 44)
    print("  AutoQuill 一键启动")
    print("=" * 44)

    if not (root / "main.py").exists():
        print("未找到 main.py！")
        print("请把启动器放在 AutoQuill 项目根目录（与 main.py 同级）后重试。")
        _pause()
        return 1

    # 服务已在跑 → 复用，直接开浏览器
    if service_alive():
        print(f"检测到 AutoQuill 服务已在运行（{BASE_URL}），直接打开浏览器…")
        webbrowser.open(BASE_URL)
        print("注意：该服务并非本启动器启动，关闭本窗口不会停止它。")
        _pause()
        return 0

    python_cmd, python_exe = find_python()
    if python_cmd is None:
        print("未找到 Python 环境。")
        print("请先安装 Python 3.10+（安装时勾选 Add to PATH），再运行本启动器。")
        _pause()
        return 1
    print(f"Python 环境：{python_exe}")

    if not check_deps(python_cmd):
        print("缺少运行依赖（fastapi / uvicorn / playwright）。")
        print("请在项目目录执行：")
        print("    pip install -r requirements.txt")
        _pause()
        return 1

    print("正在启动服务…（首次约 3-5 秒）")
    proc = start_service(python_cmd, root)

    ready = False
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # 服务进程退出 = 启动失败
        if service_alive():
            ready = True
            break
        time.sleep(0.5)

    if not ready:
        print("服务启动失败，最近日志：")
        _print_tail(root / "logs" / "webui.log")
        _pause()
        return 1

    print(f"服务已就绪：{BASE_URL}")
    print("正在打开浏览器…")
    webbrowser.open(BASE_URL)
    print()
    print("AutoQuill 正在运行。")
    print("使用期间请保持本窗口开启，关闭本窗口即可停止服务。")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止服务…")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("服务已停止，再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
