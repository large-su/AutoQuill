"""ActionResult — 动作执行结果。

架构位置：Layer 2 (Core Capabilities) — Action
对应文档：agent_framework_architecture_manifesto.md §5.4
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActionResult:
    """统一的动作执行结果。

    字段：
        success: 动作是否成功
        action_type: 执行的动作类型
        start_time: 开始时间
        end_time: 结束时间
        actual_target: 实际命中的目标
        error: 错误描述（None 表示成功）
        side_effects: 副作用列表（如弹窗、页面跳转）
        artifacts: 关联的工件引用（如截图路径）
    """
    success: bool
    action_type: str = ""
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    actual_target: Optional[str] = None
    error: Optional[str] = None
    side_effects: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
