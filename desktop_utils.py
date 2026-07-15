# ============================================================
# desktop_utils.py — 桌面自动化工具集
#
# 职责：电脑端的感知与操作
#   - 浏览器控制（导航、标签页、粘贴、截图）
#   - 窗口焦点管理
#   - 坐标管理与校准
#   - 并行任务进度面板
#
# 与 ocr_utils.py 的分工：
#   ocr_utils.py = "看"（OCR 识别、文字定位、图标匹配）
#   desktop_utils.py = "动"（点击、导航、窗口管理、坐标校准）
# ============================================================

import pyautogui
import pyperclip
import time
import os
import sys
import json
import subprocess
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ============================================================
# 浏览器操作
# ============================================================

def navigate_to_url(url):
    """在当前标签页导航到指定 URL"""
    from config import random_delay, WAIT_HOTKEY, WAIT_PAGE_LOAD
    log.info(f"导航: {url}")
    pyautogui.hotkey('ctrl', 'l')
    random_delay(WAIT_HOTKEY)
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.15)
    pyperclip.copy(url)
    pyautogui.hotkey('ctrl', 'v')
    random_delay(WAIT_HOTKEY)
    pyautogui.press('enter')
    random_delay(WAIT_PAGE_LOAD)


def open_new_tab(url=None):
    """打开新标签页，可选导航到 URL"""
    from config import random_delay, WAIT_TAB_OPEN
    pyautogui.hotkey('ctrl', 't')
    random_delay(WAIT_TAB_OPEN)
    if url:
        navigate_to_url(url)


def close_current_tab():
    """关闭当前标签页"""
    from config import random_delay, WAIT_HOTKEY
    pyautogui.hotkey('ctrl', 'w')
    random_delay(WAIT_HOTKEY)


def paste_text(text=None):
    """粘贴文本（如果提供了 text 则先复制到剪贴板）"""
    from config import random_delay, WAIT_PASTE
    if text is not None:
        pyperclip.copy(text)
        time.sleep(0.15)
    pyautogui.hotkey('ctrl', 'v')
    random_delay(WAIT_PASTE)


def grab_current_url():
    pyautogui.hotkey('ctrl', 'l')
    time.sleep(0.2)
    pyautogui.hotkey('ctrl', 'c')
    time.sleep(0.2)
    url = pyperclip.paste()
    
    # 关键：显式抬起所有可能粘连的修饰键，防止下面的 Esc 变成 Ctrl+Esc(=Win键)
    pyautogui.keyUp('ctrl')
    pyautogui.keyUp('shift')
    pyautogui.keyUp('alt')
    time.sleep(0.05)
    
    pyautogui.press('escape')
    time.sleep(0.05)
    pyautogui.press('escape')
    time.sleep(0.15)
    return url


def take_screenshot(name="debug"):
    """保存截图到 screenshots/ 目录"""
    os.makedirs("screenshots", exist_ok=True)
    fn = f"screenshots/{name}_{datetime.now():%H%M%S}.png"
    pyautogui.screenshot().save(fn)


def countdown(s=5):
    """倒计时提示"""
    for i in range(s, 0, -1):
        print(f"  {i}...")
        time.sleep(1)


# ============================================================
# 并行 Web 模式：独立 Edge 窗口 + 多标签页切换
# ============================================================

EDGE_EXE_DEFAULT = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 记录自动化专用的 Edge 窗口 PID，避免 focus_edge() 误抢用户的其他窗口
_edge_automation_pid = None


def open_new_edge_window(url="about:blank", edge_exe=None):
    """
    启动一个独立的 Edge 窗口（继承主浏览器的登录态/cookie）。

    参数：
        url: 初始标签页打开的 URL
        edge_exe: Edge 可执行文件路径，None 则使用默认 Win11 路径

    返回：
        subprocess.Popen 句柄（用于后续关闭窗口）

    说明：
        - 使用 --new-window 强制开新窗口（不复用已有的 Edge 窗口）
        - 不传 --user-data-dir，默认共享主 Profile，自动继承登录态
        - 启动后 Win+Up 最大化，保证校准坐标一致
    """
    import shutil
    exe = edge_exe or EDGE_EXE_DEFAULT
    if not os.path.exists(exe):
        fallback = shutil.which("msedge")
        if fallback:
            exe = fallback
            log.warning(f"默认 Edge 路径未找到，改用 PATH 中的：{exe}")
        else:
            raise FileNotFoundError(
                f"未找到 Edge 可执行文件：{exe}\n"
                f"请确认已安装 Microsoft Edge，或在 "
                f"desktop_utils.py 的 EDGE_EXE_DEFAULT 中配置正确路径"
            )

    log.info(f"启动独立 Edge 窗口：{url}")
    # --start-maximized 让 Edge 启动时就直接以最大化状态打开窗口，
    # 比启动后再用 Win+Up 快捷键更可靠（不依赖焦点/窗口状态）
    proc = subprocess.Popen([exe, "--new-window", "--start-maximized", url])

    # 记录 PID，供 focus_edge() 精准定位我们的窗口
    global _edge_automation_pid
    _edge_automation_pid = proc.pid
    log.info(f"  自动化窗口 PID: {_edge_automation_pid}")

    # 等窗口创建 + 页面初步加载
    time.sleep(2.5)

    return proc


