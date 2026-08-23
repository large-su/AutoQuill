# ============================================================
# story_generation.py — 故事生成编排（由 llm_api.py 拆分，2026-08）
#
# 职责：短文/并行的故事生成主流程：模式分发 → prompt 构建
#       （story_prompt）→ 流式调用（llm_client）→ 清洗后处理。
#
# 架构位置：Layer 0 (Tools) — 被 workflows / main / tools 调用。
#
# 运行时切换模型原理：函数内 `from config import ...` 动态读取，
# Web 控制台 set_runtime_model 重赋值后立即生效，勿改为顶层导入。
# ============================================================

import logging
import sys
import time

from core.story_text import clean_story_output, fix_story_format
from story_prompt import build_story_prompt

log = logging.getLogger(__name__)


def generate_story(question_title, reference_answer=None, recipe=None,
                   meta_knowledge=None, author=None, feedback=None):
    """
    通过 API 生成故事，支持流式输出到终端。

    根据 config.story.STORY_MATERIAL_MODE 自动选择 prompt 构建方式：
      - "recipe"               配方驱动
      - "reference"            参考文章模式
      - "recipe_and_reference" 配方 + 参考文章结合

    参数：
        question_title:    知乎问题标题
        reference_answer:  高赞回答文本
        recipe:            知识库配方 dict
        meta_knowledge:    跨任务积累的元知识文本（可选，外部注入用）
        author:            作者名（可选）。从 data/authors/{name}.json 加载
                           技能签名并注入 prompt（仅短文模式）
        feedback:          重试修正反馈（可选，str 或 str 列表）。上一版
                           故事的失败原因，非空时注入 prompt 供模型修正重写

    返回：
        生成的故事文本（已清洗），失败返回 None
    """
    from config import LLM_API_KEY, LLM_API_MAX_TOKENS, LLM_API_MODEL

    if not LLM_API_KEY:
        log.error("API Key 未配置！请在 config.py 中设置 LLM_API_KEY")
        return None

    author_profile = _load_author_profile_or_none(author)

    # ===== 统一 prompt 构建 =====
    user_message, mode_str = build_story_prompt(
        question_title, reference_answer, recipe,
        meta_knowledge=meta_knowledge, author_profile=author_profile,
        feedback=feedback,
    )

    log.info(f"API 流式调用开始")
    log.info(f"  模型：{LLM_API_MODEL} | 模式：{mode_str}")
    log.info(f"  问题：{question_title[:40]}...")
    print()
    print("  ── 生成内容开始 ──")

    # 心跳：长生成可能持续数分钟，期间无日志会让外层看门狗
    # （日志 mtime 静默超时）误判卡死杀进程——定期写进度。
    # ★ 展示「累计总量」而非每 400 字窗口：窗口计数会反复显示
    # ~400，让人误以为生成卡住
    _heartbeat = {"n": 0, "total": 0}

    def _on_chunk(c):
        sys.stdout.write(c)
        sys.stdout.flush()
        _heartbeat["n"] += len(c)
        _heartbeat["total"] += len(c)
        if _heartbeat["n"] >= 400:
            log.info(f"    生成中… 累计输出 {_heartbeat['total']} 字符")
            _heartbeat["n"] = 0

    from llm_client import _call_llm_streaming
    full_content, elapsed, error = _call_llm_streaming(
        user_message,
        max_tokens=LLM_API_MAX_TOKENS,
        on_chunk=_on_chunk,
        label=f"短文 [{mode_str}]",
    )

    print()
    print("  ── 生成内容结束 ──")
    print()

    if error and not full_content:
        log.error(f"API 调用失败：{error}")
        return None

    if error and full_content:
        log.warning(f"API 部分失败（{error}），使用已接收内容（{len(full_content)} 字符）")

    log.info(f"  ✓ 流式生成完成！耗时 {elapsed:.1f}s | {len(full_content)} 字符")

    # 清洗 + 格式后处理
    cleaned = clean_story_output(full_content)
    cleaned = fix_story_format(cleaned)
    if len(cleaned) != len(full_content):
        log.info(f"  清洗+后处理后：{len(cleaned)} 字符（原始 {len(full_content)} 字符）")

    return cleaned


