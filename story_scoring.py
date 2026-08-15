# ============================================================
# story_scoring.py — 故事质量评分（由 llm_api.py 拆分，2026-08）
#
# 职责：用 LLM 对多篇故事做知乎读者视角评分（6 维），
#       附 KB 配置解析（评分用知识库模型）。
#
# 架构位置：Layer 0 (Tools) — 被 workflows/base 调用。
# ============================================================

import json
import logging

from core.story_text import parse_score_json, strip_json_fences
from llm_client import call_llm_non_streaming, resolve_kb_llm_config

log = logging.getLogger(__name__)


def score_stories(stories_data):
    """
    用 LLM 对多篇故事进行质量评分（知乎读者视角）。

    评分维度（6项，每项1-10分，满分60）：
    1. 开头冲击力（3秒生死线）
    2. 情节节奏（心跳图vs生产线）
    3. 情绪与人物（活人vs提线木偶）
    4. 语言人味（说人话vs播音腔）
    5. 结尾余味（留钩vs句号）
    6. 细节质感（毛坯房vs样板间）

    参数：
        stories_data: [{
            'index': 序号,
            'title': 问题标题,
            'story': 故事全文,
            'url': 问题链接,
            'md_path': .md 文件路径,
        }, ...]

    返回：
        按总分降序排列的列表，每个元素增加 'score' 和 'score_detail' 字段
    """
    api_key, base_url, _MODEL, extra_body = resolve_kb_llm_config()

    if not api_key or not stories_data:
        log.warning("评分跳过（无 API Key 或无故事）")
        return stories_data

    log.info(f"=" * 50)
    log.info(f"文章质量评分（共 {len(stories_data)} 篇）")
    log.info(f"=" * 50)

    # 构建评分 prompt
    from applications.zhihu_story.prompts import SCORE_PROMPT
    prompt = SCORE_PROMPT
    from config.story import SCORE_STORY_HEAD_CHARS, SCORE_STORY_TAIL_CHARS

    def _build_score_preview(story):
        """评分只看开头+结尾，降低 prompt 体积。"""
        story = story or ""
        head_chars = max(0, SCORE_STORY_HEAD_CHARS)
        tail_chars = max(0, SCORE_STORY_TAIL_CHARS)
        if len(story) <= head_chars + tail_chars:
            return story
        head = story[:head_chars]
        tail = story[-tail_chars:] if tail_chars else ""
        omitted = len(story) - head_chars - tail_chars
        return (
            f"{head}\n\n...(中间省略 {omitted} 字)...\n\n"
            f"【结尾片段】\n{tail}"
        )

    for i, item in enumerate(stories_data):
        story_preview = _build_score_preview(item['story'])

        prompt += f"\n--- 故事 {i+1}（问题：{item['title'][:50]}）---\n"
        prompt += story_preview
        prompt += "\n"

    try:
        log.info("发送评分请求...")
        reply, elapsed, error = call_llm_non_streaming(
            prompt, max_tokens=max(4000, len(stories_data) * 350 + 500),
            temperature=0.3, timeout=120,
            api_key=api_key, base_url=base_url, model=_MODEL,
            extra_body=extra_body)
        if error:
            log.error("评分 API 失败：%s", error)
            return stories_data

        log.info(f"评分完成（{elapsed:.1f}s）")

        scores = parse_score_json(strip_json_fences(reply), len(stories_data))

        # 将评分合并到 stories_data
        score_map = {s['index']: s for s in scores}

        for i, item in enumerate(stories_data):
            idx = i + 1
            if idx in score_map:
                s = score_map[idx]
                item['score'] = s.get('total', 0)
                item['score_detail'] = {
                    '开头冲击力': s.get('hook', 0),
                    '情节节奏': s.get('plot', s.get('pacing', 0)),
                    '情感共鸣': s.get('emotion', s.get('character', 0)),
                    '真实感': s.get('authenticity', s.get('language', 0)),
                    '结尾余味': s.get('ending', 0),
                    '格式体验': s.get('format', s.get('texture', 0)),
                }
                item['score_comment'] = s.get('comment', '')

                detail = ' | '.join(f"{k}={v}" for k, v in item['score_detail'].items())
                log.info(f"  故事 {idx}「{item['title'][:30]}...」")
                log.info(f"    总分={item['score']} | {detail}")
                log.info(f"    点评：{item['score_comment']}")
            else:
                item['score'] = 0
                item['score_detail'] = {}
                item['score_comment'] = '评分缺失'

        # 按总分降序排列
        stories_data.sort(key=lambda x: x.get('score', 0), reverse=True)

        log.info(f"\n  排名：")
        for rank, item in enumerate(stories_data):
            log.info(f"  第{rank+1}名: [{item['score']}分] {item['title'][:40]}...")

        return stories_data

    except json.JSONDecodeError as e:
        log.error(f"评分结果 JSON 解析失败：{e}")
        log.error(f"  原始回复（前 500 字）：{reply[:500]}")
        log.error(f"  原始回复（后 300 字）：{reply[-300:]}")
        return stories_data

    except Exception as e:
        log.error(f"评分出错：{e}")
        return stories_data