def switch_to_tab(position):
    """
    切换到当前窗口内第 position 个标签页（1-9）。

    使用 Ctrl+1..Ctrl+9 快捷键，position=9 在 Edge 中是"跳到最后一个 tab"，
    其他位置精确对应 tab 顺序。并行模式下应保持 num_slots ≤ 8 以避免歧义。
    """
    if not (1 <= position <= 9):
        raise ValueError(f"标签页位置必须在 1-9 之间：{position}")
    pyautogui.hotkey('ctrl', str(position))
    time.sleep(0.25)


def close_browser_window():
    """
    关闭当前 Edge 窗口（含所有标签页）。

    Ctrl+Shift+W 是 Edge 的"关闭窗口"快捷键，比逐个 Ctrl+W 更安全
    （避免关到最后一个 tab 时触发"确认关闭"对话框）。
    """
    pyautogui.hotkey('ctrl', 'shift', 'w')
    time.sleep(0.5)


# ============================================================
# 窗口焦点
# ============================================================

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
            result = subprocess.run(
                ['powershell', '-EP', 'Bypass', '-C', ps],
                capture_output=True, text=True, timeout=5
            )
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
        result = subprocess.run(
            ['powershell', '-EP', 'Bypass', '-C', ps],
            capture_output=True, text=True, timeout=5
        )
        if 'OK' in (result.stdout or ''):
            time.sleep(0.3)
            return True
    except Exception:
        pass

    # PowerShell 方案失败，尝试 Alt+Tab 兜底（不保证正确窗口）
    try:
        pyautogui.hotkey('alt', 'tab')
        time.sleep(0.3)
    except Exception:
        pass
    return False


def _is_foreground_maximized():
    """
    检测当前前台窗口是否已最大化。

    通过 PowerShell 查询前台窗口的 WindowPlacement.showCmd：
      3 = SW_SHOWMAXIMIZED（已最大化）
      1 = SW_SHOWNORMAL（普通状态）

    返回 True（已最大化）/ False（未最大化或无法检测）。
    """
    ps = '''
    Add-Type @"
    using System; using System.Runtime.InteropServices;
    public struct RECT { public int L,T,R,B; }
    public struct WINDOWPLACEMENT {
        public int length; public int flags; public int showCmd;
        public int ptMinX, ptMinY, ptMaxX, ptMaxY;
        public RECT rcNormal;
    }
    public class WP {
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr h, ref WINDOWPLACEMENT wp);
    }
"@
    $h = [WP]::GetForegroundWindow()
    if ($h -eq [IntPtr]::Zero) { Write-Output 'ERR'; return }
    $wp = New-Object WINDOWPLACEMENT
    $wp.length = [Runtime.InteropServices.Marshal]::SizeOf($wp)
    [WP]::GetWindowPlacement($h, [ref]$wp) | Out-Null
    Write-Output $wp.showCmd
    '''
    try:
        import subprocess
        result = subprocess.run(
            ['powershell', '-EP', 'Bypass', '-C', ps],
            capture_output=True, text=True, timeout=5
        )
        show_cmd = (result.stdout or '').strip()
        return show_cmd == '3'
    except Exception:
        return False


def ensure_edge(url="about:blank"):
    """
    启动一个全新的 Edge 窗口用于自动化操作。

    为避免干扰用户已有的浏览器窗口（个人标签页、工作内容等），
    始终启动独立的新窗口，不复用已有窗口。
    打开后检测窗口状态，未最大化时才执行 Win+Up 最大化。

    参数：
        url: 新窗口打开的初始 URL（默认空白页）

    返回：
        True   Edge 已就绪
    """
    log.info("启动独立 Edge 窗口（不干扰已有浏览器窗口）...")
    try:
        open_new_edge_window(url)
        time.sleep(1.5)  # 等窗口完全创建
        focus_edge()
        time.sleep(0.3)

        # 只在未最大化时才执行
        if _is_foreground_maximized():
            log.info("  窗口已最大化，跳过 Win+Up")
        else:
            pyautogui.hotkey('win', 'up')
            time.sleep(0.8)
            log.info("  窗口已最大化（Win+Up）")

        log.info("Edge 已启动、最大化并就绪")
        return True
    except Exception as e:
        log.error(f"无法启动 Edge 浏览器：{e}")
        return False


