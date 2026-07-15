"""Mind（脑）—— 认知能力抽象接口。

架构位置：Layer 2 (Core Capabilities) — Mind
对应文档：agent_framework_architecture_manifesto.md §6.1

要求：
  - 可由 API 实现
  - 可由 Web LLM 实现
  - 对调用方屏蔽底层差异
"""

from abc import ABC, abstractmethod
from core.mind.tasks import MindTask
from core.mind.results import MindResult


class Mind(ABC):
    """认知中枢抽象接口。

    子类实现 run() 以提供具体的 LLM 调用通道（API / Web / ...），
    调用方只依赖此接口，不感知底层是 API 还是浏览器。
    """

    @abstractmethod
    def run(self, task: MindTask) -> MindResult:
        """执行认知任务并返回结构化结果。

        参数：
            task: 统一的 MindTask（含 task_type、input、约束等）

        返回：
            MindResult（含 success、content、structured_output 等）
        """
        ...
