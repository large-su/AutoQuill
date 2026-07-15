"""MindResult — "脑"的认知任务输出。

架构位置：Layer 2 (Core Capabilities) — Mind
对应文档：agent_framework_architecture_manifesto.md §5.6
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MindResult:
    """统一的认知任务输出。

    字段：
        success: 任务是否成功
        content: 生成的文本内容（如有）
        structured_output: 结构化的输出（如 JSON 解析结果）
        raw_output: 原始输出（未后处理的）
        provider: 模型服务商名称
        model: 使用的模型 ID
        mode: 调用模式（api / web）
        latency: 耗时（秒）
        warnings: 警告信息列表
        artifacts: 关联的工件引用
    """
    success: bool
    content: Optional[str] = None
    structured_output: Optional[Any] = None
    raw_output: Optional[str] = None
    provider: str = ""
    model: str = ""
    mode: str = "api"
    latency: float = 0.0
    warnings: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
