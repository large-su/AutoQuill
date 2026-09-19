# ============================================================
# automation/ — 自动化模块（无人化账号运营的时间轴调度）
#
# 职责边界（刻意收窄，避免长歪）：
#   - 本包只做「什么时候做什么、做多少、做完记账」——纯编排与状态，
#     不认识知乎，也不直接操作 DOM；
#   - 具体动作仍归 applications/zhihu_story/（DOM 能力）与 workflows/（全链路）；
#   - 执行统一交给 webui.run_manager.TaskRunner，浏览器互斥沿用
#     webui.browser_tasks.browser_busy()（手动操作优先，自动化排队）。
#
# 模块划分：
#   model.py      任务类型契约 + 计划默认值 + 校验/归一
#   store.py      计划 / 当日排班 / 台账 的原子读写
#   planner.py    把计划展开成「今天的时间轴」（配额/时段/随机间隔/去碰撞/补做）
#   scheduler.py  tick 循环：到点入队、串行执行、幂等、熔断、暂停/停止
#   executor.py   把一个作业交给现有能力执行，并把结果归一成台账字段
#
# 设计约定见 docs/AUTOMATION-PLAN.md。
# ============================================================

from automation.model import (  # noqa: F401
    DEFAULT_PLAN, TASK_TYPES, normalize_plan,
)

__all__ = ["DEFAULT_PLAN", "TASK_TYPES", "normalize_plan"]
