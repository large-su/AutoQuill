"""ActionCommand — 系统"要做什么动作"。

架构位置：Layer 2 (Core Capabilities) — Action
对应文档：agent_framework_architecture_manifesto.md §5.3

原则：动作应先被表达，再被执行。这样才方便日志、回放、调试和未来策略学习。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActionCommand:
    """统一的动作命令。

    字段：
        type: 动作类型（click / hotkey / paste / scroll / focus_window / navigate / upload）
        target: 动作目标（如 UI 文本、坐标、URL）
        params: 额外参数（如按键组合、滚动量）
        preconditions: 前置条件列表
        timeout: 超时时间（秒）
        retries: 失败重试次数
    """
    type: str  # click / hotkey / paste / scroll / focus_window / navigate / upload
    target: Optional[str] = None
    params: dict = field(default_factory=dict)
    preconditions: list = field(default_factory=list)
    timeout: float = 10.0
    retries: int = 1
