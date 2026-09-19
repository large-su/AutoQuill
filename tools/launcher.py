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

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# 双击场景下 pythonw 以 tools/ 为工作目录启动，项目根不在 sys.path：
# 显式注入，保证 core/ports.py 等顶层包可导入（打包态无 core 时兜底常量）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from core.ports import WEB_PORT
except Exception:  # noqa: BLE001
    WEB_PORT = 8787
try:
    from core import launcher_config   # 关窗行为 / 开机自启（设置页也读写这一份）
except Exception:  # noqa: BLE001
    launcher_config = None
PORT = WEB_PORT
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
    # 源码态优先用当前解释器：启动器是用哪个 python 跑的，就用哪个。
    # 否则 PATH 里 `python` 可能指向无依赖的解释器（如 conda base），
    # 导致"缺少运行依赖"的误判——即使已用 .venv 的 python 启动。
    candidates = [
        ("当前解释器 (sys.executable)", [sys.executable]),
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


def _prewarm_webview():
    """后台预热 pywebview 运行时（WinForms/.NET 程序集加载约 1s）。

    在服务就绪轮询期间并行执行，open_window 的 import 命中模块缓存，
    省去窗口打开前的串行等待。失败静默——不影响主流程。"""
    try:
        import webview  # noqa: F401
        import webview.platforms.winforms  # noqa: F401
    except Exception:
        pass


def open_window(start_hidden=False):
    """打开控制台窗口：pywebview 独立窗口（WebView2 内核），失败回退系统浏览器。

    窗口背景预置为深色（防启动白闪），Win10/11 下标题栏一并染深；
    M4 起挂上托盘图标（关窗默认最小化到托盘，可在设置里切换成直接退出）。
    start_hidden=True（开机自启带 --tray）时窗口只创建不显示，靠托盘图标唤出。
    阻塞直到窗口真正关闭；返回 True=独立窗口，False=回退浏览器（调用方需保持
    服务存活语义，等待服务进程退出）。"""
    try:
        import inspect
        import webview
        t0 = time.time()

        window = webview.create_window(
            "AutoQuill", BASE_URL,
            width=1280, height=820, min_size=(960, 640),
            background_color="#0b0e14",
            hidden=bool(start_hidden),
        )
        # pywebview 6.x 把 icon 参数从 create_window 移到 webview.start
        # （传 create_window 会 TypeError，V4.2.1 线上证据：整个窗口失败、
        # 静默回退浏览器）→ 按签名探测传对位置，兼容新老版本
        start_kwargs = {}
        ico = _window_icon()
        if ico:
            try:
                if "icon" in inspect.signature(webview.start).parameters:
                    start_kwargs["icon"] = ico
            except (ValueError, TypeError):
                pass  # 签名不可探测 → 用默认图标，不阻断启动
        # 窗口显示时记录就位耗时（launcher.log 启动速度审计）
        try:
            def _on_shown():
                _log_diag(
                    f"窗口已显示（open_window 起 {time.time() - t0:.1f}s）")

            window.events.shown += _on_shown
        except Exception:
            pass
        # start(func) 在窗口创建后、显示前调用回调 → 标题栏在用户看到前已染深
        # （start 本身阻塞直到窗口关闭，样式调用不能放在其后面）
        def _on_start():
            _apply_dark_titlebar(window)
            tray = TrayController(window)
            # ★ 等窗口原生对象：start(func) 的回调早于窗口创建（详见 wait_native 注释）
            if not tray.wait_native(timeout=15):
                _log_diag("托盘：等待窗口原生对象超时（15s），退化为普通窗口")
                tray._save_status()
                return
            if tray.attach():
                try:
                    window.events.closing += tray.on_closing
                except Exception as exc:      # noqa: BLE001
                    _log_diag(f"托盘：关窗钩子挂载失败（{exc!r}）")
                if start_hidden:
                    # 开机自启：不弹窗打断用户，只提示一次「已在托盘运行」
                    window.hide()
                    tray.hint_once(
                        launcher_config.load() if launcher_config else {})

        webview.start(_on_start, **start_kwargs)
        return True
    except Exception as exc:
        # 独立窗口失败原因写进诊断日志（否则回退浏览器时无迹可查——
        # V4.2.1 用户反馈"变成浏览器打开"即此路径，曾完全不可见）
        _log_diag(f"独立窗口打开失败，回退系统浏览器：{exc!r}")
        webbrowser.open(BASE_URL)
        return False


# ------------------------------------------------------------
# M4：托盘常驻（关窗最小化 + 托盘菜单操作）
#
# spike 结论（tools/archive/probes/spike_tray.py，真机 11/11 通过）：
#   · NotifyIcon 必须由**消息循环所在线程**创建（用 form.Invoke(Action(...))），
#     否则图标收不到消息、菜单点了没反应；
#   · 关窗用 window.events.closing 返回 False 取消关闭 → 窗口只是 Hide()，
#     webview.start 不返回，进程、服务、自动化都还活着；
#   · 托盘菜单回调跑在 UI 线程，HTTP 请求必须丢到子线程，别卡住界面。
# ------------------------------------------------------------

def api_call(path, method="GET", payload=None, timeout=5):
    """调本机控制台 API（托盘菜单：暂停/恢复、立即执行、读进度）。"""
    url = BASE_URL + path
    body = json.dumps(payload or {}).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw or "{}")


