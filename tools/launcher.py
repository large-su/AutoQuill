#!/usr/bin/env python3
"""AutoQuill 一键启动器

双击即可启动 Web 控制台（独立窗口，pywebview / WebView2 内核）：
  源码态：检查 Python 环境与依赖（fastapi / uvicorn / playwright / webview），
          后台启动 `python main.py --web`（日志写 DATA_ROOT/logs/webui.log）
  打包态：跳过环境检查，首启迁移旧数据（旧解压目录 → %APPDATA%/AutoQuill），
          拉起自身 `AutoQuill.exe --service` 作为服务进程
  通用：8787 已有服务 → 直接开独立窗口复用；关窗/强杀 → Job Object 连带
        服务进程清理；就绪后打开独立窗口（pywebview 失败时回退系统浏览器）

打包态数据目录与 core/paths.py 保持一致（%APPDATA%/AutoQuill），
程序文件（含服务代码）全部内置于 exe，不依赖系统 Python。
"""

import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
SERVICE_ARGS = ["main.py", "--web"]
READY_TIMEOUT = 40  # 服务就绪等待上限（秒）——打包态首次解压较慢
DEPCHECK_TIMEOUT = 60  # 依赖检查超时（秒）——playwright 导入较慢


def project_root():
    """项目根目录：打包后 = exe 所在目录；开发时 = tools/ 的上级。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root():
    """数据根目录：打包态 = %APPDATA%/AutoQuill（与 core/paths 一致）；
    源码态 = 项目根。"""
    if getattr(sys, "frozen", False):
        try:
            from core import paths
            return Path(paths.DATA_ROOT)
        except Exception:
            pass
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "AutoQuill"
    return project_root()


def migrate_legacy_data():
    """打包态首启：旧解压目录数据 → %APPDATA%/AutoQuill（幂等）。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        from core import paths
        result = paths.migrate_legacy_data()
        if result["migrated"]:
            print("已迁移旧版数据到用户数据目录（%APPDATA%/AutoQuill）。")
        elif result["error"]:
            print(f"数据迁移失败（可稍后重试）：{result['error']}")
    except Exception as exc:
        print(f"数据迁移跳过：{exc}")


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
    r = _run_quiet(python_cmd + ["-c", "import fastapi, uvicorn, playwright, webview"], timeout=DEPCHECK_TIMEOUT)
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
    """后台启动 Web 控制台服务（无窗口），日志追加到 DATA_ROOT/logs/webui.log。

    打包态：python_cmd 为 None，直接拉起自身 `AutoQuill.exe --service`
    （服务代码内置于 exe，不依赖系统 Python）；源码态仍走 python main.py。"""
    log_root = data_root() / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = open(log_root / "webui.log", "ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--service"]
    else:
        cmd = list(python_cmd) + SERVICE_ARGS
    proc = subprocess.Popen(
        cmd,
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


def _pause():
    try:
        input("\n按回车退出…")
    except EOFError:
        pass


def _log_diag(msg):
    """启动器诊断日志（打包态用户可反馈 %APPDATA%/AutoQuill/logs/launcher.log）。"""
    try:
        path = data_root() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "launcher.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _find_titlebar_handle(window):
    """多路径取窗口句柄：pywebview 版本/后端差异时逐级兜底。"""
    if not window or not window.native:
        return 0
    try:
        handle = window.native.Handle  # pywebview 6.x WinForms：Form.Handle
        if handle:
            return handle
    except AttributeError:
        pass
    try:
        handle = window.native.get_handle()  # 旧版本 API
        if handle:
            return handle
    except Exception:
        pass
    if os.name == "nt":
        # 兜底：按窗口标题找（防 native 结构变化导致取不到句柄）。
        # 显式声明签名：默认 restype 按 32 位截断，64 位句柄会错位
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        hwnd = user32.FindWindowW(None, "AutoQuill")
        if hwnd:
            return hwnd
    return 0


def _handle_to_int(handle):
    """把 .NET System.IntPtr 转成 int：先试 int()，失败再试 .NET
    ToInt64()/ToInt32()（真实 IntPtr 无 __int__，int() 必失败）。
    全部失败返回 0（调用方记日志，不抛不静默）。"""
    try:
        return int(handle)
    except Exception:
        pass
    for method in ("ToInt64", "ToInt32"):
        fn = getattr(handle, method, None)
        if fn is None:
            continue
        try:
            return int(fn())
        except Exception:
            continue
    return 0


def _apply_dark_titlebar(window):
    """Windows 10/11：把标题栏染成与界面一致的深色（DWM 属性）。

    WebView2 无 dark_title_bar 参数，深色标题栏需原生 API。
    失败原因写入日志（launcher.log），方便安装版用户反馈排查；
    页面本身已是深色，仅标题栏会白一点，不阻塞启动。"""
    def _apply():
        try:
            if os.name != "nt" or not window or not window.native:
                return
            import ctypes
            from ctypes import wintypes

            handle = _find_titlebar_handle(window)
            if not handle:
                _log_diag("深色标题栏：未取得窗口句柄，跳过")
                return
            # pywebview WinForms 的 Handle 是 .NET IntPtr 对象（非 int），
            # ctypes 直接传会 TypeError: wrong type（线上证据）→ 转 int。
            # 注意真实 IntPtr 无 __int__（int() 报 "not 'IntPtr'"，V4.1.4
            # 线上证据）→ 兜底用 .NET 方法 ToInt64()/ToInt32()
            if not isinstance(handle, int):
                handle = _handle_to_int(handle)
                if not handle:
                    _log_diag("深色标题栏：句柄无法转 int"
                              "（无 __int__ 且无 ToInt64/ToInt32）")
                    return
            # 显式声明签名：句柄按 64 位传递，防默认 c_int 截断
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            dwm.argtypes = [wintypes.HWND, wintypes.DWORD,
                            wintypes.LPCVOID, wintypes.DWORD]
            dwm.restype = ctypes.HRESULT
            # DWMWA_USE_IMMERSIVE_DARK_MODE：20（Win10 1809+ / Win11），
            # 19 为旧值（更早的 Win10 构建用 19）
            last_hr = 0
            for attr in (20, 19):
                value = wintypes.BOOL(True)
                hr = dwm(wintypes.HWND(handle), attr, ctypes.byref(value),
                         ctypes.sizeof(value))
                if hr == 0:
                    return
                last_hr = hr
            _log_diag(f"深色标题栏：DwmSetWindowAttribute 失败"
                      f" HRESULT=0x{last_hr & 0xFFFFFFFF:08x}")
        except Exception as exc:
            _log_diag(f"深色标题栏：{exc}")

    _apply()
    # 窗口显示过程可能重置 DWM 属性（慢机器/冷启动上更明显），
    # 显示后再补设一次，覆盖时序竞态
    try:
        window.events.shown += lambda: threading.Timer(
            0.3, _apply).start()
    except Exception:
        pass


def _window_icon():
    """窗口图标：源码态 = 项目 assets/；打包态优先 _MEIPASS（PyInstaller
    onedir 把 datas 放进 _internal/），兜底 exe 目录。文件缺失返回 None
    （pywebview 用默认图标，不阻断启动）。"""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "assets" / "AutoQuill.ico")
    candidates.append(project_root() / "assets" / "AutoQuill.ico")
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def open_window():
    """打开控制台窗口：pywebview 独立窗口（WebView2 内核），失败回退系统浏览器。

    窗口背景预置为深色（防启动白闪），Win10/11 下标题栏一并染深。
    阻塞直到窗口关闭；返回 True=独立窗口，False=回退浏览器（调用方需保持
    服务存活语义，等待服务进程退出）。"""
    try:
        import webview

        window = webview.create_window(
            "AutoQuill", BASE_URL,
            width=1280, height=820, min_size=(960, 640),
            background_color="#0b0e14",
            icon=_window_icon(),
        )
        # start(func) 在窗口创建后、显示前调用回调 → 标题栏在用户看到前已染深
        # （start 本身阻塞直到窗口关闭，样式调用不能放在其后面）
        webview.start(lambda: _apply_dark_titlebar(window))
        return True
    except Exception:
        webbrowser.open(BASE_URL)
        return False