# ============================================================
# 并行生成（批量模式用）
# ============================================================

def _load_author_profile_or_none(author):
    """按作者名加载技能签名；无签名文件或失败时返回 None。"""
    if not author:
        return None
    try:
        from applications.zhihu_story.author_profiler import (
            load_author_profile, load_general_profile)
        if author == "通用":
            # 通用文风走专用加载：本地提炼版优先，缺失回退内置规则
            profile = load_general_profile()
            if profile:
                log.info("  [作者风格] 已加载「通用」写作规则")
            else:
                log.warning("  [作者风格] 通用写作规则不可用（内置文件缺失？）")
            return profile
        profile = load_author_profile(author)
        if profile:
            log.info(f"  [作者风格] 已加载「{author}」技能签名")
        else:
            log.warning(f"  [作者风格] 未找到「{author}」技能签名"
                        f"（先运行 author_profiler）")
        return profile
    except Exception as e:
        log.warning(f"  [作者风格] 加载失败：{e}")
        return None


def generate_story_parallel(question_title, reference_answer, task_id, progress,
                            recipe=None, meta_knowledge=None, author=None,
                            feedback=None):
    """
    非流式生成故事，用于多线程并行调用。

    与 generate_story() 的区别：
    - 不使用流式输出（避免多线程 stdout 交叉）
    - 通过 progress dict 实时报告状态
    - 线程安全

    参数：
        question_title:    知乎问题标题
        reference_answer:  高赞回答文本（配方模式下可为 None）
        task_id:           任务编号（从1开始）
        progress:          共享进度字典
        recipe:            知识库配方（可选，提供则使用配方模式）
        meta_knowledge:    跨任务积累的元知识文本（可选）
        author:            作者名（可选），注入其技能签名
        feedback:          重试修正反馈（可选，str 或 str 列表）

    返回：
        生成的故事文本（已清洗），失败返回 None
    """
    from config import LLM_API_KEY, LLM_API_MAX_TOKENS

    short_title = question_title[:20] + "..." if len(question_title) > 20 else question_title

    # 初始化进度
    progress[task_id] = {
        'status': '等待中',
        'chars': 0,
        'elapsed': 0,
        'started_at': None,
        'title': short_title,
    }

    if not LLM_API_KEY:
        progress[task_id]['status'] = '❌ 无Key'
        return None

    author_profile = _load_author_profile_or_none(author)

    # ===== 统一 prompt 构建 =====
    user_message, _ = build_story_prompt(
        question_title, reference_answer, recipe,
        meta_knowledge=meta_knowledge, author_profile=author_profile,
        feedback=feedback,
    )

    local_start = time.time()
    accumulated = {"text": ""}
    first_token_logged = False
    progress[task_id].update({
        'status': '生成中',
        'started_at': local_start,
    })
    log.info(f"  任务 {task_id} 开始 API 生成")

    def _on_chunk(c):
        nonlocal first_token_logged
        if not first_token_logged:
            first_token_logged = True
            log.info(f"  任务 {task_id} 收到首个正文 token")
        accumulated["text"] += c
        elapsed = time.time() - local_start
        progress[task_id].update({
            'chars': len(accumulated["text"]),
            'elapsed': elapsed,
        })

    from llm_client import _call_llm_streaming
    full_content, elapsed, error = _call_llm_streaming(
        user_message,
        max_tokens=LLM_API_MAX_TOKENS,
        on_chunk=_on_chunk,
    )

    if error:
        progress[task_id].update({
            'status': f'❌ {error[:30]}',
            'elapsed': elapsed,
        })
        log.warning(f"  任务 {task_id} 生成失败：{error}")
        return None

    cleaned = clean_story_output(full_content)
    cleaned = fix_story_format(cleaned)

    progress[task_id].update({
        'status': f'✓ 完成',
        'chars': len(cleaned),
        'elapsed': elapsed,
    })

    return cleaned