def wait_for_user(prompt="按 Enter >> ", auto_focus=True):
    """等待用户确认，可选自动聚焦浏览器"""
    input(prompt)
    if auto_focus:
        focus_edge()
        time.sleep(0.3)


# ============================================================
# OCR 辅助点击
# ============================================================

def make_region(x1, y1, x2, y2):
    """
    将两个对角坐标转换为 pyautogui region 格式 (x, y, width, height)。

    用法：make_region(left, top, right, bottom)
    """
    return (int(x1), int(y1), int(x2 - x1), int(y2 - y1))


def ocr_click_text(target_text, region=None, retries=3, wait=0.8,
                   log_name=None, click_offset=(0, 0)):
    """
    全屏/指定区域 OCR 查找文字并点击。

    参数：
        target_text: 要查找的文字
        region: 搜索区域 (x, y, w, h)，None 为全屏
        retries: 重试次数
        wait: 每次重试等待秒数
        log_name: 日志中显示的名称（默认用 target_text）
        click_offset: 点击偏移 (dx, dy)

    返回 True/False
    """
    from ocr_utils import find_text_on_screen
    from config import random_mouse_duration
    name = log_name or target_text

    for attempt in range(retries):
        pos = find_text_on_screen(target_text, region=region)
        if pos:
            x = int(pos[0] + click_offset[0])
            y = int(pos[1] + click_offset[1])
            log.info(f"  OCR 定位「{name}」→ ({x}, {y})")
            pyautogui.click(x, y, duration=random_mouse_duration())
            time.sleep(0.3)
            return True
        if attempt < retries - 1:
            log.info(f"  未找到「{name}」，重试（{attempt+1}/{retries}）")
            time.sleep(wait)

    log.warning(f"  OCR 未找到「{name}」")
    return False



# ============================================================
# 坐标管理
# ============================================================

COORDS_FILE = os.path.join("data", "coordinates.json")

COORD_KEYS_BASE = {
    "ocr_content_left":   "知乎正文区域的左边界",
    "ocr_content_right":  "知乎正文区域的右边界（侧边栏左侧）",
    "ocr_content_top":    "知乎正文区域的上边界（标签栏下方）",
    "ocr_content_bottom": "知乎正文区域的下边界",
}

COORDS = {}


def _get_required_keys():
    """返回必须校准的坐标 key"""
    return dict(COORD_KEYS_BASE)


def load_coords():
    """加载校准坐标，返回是否所有必要坐标都已校准"""
    global COORDS
    if not os.path.exists(COORDS_FILE):
        return False
    try:
        with open(COORDS_FILE, 'r', encoding='utf-8') as f:
            COORDS = json.load(f).get("coordinates", {})
        required = _get_required_keys()
        return all(k in COORDS for k in required)
    except Exception:
        return False


def save_coords(c):
    """保存校准坐标到文件"""
    with open(COORDS_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "coordinates": c,
            "screen_size": list(pyautogui.size()),
            "at": datetime.now().isoformat()
        }, f, ensure_ascii=False, indent=2)


def get_coord(key):
    """获取指定 key 的校准坐标 (x, y)"""
    if key not in COORDS:
        raise RuntimeError(f"'{key}' 未校准！--calibrate")
    return COORDS[key][0], COORDS[key][1]



# ============================================================
# 校准系统
# ============================================================

_HINTS = {
    "ocr_content_left":   "将鼠标移到知乎回答正文最左边（文字起始位置）",
    "ocr_content_right":  "将鼠标移到知乎回答正文最右边（侧边栏之前）",
    "ocr_content_top":    "将鼠标移到知乎页面内容上边界（标签栏下方）",
    "ocr_content_bottom": "将鼠标移到知乎页面内容下边界",
}