def _set_title(title):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def _redirect_frozen_stdio():
    """打包态（windowed 无控制台）把 stdout/stderr 重定向到
    DATA_ROOT/logs/launcher.log：启动器输出留档可查，且避免 print
    因 stdout 为 None（windowed 模式下 PyInstaller 置空）崩溃。
    源码态不重定向——保留调试终端。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        log_root = data_root() / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        stream = open(log_root / "launcher.log", "a",
                      encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream
        if sys.stdin is None:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
    except Exception:
        pass


def _message_box(title, text):
    """打包态无控制台时用 Windows 消息框提示（源码态回退 print）。"""
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            from ctypes import wintypes

            ctypes.windll.user32.MessageBoxW(
                None, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(f"{title}\n{text}")


def main():
    # 必须先于任何 print：windowed 模式下 sys.stdout 可能为 None
    _redirect_frozen_stdio()
    if sys.stdout and not sys.stdout.isatty():
        sys.stdout.reconfigure(line_buffering=True)  # 管道/重定向时也让输出即时可见

    # --service：打包态服务子进程入口（启动器拉起自身后进入服务本体）。
    # 先做数据目录 bootstrap，再走 main.py 主入口（与 --web 等价，
    # 含日志 FileHandler / 取消钩子 / uvicorn）。
    if '--service' in sys.argv:
        migrate_legacy_data()
        import main
        main.main()
        return 0

    root = project_root()
    frozen = getattr(sys, "frozen", False)
    _set_title("AutoQuill 启动器")
    print("=" * 44)
    print("  AutoQuill 一键启动" + ("（正式版）" if frozen else ""))
    print("=" * 44)

    if not frozen and not (root / "main.py").exists():
        print("未找到 main.py！")
        print("请把启动器放在 AutoQuill 项目根目录（与 main.py 同级）后重试。")
        _pause()
        return 1

    # 服务已在跑 → 复用，直接开窗口
    if service_alive():
        print(f"检测到 AutoQuill 服务已在运行（{BASE_URL}），直接打开窗口…")
        open_window()
        print("注意：该服务并非本启动器启动，关闭本窗口不会停止它。")
        return 0

    if frozen:
        # 打包态：无需 Python/依赖检查，首启迁移旧数据
        print("运行模式：正式版（内置运行环境，无需安装 Python）")
        migrate_legacy_data()
        python_cmd = None
    else:
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
        tail = _read_log(data_root() / "logs" / "webui.log")
        if getattr(sys, "frozen", False):
            # 打包态无控制台：直接弹框，避免用户只见闪退不知原因
            _message_box("AutoQuill 服务启动失败",
                         "服务未在等待窗口内就绪，最近日志：\n\n"
                         + tail[-1500:] + "\n\n完整日志："
                         + str(data_root() / "logs" / "webui.log"))
        else:
            print("服务启动失败，最近日志：")
            print(tail[-1000:] or "（无日志内容）")
            _pause()
        return 1

    print(f"服务已就绪：{BASE_URL}")
    print("正在打开 AutoQuill 窗口…")
    try:
        if not open_window():
            # 回退浏览器：保持等待服务退出（Job Object 在进程退出时清理服务）
            proc.wait()
    except KeyboardInterrupt:
        pass
    print("窗口已关闭，正在停止服务…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