def tray_status_text(status):
    """把 /api/automation 状态压成一行短文本（托盘提示 + 菜单状态行共用）。"""
    if not status:
        return "状态未知"
    per = ((status.get("summary") or {}).get("per_type")) or {}
    parts = []
    for key, label in (("publish_drafts", "发布"), ("full_chain", "撰写")):
        item = per.get(key) or {}
        cap = int(item.get("cap") or 0)
        if cap:
            parts.append("%s %d/%d" % (label, int(item.get("done") or 0), cap))
    body = " · ".join(parts) or "无任务"
    if status.get("running"):
        return "执行中 · " + body
    if status.get("paused"):
        return "已暂停 · " + body
    if status.get("enabled"):
        return "运行中 · " + body
    return "未启用 · " + body


class TrayController:
    """托盘图标 + 右键菜单 + 关窗行为（写法取自真机 spike 的验证结论）。"""

    def __init__(self, window, api_base=BASE_URL):
        self.window = window
        self.api_base = api_base
        self.form = None
        self.notify = None
        self.state_item = None
        self.pause_item = None
        self.available = False        # 托盘建不起来时，关窗必须退化成「真关闭」
        self._quitting = False
        self._stopped = threading.Event()
        self._Action = None
        self._ui = None

    # ---------------- 生命周期 ----------------

    def wait_native(self, timeout=15.0):
        """等窗口原生对象出现。

        ★ 必须等：pywebview 的 `start(func)` 是**先起 func 线程、再创建窗口**
        （webview/__init__.py：thread.start() 在 guilib.create_window 之前），
        所以回调里 `window.native` 可能还是 None。线上实测过这个竞态：
        同一份代码 22:06 拿到（托盘就绪）、22:15 没拿到（退化成普通窗口，
        关窗后用户找不到托盘图标）。
        """
        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            try:
                if self.window.native is not None:
                    return True
            except Exception:      # noqa: BLE001
                pass
            time.sleep(0.1)
        return False

    def attach(self):
        """创建托盘图标；失败只写日志，绝不影响窗口本身。"""
        try:
            import webview.platforms.winforms  # noqa: F401  触发 pythonnet/.NET 装配
            import clr
            clr.AddReference("System.Drawing")
            from System import Action
            from System.Windows.Forms import (NotifyIcon, ContextMenuStrip,
                                              ToolStripMenuItem, ToolTipIcon,
                                              ToolStripSeparator,
                                              MessageBox, MessageBoxButtons,
                                              MessageBoxIcon)
            from System.Drawing import Icon
        except Exception as exc:      # noqa: BLE001
            _log_diag(f"托盘：.NET 组件加载失败，退化为普通窗口（{exc!r}）")
            return False
        self._Action = Action
        self._ui = dict(NotifyIcon=NotifyIcon, ContextMenuStrip=ContextMenuStrip,
                        ToolStripMenuItem=ToolStripMenuItem,
                        ToolTipIcon=ToolTipIcon,
                        ToolStripSeparator=ToolStripSeparator,
                        MessageBox=MessageBox,
                        MessageBoxButtons=MessageBoxButtons,
                        MessageBoxIcon=MessageBoxIcon, Icon=Icon)
        form = self.window.native
        if form is None:
            _log_diag("托盘：拿不到窗口原生对象，退化为普通窗口")
            return False
        self.form = form
        try:
            # NotifyIcon 依赖消息循环 → 交回 UI 线程创建
            form.Invoke(Action(self._build))
        except Exception as exc:      # noqa: BLE001
            _log_diag(f"托盘：创建失败（{exc!r}）")
            return False
        self.available = bool(self.notify)
        self._save_status()
        if self.available:
            _log_diag("托盘：已就绪（关窗 = 最小化到托盘）")
            threading.Thread(target=self._poll_loop, daemon=True).start()
        return self.available

    def _save_status(self):
        """把托盘自检结果写进启动器设置（设置页展示；托盘问题不该只躺在日志里）。"""
        if launcher_config is None:
            return
        try:
            launcher_config.save({"tray_ok": bool(self.available),
                                  "tray_checked_at":
                                      time.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception:      # noqa: BLE001
            pass

    def _build(self):
        """建图标与菜单（必须运行在 UI 线程）。"""
        ui = self._ui
        try:
            icon = self.form.Icon
            if icon is None:
                ico = _window_icon()
                if ico:
                    icon = ui["Icon"](ico)
            notify = ui["NotifyIcon"]()
            if icon is not None:
                notify.Icon = icon
            notify.Text = "AutoQuill"
            menu = ui["ContextMenuStrip"]()
            menu.Items.Add(self._item("打开控制台", self.show_window))
            self.state_item = ui["ToolStripMenuItem"]("状态：加载中…")
            self.state_item.Enabled = False
            menu.Items.Add(self.state_item)
            self.pause_item = self._item("暂停自动化", self.toggle_pause)
            menu.Items.Add(self.pause_item)
            menu.Items.Add(self._item("立即执行下一个", self.run_now))
            menu.Items.Add(ui["ToolStripSeparator"]())
            menu.Items.Add(self._item("退出 AutoQuill", self.quit))
            notify.ContextMenuStrip = menu
            notify.DoubleClick += lambda s, e: self._dispatch(self.show_window)
            notify.Visible = True
            self.notify = notify
        except Exception as exc:      # noqa: BLE001
            _log_diag(f"托盘：图标/菜单创建异常（{exc!r}）")
            self.notify = None

    def _item(self, text, handler):
        item = self._ui["ToolStripMenuItem"](text)
        item.Click += lambda s, e: self._dispatch(handler)
        return item

    # ---------------- 菜单动作（都在子线程跑，界面不卡） ----------------

    def _dispatch(self, fn):
        threading.Thread(target=self._guard, args=(fn,), daemon=True).start()

    def _guard(self, fn):
        try:
            fn()
        except Exception as exc:      # noqa: BLE001
            _log_diag(f"托盘动作失败：{fn.__name__} {exc!r}")

    def show_window(self):
        """打开控制台（双击图标 / 菜单第一项）。"""
        self.window.show()
        try:
            self.form.Activate()
        except Exception:      # noqa: BLE001
            pass

    def toggle_pause(self):
        status = api_call("/api/automation")
        if status.get("paused"):
            api_call("/api/automation/resume", "POST")
        else:
            api_call("/api/automation/pause", "POST",
                     {"reason": "托盘手动暂停"})
        self._refresh()

    def run_now(self):
        """立即执行下一个作业；下一个是「发布草稿」时先确认（不可逆）。"""
        status = api_call("/api/automation")
        job = self._next_job(status)
        if job and job.get("type") == "publish_drafts" and not self._ask(
                "立即发布？",
                "排班里最近的一个作业是「发布草稿」，执行后会真的公开一篇回答"
                "（不可逆）。\n\n确定现在执行吗？"):
            return
        api_call("/api/automation/run-now", "POST", {})
        self._refresh()

    @staticmethod
    def _next_job(status):
        pending = [j for j in (status.get("schedule") or [])
                   if j.get("status") == "planned" and j.get("planned_at")]
        pending.sort(key=lambda j: j.get("planned_at") or "")
        return pending[0] if pending else None

    def _ask(self, title, text):
        """UI 线程上弹确认框（返回 True = 用户点了确定）。"""
        ui = self._ui
        answer = {}

        def _do():
            answer["r"] = ui["MessageBox"].Show(
                text, title, ui["MessageBoxButtons"].OKCancel,
                ui["MessageBoxIcon"].Question)
        try:
            self.form.Invoke(self._Action(_do))
        except Exception:      # noqa: BLE001
            return True        # 弹不出来就别拦着用户（按钮本身就带提示）
        return str(answer.get("r", "")).endswith("OK")

    def quit(self):
        """真退出：放行关闭 → webview.start 返回 → 进程退出（服务由 Job Object 带走）。"""
        status = {}
        try:
            status = api_call("/api/automation")
        except Exception:      # noqa: BLE001
            pass
        if status.get("running") and not self._ask(
                "正在执行，确定退出？",
                "自动化正在执行一个作业，现在退出会中断它（已完成的部分会留在台账里）。"
                "\n\n确定退出吗？"):
            return
        self._quitting = True
        self._stopped.set()
        try:
            if self.notify is not None:
                self.notify.Visible = False       # 先收图标，避免留下幽灵图标
        except Exception:      # noqa: BLE001
            pass
        self.window.destroy()

    # ---------------- 关窗行为 ----------------

    def on_closing(self):
        """窗口 closing 事件：返回 False = 取消关闭（藏进托盘）。"""
        cfg = launcher_config.load() if launcher_config else {}
        if self._quitting or not cfg.get("close_to_tray", True):
            return True                            # 放行：真关闭
        if not self.available:
            # 没有托盘图标还「藏起来」= 用户再也找不回来。
            # 但直接关掉又违背「关窗 = 后台继续跑」的预期 → 退一步：最小化到任务栏
            # （任务栏按钮看得见，程序继续跑；用户也能从设置里改成「直接退出」）
            if self.minimize_to_taskbar():
                _log_diag("托盘不可用：关窗改为最小化到任务栏")
                return False
            _log_diag("托盘不可用且无法最小化：按普通关闭处理")
            return True
        self.window.hide()
        self.hint_once(cfg)
        return False

    def minimize_to_taskbar(self):
        """把窗口最小化（托盘不可用时的兜底）：任务栏按钮还在，程序不退出。"""
        try:
            import clr
            clr.AddReference("System.Windows.Forms")
            from System import Action
            from System.Windows.Forms import FormWindowState

            def _do():
                self.form.WindowState = FormWindowState.Minimized
            self.form.Invoke(Action(_do))
            return True
        except Exception as exc:      # noqa: BLE001
            _log_diag(f"最小化失败：{exc!r}")
            return False

    def hint_once(self, cfg):
        """首次藏进托盘时提示一次（Win11 会把新图标收进折叠区，不提示会以为程序没了）。"""
        if cfg.get("tray_hint_shown") or self.notify is None:
            return
        try:
            self.notify.ShowBalloonTip(4000, "AutoQuill 仍在运行",
                                       "已最小化到托盘：双击图标打开控制台，"
                                       "右键可暂停自动化或退出。",
                                       self._ui["ToolTipIcon"].Info)
        except Exception:      # noqa: BLE001
            pass
        if launcher_config:
            launcher_config.save({"tray_hint_shown": True})

    # ---------------- 状态刷新 ----------------

    def _poll_loop(self):
        while not self._stopped.wait(20):
            try:
                self._refresh()
            except Exception:      # noqa: BLE001
                pass
            # 控制台里的「退出 AutoQuill」：用户点完，这里负责真的退
            try:
                if launcher_config and launcher_config.load().get("quit_requested_at"):
                    _log_diag("收到控制台的退出请求，正在退出")
                    self.quit()
                    return
            except Exception:      # noqa: BLE001
                pass

    def _refresh(self):
        try:
            status = api_call("/api/automation")
        except Exception:      # noqa: BLE001
            status = None
        text = tray_status_text(status)
        self._apply_state(status, text)

    def _apply_state(self, status, text):
        """把状态写到托盘提示与菜单（必须回 UI 线程改控件）。"""
        if self.form is None or self.notify is None:
            return

        def _do():
            try:
                self.notify.Text = ("AutoQuill · " + text)[:62]
                if self.state_item is not None:
                    self.state_item.Text = "状态：" + text
                if self.pause_item is not None:
                    self.pause_item.Text = ("恢复自动化" if (status or {}).get("paused")
                                            else "暂停自动化")
            except Exception:      # noqa: BLE001
                pass
        try:
            self.form.Invoke(self._Action(_do))
        except Exception:      # noqa: BLE001
            pass


def _set_title(title):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def _redirect_frozen_stdio():
    """stdout/stderr 为 None（pythonw / windowed 打包态）时重定向到
    DATA_ROOT/logs/launcher.log：启动器输出留档可查，且避免 print
    因 stdout 为 None 崩溃。
    - 打包态：PyInstaller windowed 置空 stdout；
    - 源码态用 pythonw 双击启动时同样置空（无控制台）。
    普通 python 终端（有 stdout）保持原样，不重定向。"""
    # 需要重定向的场景：打包态（frozen，windowed 无控制台，旧语义保持）
    # 或 pythonw 源码态（stdout 为 None）。普通 python 终端不重定向。
    needs = getattr(sys, "frozen", False) \
        or sys.stdout is None or sys.stderr is None
    if not needs:
        return
    try:
        log_root = data_root() / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        stream = open(log_root / "launcher.log", "a",
                      encoding="utf-8", buffering=1)
        # needs=True 时无条件替换（frozen 或 stdout 为 None 两种场景）
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
    # 启动耗时审计（launcher.log 时间戳可还原各阶段耗时）
    t_start = time.time()
    _log_diag("启动器开始启动")

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
    # 开机自启带 --tray：窗口只创建不显示，直接驻留托盘（不打断用户）
    start_hidden = "--tray" in sys.argv
    if launcher_config is not None:
        # 清掉上一次会话留下的退出请求，否则「刚启动就自己退了」
        try:
            launcher_config.clear_quit_request()
        except Exception:      # noqa: BLE001
            pass
    if launcher_config is not None and not start_hidden:
        # 自愈：设置里开着自启、注册表项却没了（换目录/被杀软清）→ 补写
        try:
            warn = launcher_config.ensure_autostart_consistent()
            if warn:
                _log_diag("开机自启自愈失败：%s" % warn)
        except Exception:      # noqa: BLE001
            pass
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
        open_window(start_hidden=start_hidden)
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
    # 服务就绪轮询期间并行预热 pywebview（WinForms/.NET 程序集加载
    # 约 1s，串行等会拉长启动）——open_window 的 import 命中缓存
    threading.Thread(target=_prewarm_webview, daemon=True).start()

    ready = False
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # 服务进程退出 = 启动失败
        if service_alive():
            ready = True
            break
        time.sleep(0.3)

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

    _log_diag(f"服务就绪（启动后 {time.time() - t_start:.1f}s）")
    print(f"服务已就绪：{BASE_URL}")
    print("正在打开 AutoQuill 窗口…")
    try:
        if not open_window(start_hidden=start_hidden):
            # 回退浏览器：保持等待服务退出（Job Object 在进程退出时清理服务）
            proc.wait()
    except KeyboardInterrupt:
        pass
    print("窗口已关闭，正在停止服务…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
