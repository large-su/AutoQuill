# ============================================================
# llm_api.py — 【已废弃】历史门面（原 1425 行上帝模块，2026-08 拆分）
#
# 运行时代码已全部直连四个职责模块：
#   - llm_client.py       HTTP 流式调用 / 连通性测试
#   - story_prompt.py     prompt 构建
#   - story_generation.py 生成编排（短文/长文/并行）
#   - story_scoring.py    质量评分 / 问题池筛选
#
# 本文件仅为 archive/ 历史脚本与外部旧引用保留 re-export；
# import 时发 DeprecationWarning。新代码禁止再从这里导入。
# 计划在 v5.x 移除。
# ============================================================
import warnings

warnings.warn(
    "llm_api 门面已拆分废弃：请直接 import llm_client / story_prompt / "
    "story_generation / story_scoring（计划 v5.x 移除本文件）",
    DeprecationWarning,
    stacklevel=2,
)

from llm_client import _call_llm_streaming, test_api_connection
from story_prompt import _resolve_meta_content, build_story_prompt
from story_scoring import score_stories, screen_question_pool
from story_generation import (
    generate_story,
    generate_story_parallel,
    _load_author_profile_or_none,
)
