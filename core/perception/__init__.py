# Perception（眼）—— 环境感知能力抽象接口
#
# 职责：OCR 识别、视觉区域观察、文本定位、图标定位、状态判断
# 它不负责决定下一步做什么——只负责"看见世界并结构化输出"。
from core.perception.base import Perception
from core.perception.observation import Observation
