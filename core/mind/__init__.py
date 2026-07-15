# Mind（脑）—— 认知能力抽象接口
#
# 职责：生成、判断、分类、评分、规划、结构化提取
# 不关心当前业务是不是知乎。
from core.mind.base import Mind
from core.mind.tasks import MindTask, GenerateTask, ClassifyTask, ScoreTask, ExtractTask, PlanTask
from core.mind.results import MindResult
