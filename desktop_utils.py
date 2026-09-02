# ============================================================
# desktop_utils.py — 桌面工具集
#
# 职责：
#   - 窗口焦点管理（focus_edge：PowerShell SetForegroundWindow）
#   - 全屏截图（take_screenshot：PowerShell System.Drawing）
#   - 并行任务进度面板（print_progress / reset_progress）
#
# 已归档（2026-08，V4.0.4 精简）：坐标校准系（load_coords/get_bounds/
# calibrate_mode 等）、navigate_to_url、countdown、open_new_edge_window/
# ensure_edge——OCR/坐标时代残留，主通道已 DOM 化，见 archive/。
# ============================================================

import time
import os
import sys
import subprocess
import logging
from datetime import datetime

log = logging.getLogger(__name__)


def run_process_silent(args, timeout=15, **kwargs):
    """无控制台弹窗运行外部命令（PowerShell/taskkill 等）。

    不带 CREATE_NO_WINDOW 时，subprocess 会为每个控制台程序弹出
    一闪而过的黑色终端框——运行链路里多次调用 = 多次闪框
    （打开软件/跑链路时最常见的「黑框一闪」来源）。
    返回 subprocess.CompletedProcess（同 subprocess.run）。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=flags, startupinfo=startupinfo,
                          **kwargs)


def take_screenshot(name="debug"):
    """保存全屏截图到 screenshots/ 目录（PowerShell，无第三方依赖）。"""
    os.makedirs("screenshots", exist_ok=True)
    fn = f"screenshots/{name}_{datetime.now():%H%M%S}.png"
    ps = (
        "Add-Type -A System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        f"$img=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
        "$g=[System.Drawing.Graphics]::FromImage($img);"
        "$g.CopyFromScreen(0,0,0,0,$b.Size);"
        f"$img.Save('{os.path.abspath(fn)}');"
    )
    try:
        run_process_silent(['powershell', '-EP', 'Bypass', '-C', ps])
    except Exception as e:
        log.warning("截图失败：%s", e)


# ============================================================
# 窗口焦点
# ============================================================

# 记录自动化专用的 Edge 窗口 PID，避免 focus_edge() 误抢用户的其他窗口
_edge_automation_pid = None


def focus_edge():
    """将自动化专用的 Edge 窗口调到前台。

    优先使用 open_new_edge_window() 记录的 PID 精准定位；
    若未记录 PID（如手动启动），则回退到查找任意 Edge 窗口。

    返回 True/False 表示是否找到并聚焦了 Edge 窗口。
    """
    global _edge_automation_pid

    # 优先按 PID 精准定位
    if _edge_automation_pid is not None:
        ps = f'''
        Add-Type @"
        using System; using System.Runtime.InteropServices;
        public class W {{ [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h); }}
"@
        $p = Get-Process -Id {_edge_automation_pid} -EA 0
        if($p -and $p.MainWindowHandle -ne 0){{ $h=$p.MainWindowHandle; if([W]::IsIconic($h)){{[W]::ShowWindow($h,9)}}; [W]::SetForegroundWindow($h); Write-Output 'OK' }}
        '''
        try:
            result = run_process_silent(
                ['powershell', '-EP', 'Bypass', '-C', ps], text=True)
            if 'OK' in (result.stdout or ''):
                time.sleep(0.3)
                return True
        except Exception:
            pass
        # PID 定位失败（进程可能已退出），清除记录并回退到通用查找
        log.warning(f"PID {_edge_automation_pid} 定位失败，回退到通用查找")
        _edge_automation_pid = None

    # 回退：查找任意 Edge 窗口
    ps = '''
    Add-Type @"
    using System; using System.Runtime.InteropServices;
    public class W { [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h); }
"@
    $p = Get-Process msedge -EA 0 | ? { $_.MainWindowHandle -ne 0 } | Select -First 1
    if($p){ $h=$p.MainWindowHandle; if([W]::IsIconic($h)){[W]::ShowWindow($h,9)}; [W]::SetForegroundWindow($h); Write-Output 'OK' }
    '''
    try:
        result = run_process_silent(
            ['powershell', '-EP', 'Bypass', '-C', ps], text=True)
        if 'OK' in (result.stdout or ''):
            time.sleep(0.3)
            return True
    except Exception:
        pass
    return False


# ============================================================
# 进度面板（并行任务覆盖式刷新）
# ============================================================

_progress_lines_count = [0]

TITLE_DISPLAY_LEN = 10


def _enable_ansi_windows():
    """Windows 终端启用 ANSI 转义码支持"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


# 启动时启用
_enable_ansi_windows()


def print_progress(progress, total):
    """
    打印并行生成的实时进度面板（覆盖式刷新）。

    使用 ANSI 转义码将光标上移并覆盖旧内容。

    参数：
        progress: {task_id: {status, chars, elapsed, title}}
        total: 任务总数
    """
    lines = []
    lines.append("  ┌───────────────────────────────────────────────┐")
    lines.append(
        f"  │  并行生成进度 ({len(progress)}/{total})"
        f"                          │"
    )
    lines.append("  ├───────────────────────────────────────────────┤")

    for tid in sorted(progress.keys()):
        p = progress[tid]
        status = p['status']
        chars = p['chars']
        elapsed = p['elapsed']
        title = p['title']

        if len(title) > TITLE_DISPLAY_LEN:
            title_display = title[:TITLE_DISPLAY_LEN] + ".."
        else:
            title_display = title.ljust(TITLE_DISPLAY_LEN + 2)

        if '完成' in status:
            icon = '✓'
            status_display = '完成'
        elif '生成中' in status:
            icon = '>'
            status_display = '生成中'
        elif '等待' in status:
            icon = '-'
            status_display = '等待'
        else:
            icon = 'x'
            status_display = '失败'

        elapsed_str = f"{elapsed:.0f}s" if elapsed > 0 else " --"
        chars_str = f"{chars}字" if chars > 0 else "  --"

        line = (f"  │ {icon} {tid}: {title_display} "
                f"{status_display:<4s} {chars_str:>7s} {elapsed_str:>5s} │")
        lines.append(line)

    lines.append("  └───────────────────────────────────────────────┘")

    prev_count = _progress_lines_count[0]
    if prev_count > 0:
        sys.stdout.write(f"\033[{prev_count}A\033[G")

    output_clean = '\n'.join(line + '\033[K' for line in lines)
    sys.stdout.write(output_clean + '\n')
    sys.stdout.flush()

    _progress_lines_count[0] = len(lines)


def reset_progress():
    """重置进度面板行数计数器"""
    _progress_lines_count[0] = 0
