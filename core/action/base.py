"""Action（手）—— 动作执行能力抽象接口。

架构位置：Layer 2 (Core Capabilities) — Action
对应文档：agent_framework_architecture_manifesto.md §6.3

子能力示例：
  - click(target)
  - hotkey(keys)
  - paste(text)
  - scroll(amount)
  - focus(window)
  - navigate(url)
  - upload(file)

原则：Action 只执行，不推理。
"""

from abc import ABC, abstractmethod
from core.action.commands import ActionCommand
from core.action.results import ActionResult


class Action(ABC):
    """动作执行抽象接口。

    子类实现 execute() 以提供具体的动作通道（桌面自动化 / 浏览器控制 / …），
    调用方只需构造 ActionCommand，不感知底层 pyautogui 或 Selenium。
    """

    @abstractmethod
    def execute(self, command: ActionCommand) -> ActionResult:
        """执行动作命令并返回结果。

        参数：
            command: 统一的 ActionCommand（含 type、target、params 等）

        返回：
            ActionResult（含 success、error、artifacts 等）
        """
        ...
