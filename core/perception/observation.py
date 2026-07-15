"""Observation — 系统"看到了什么"。

架构位置：Layer 2 (Core Capabilities) — Perception
对应文档：agent_framework_architecture_manifesto.md §5.2

原则：流程层消费的应是 Observation，而不是直接消费底层 OCR 原始碎片。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Observation:
    """统一的环境观察结果。

    字段：
        source: 数据来源（ocr / icon_match / page_snapshot / window_scan）
        region: 观察区域 (x, y, w, h)，None 表示全屏
        raw_text: 识别到的原始文本
        blocks: 文本块列表（每块含坐标和文字）
        elements: 检测到的 UI 元素列表
        confidence: 置信度（0.0 ~ 1.0）
        timestamp: 观察时间戳
        stable: 是否已达到稳定状态（如页面停止滚动）
        metadata: 可扩展的元数据
    """
    source: str = ""
    region: Optional[tuple] = None
    raw_text: Optional[str] = None
    blocks: list = field(default_factory=list)
    elements: list = field(default_factory=list)
    confidence: float = 0.0
    timestamp: Optional[str] = None
    stable: bool = False
    metadata: dict = field(default_factory=dict)
