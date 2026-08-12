# ============================================================
# applications/zhihu_story/config.py — 知乎故事创作业务参数（re-export）
#
# 架构位置：Layer 5 (Applications)
#
# 历史：常量定义原在此文件；因被 workflows/core/config/llm_api 等
# 底层与中间层反向引用，造成分层倒置，2026-08 迁移至 config/story.py。
# 本文件保留为薄 re-export 兼容层，应用层内部 200+ 处引用零改动。
#
# 原则：新增/修改参数请直接编辑 config/story.py；本文件不再定义常量。
# ============================================================

from config.story import *  # noqa: F401,F403 — 有意 re-export 全量常量
