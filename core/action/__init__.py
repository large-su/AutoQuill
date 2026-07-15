# Action（手）—— 动作执行能力抽象接口
#
# 职责：鼠标操作、键盘操作、滚动、剪贴板、窗口切换、浏览器基本控制
# 它不负责业务判断——只负责"把动作执行出去，并返回动作结果"。
from core.action.base import Action
from core.action.commands import ActionCommand
from core.action.results import ActionResult
