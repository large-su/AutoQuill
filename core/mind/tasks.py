"""MindTask — 提交给"脑"的认知任务类型。

架构位置：Layer 2 (Core Capabilities) — Mind
对应文档：agent_framework_architecture_manifesto.md §5.5
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MindTask:
    """统一的认知任务基类。

    字段：
        task_type: 任务类型（generate / classify / score / extract / plan / judge / rewrite）
        input: 任务输入（可以是纯文本、结构化数据等）
        constraints: 可选约束条件
        expected_schema: 可选的期望输出 schema
        mode_preference: "api" / "web" / "auto"（默认 api）
        postprocess_profile: 可选的后处理配置名
        metadata: 可扩展的元数据
    """
    task_type: str = ""
    input: Any = None
    constraints: Optional[dict] = None
    expected_schema: Optional[dict] = None
    mode_preference: str = "api"
    postprocess_profile: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class GenerateTask(MindTask):
    """生成类任务。"""
    task_type: str = "generate"
    max_tokens: int = 4096
    temperature: float = 0.9


@dataclass
class ClassifyTask(MindTask):
    """分类类任务。"""
    task_type: str = "classify"
    categories: Optional[list] = None


@dataclass
class ScoreTask(MindTask):
    """评分类任务。"""
    task_type: str = "score"
    score_dimensions: Optional[list] = None


@dataclass
class ExtractTask(MindTask):
    """提取类任务。"""
    task_type: str = "extract"


@dataclass
class PlanTask(MindTask):
    """规划类任务。"""
    task_type: str = "plan"