def calibrate_mode():
    """交互式坐标校准"""
    from config import LLM_MODE, WEB_DRIVER_NAME

    required = _get_required_keys()

    # 获取当前 Web 驱动的可选校准坐标
    driver_keys = {}
    driver_hints = {}
    if LLM_MODE == "web":
        try:
            from web_drivers import get_driver
            drv = get_driver()
            driver_keys = drv.get_calibration_keys()
            driver_hints = drv.get_calibration_hints()
        except Exception:
            pass

    print(f"""
    ===== 校准模式 =====
    必须校准 {len(required)} 个基础边界点
    """)

    if LLM_MODE == "web" and driver_keys:
        from config import WEB_DRIVERS
        drv_cfg = WEB_DRIVERS.get(WEB_DRIVER_NAME, {})
        icon_rel = drv_cfg.get("copy_icon", "")
        if icon_rel:
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), icon_rel
            )
            if os.path.exists(icon_path):
                print(f"  ✓ 检测到 {icon_rel}，复制按钮将用图标匹配")
            else:
                print("  提示：网页模式下复制按钮可通过以下两种方式定位：")
                print(f"    方式1（推荐）：截取复制图标保存为 {icon_rel}")
                print("    方式2：校准时额外标定复制按钮坐标（下方会询问）")

    existing = {}
    if os.path.exists(COORDS_FILE):
        try:
            with open(COORDS_FILE, 'r', encoding='utf-8') as f:
                existing = json.load(f).get("coordinates", {})
        except Exception:
            pass

    if existing:
        print("\n  已有坐标：")
        all_keys = dict(required)
        all_keys.update(driver_keys)
        for k, d in all_keys.items():
            if k in existing:
                print(f"    ✓ {d}: ({existing[k][0]}, {existing[k][1]})")
            else:
                opt = "（可选）" if k in driver_keys else ""
                print(f"    ✗ {d}: 未校准 {opt}")

        missing = [k for k in required if k not in existing]
        have = [k for k in required if k in existing]

        if not missing:
            print("\n  基础坐标全部已校准。要重新校准哪些？")
            for i, k in enumerate(have):
                print(f"    {i+1}. {required[k]}")
            redo = input("\n  编号（如 1,3）或回车跳过 >> ").strip()
            if not redo:
                for dk, dd in driver_keys.items():
                    if dk not in existing:
                        add = input(
                            f"  是否校准「{dd}」坐标（作为兜底）？(y/n) >> "
                        ).strip().lower()
                        if add == 'y':
                            _calibrate_single_key(dk, existing, driver_hints)
                return
            try:
                keys_to_cal = [
                    have[int(x.strip())-1] for x in redo.split(',')
                ]
            except (ValueError, IndexError):
                return
        else:
            keys_to_cal = list(missing)
            if have:
                print(f"\n  需校准 {len(missing)} 个。已有的要重新校准吗？")
                for i, k in enumerate(have):
                    print(f"    {i+1}. {required[k]}")
                redo = input("  编号（直接回车跳过）>> ").strip()
                if redo:
                    try:
                        keys_to_cal += [
                            have[int(x.strip())-1] for x in redo.split(',')
                        ]
                    except (ValueError, IndexError):
                        pass
    else:
        keys_to_cal = list(required.keys())

    input("\n  按 Enter 开始校准...")
    c = dict(existing)
    all_hints = dict(_HINTS)
    all_hints.update(driver_hints)
    all_descs = dict(required)
    all_descs.update(driver_keys)

    for k in keys_to_cal:
        desc = all_descs.get(k, k)
        print(f"\n--- {desc} ---")
        print(f"    {all_hints.get(k, '将鼠标移到目标位置')}")
        input("    按 Enter 后 5 秒倒计时...")
        countdown(5)
        x, y = pyautogui.position()
        c[k] = [x, y]
        print(f"    ✓ ({x}, {y})")

    save_coords(c)

    # 网页模式下询问是否额外校准驱动的可选坐标
    if LLM_MODE == "web":
        for dk, dd in driver_keys.items():
            if dk not in c:
                add = input(
                    f"\n  是否额外校准「{dd}」坐标（作为兜底）？(y/n) >> "
                ).strip().lower()
                if add == 'y':
                    _calibrate_single_key(dk, c, driver_hints)

    print("\n  校准完成！")


def _calibrate_single_key(key, existing_coords, hints=None):
    """校准单个坐标点并保存"""
    all_hints = dict(_HINTS)
    if hints:
        all_hints.update(hints)
    print(f"\n--- {key} ---")
    print(f"    {all_hints.get(key, '将鼠标移到目标位置')}")
    input("    按 Enter 后 5 秒倒计时...")
    countdown(5)
    x, y = pyautogui.position()
    existing_coords[key] = [x, y]
    print(f"    ✓ ({x}, {y})")
    save_coords(existing_coords)


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

# ============================================================
# 向后兼容：重导出知乎专用操作函数
# ============================================================
from applications.zhihu_story.action import wait_editor_ready, get_bounds
