"""Perception（眼）—— 环境感知能力抽象接口。

架构位置：Layer 2 (Core Capabilities) — Perception
对应文档：agent_framework_architecture_manifesto.md §6.2

子能力示例：
  - observe_text(region)
  - find_text(text, region)
  - find_icon(icon, region)
  - inspect_page_state(hints)
  - capture_screen(region)

原则：Perception 返回观察结果与置信度，而不是直接帮流程做业务决策。
"""

from abc import ABC, abstractmethod
from core.perception.observation import Observation


class Perception(ABC):
    """环境感知抽象接口。

    子类实现 observe() 以提供具体的感知通道（OCR / 图标匹配 / 截图 / …），
    调用方只消费 Observation，不感知底层 OCR 引擎或匹配算法。
    """

    @abstractmethod
    def observe(self, query: str, region: tuple = None) -> Observation:
        """观察环境，返回结构化的观察结果。

        参数：
            query: 观察意图描述（如 "find_text"、"ocr_region"、"check_loaded"）
            region: 可选的目标区域 (x, y, w, h)

        返回：
            Observation（含 raw_text、blocks、confidence 等）
        """
        ...
