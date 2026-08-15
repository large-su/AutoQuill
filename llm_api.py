# ============================================================
# llm_api.py — LLM 接口兼容门面（P4 拆分后保留）
#
# 原 1425 行上帝模块已按职责拆分为 4 个模块（2026-08）：
#   - llm_client.py       HTTP 流式调用 / 连通性测试
#   - story_prompt.py     prompt 构建
#   - story_generation.py 生成编排（短文/长文/并行）
#   - story_scoring.py    质量评分
#
# 本文件仅 re-export，全项目现有 `from llm_api import ...`
# 调用点（workflows/main/tools/tests）零改动。
# ============================================================

from llm_client import _call_llm_streaming, test_api_connection
from story_prompt import _resolve_meta_content, build_story_prompt
from story_scoring import _resolve_kb_config, score_stories
from story_generation import (
    generate_story,
    generate_story_parallel,
    _load_author_profile_or_none,
)
