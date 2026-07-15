# ============================================================
# rich_progress.py — Rich 美化进度面板
#
# 替代 desktop_utils.print_progress / reset_progress，
# 用 rich 库的 Live + Table 渲染彩色进度面板。
#
# 用法：
#   panel = RichProgressPanel()
#   panel.setup(total_tasks)          # 初始化
#   while not all_done:
#       panel.render(progress_dict)   # 每轮刷新
#   panel.teardown()                  # 收尾
#
# 兼容：progress_dict 格式与旧 print_progress 完全一致：
#   {task_id: {status, chars, elapsed, title}, ...}
# ============================================================

import logging
from datetime import datetime

log = logging.getLogger(__name__)

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


class RichProgressPanel:
    """Rich 美化进度面板"""

    def __init__(self):
        self._live = None
        self._total = 0
        self._start_time = None

    # ============================================================
    # 生命周期
    # ============================================================

    def setup(self, total):
        """
        初始化面板。必须在主线程调用。

        参数：
            total: 总任务数
        """
        if not _RICH_AVAILABLE:
            log.warning("rich 库未安装，回退到简易进度显示")
            return False

        self._total = total
        self._start_time = datetime.now()
        self._console = Console()
        self._live = Live(
            self._build_table({}),
            console=self._console,
            refresh_per_second=4,
            transient=False,  # 保留最终面板在终端
            vertical_overflow="visible",
        )
        self._live.start()
        return True

    def render(self, progress, total=None):
        """
        刷新进度面板。线程安全——只更新 Live 的内容。

        参数：
            progress: {task_id: {status, chars, elapsed, title}}
            total:     总任务数（兼容旧 print_progress 签名，可选）
        """
        if not self._live:
            return
        # total 参数可在此处更新（非必须，setup 时已设置）
        if total is not None:
            self._total = total
        self._live.update(self._build_table(progress))

    def teardown(self):
        """关闭面板"""
        if self._live:
            self._live.stop()
            self._live = None

    # ============================================================
    # 内部：构建 Rich Table
    # ============================================================

    def _build_table(self, progress):
        """根据 progress dict 构建 Rich Table"""

        # --- 统计 ---
        done = sum(1 for p in progress.values() if '✓' in p.get('status', '') or '完成' in p.get('status', ''))
        failed = sum(1 for p in progress.values() if '❌' in p.get('status', '') or '失败' in p.get('status', '') or '超时' in p.get('status', ''))
        active = len(progress) - done - failed
        elapsed = (datetime.now() - self._start_time).total_seconds() if self._start_time else 0

        # --- 顶部面板 ---
        header = Text()
        header.append("📝 ", style="bold")
        header.append(f"并行生成进度  ", style="bold white")
        header.append(f"{done + failed}/{self._total} 完成  ", style="cyan")
        header.append(f"⏱ {elapsed:.0f}s", style="dim")

        if active > 0:
            header.append(f"  |  {active} 个进行中", style="yellow")
        if failed > 0:
            header.append(f"  |  {failed} 个失败", style="red")

        panel = Panel(
            header,
            box=box.ROUNDED,
            border_style="blue",
            padding=(0, 2),
        )

        # --- 任务表格 ---
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold dim",
            expand=True,
        )
        table.add_column("#", width=4, justify="right")
        table.add_column("状态", width=6)
        table.add_column("标题", width=18)
        table.add_column("进度", width=12, justify="right")
        table.add_column("用时", width=6, justify="right")

        for tid in sorted(progress.keys()):
            p = progress[tid]
            status_raw = p.get('status', '等待')
            chars = p.get('chars', 0)
            elapsed_val = p.get('elapsed', 0)
            title = p.get('title', '')[:16]

            # 状态图标和文字颜色
            if '✓' in status_raw or '完成' in status_raw:
                icon = '[bold green]✓[/]'
                status_text = '[green]完成[/]'
                progress_bar = self._bar(1.0, 'green')
                chars_str = f"[dim]{chars}字[/]"
            elif '❌' in status_raw or '失败' in status_raw or '超时' in status_raw:
                icon = '[bold red]✗[/]'
                status_text = '[red]失败[/]'
                progress_bar = self._bar(0.0, 'red')
                chars_str = f"[dim]{chars}字[/]"
            elif '大纲' in status_raw:
                icon = '[yellow]◐[/]'
                status_text = '[yellow]大纲[/]'
                progress_bar = self._bar(0.3, 'yellow')
                chars_str = f"[dim]{chars}字[/]"
            elif '正文' in status_raw:
                icon = '[cyan]▶[/]'
                status_text = '[cyan]正文[/]'
                # 正文目标 ~12000 字
                ratio = min(chars / 12000, 0.95) if chars > 0 else 0.1
                progress_bar = self._bar(ratio, 'cyan')
                chars_str = f"[cyan]{chars}字[/]"
            elif '等待' in status_raw:
                icon = '[dim]○[/]'
                status_text = '[dim]等待[/]'
                progress_bar = self._bar(0.0, 'dim')
                chars_str = '[dim]--[/]'
            elif '生成中' in status_raw:
                # 短文模式（非长文无需区分大纲/正文阶段）
                icon = '[yellow]●[/]'
                status_text = '[yellow]生成[/]'
                ratio = min(chars / 8000, 0.9) if chars > 0 else 0.08
                progress_bar = self._bar(ratio, 'yellow')
                chars_str = f"[yellow]{chars}字[/]" if chars else '[dim]--[/]'
            else:
                icon = '[yellow]●[/]'
                status_text = f'[yellow]{status_raw[:4]}[/]'
                progress_bar = self._bar(0.15, 'yellow')
                chars_str = f"[dim]{chars}字[/]" if chars else '[dim]--[/]'

            elapsed_str = f"[dim]{elapsed_val:.0f}s[/]" if elapsed_val > 0 else "[dim]--[/]"

            table.add_row(
                str(tid),
                f"{icon}",
                f"{title}",
                f"{progress_bar} {chars_str}",
                elapsed_str,
            )

        # 组合：面板在上，表格在下
        from rich.columns import Columns
        from rich.layout import Layout
        # 直接用 Group 垂直排列，简单可靠
        from rich.console import Group
        full = Group(panel, table)

        return full

    def _bar(self, ratio, color):
        """生成一个彩色进度条"""
        width = 8
        filled = max(0, min(width, int(ratio * width)))
        bar = f"[{color}]" + "█" * filled + "░" * (width - filled) + "[/]"
        return bar


# ============================================================
# 快捷函数：兼容旧 print_progress / reset_progress 接口
# ============================================================

def create_rich_progress(total):
    """创建并启动 Rich 进度面板，返回 (render_fn, teardown_fn)"""
    panel = RichProgressPanel()
    ok = panel.setup(total)
    if not ok:
        # 回退：返回空操作 + None（调用方自行判断是否用旧 print_progress）
        from desktop_utils import print_progress as _fallback_print
        from desktop_utils import reset_progress as _fallback_reset
        return _fallback_print, _fallback_reset, None
    return panel.render, panel.teardown, panel
