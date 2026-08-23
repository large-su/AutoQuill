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
import re

from core.story_text import parse_score_json, strip_json_fences, extract_json_block
from llm_client import call_llm_non_streaming, resolve_kb_llm_config

log = logging.getLogger(__name__)


def screen_question_pool(candidates, keep_best_only=False):
    """大模型筛选「问题+回答」候选池（硬性规则过滤后调用）。

    candidates: [{index, title, answer}]
    用 QUESTION_SCREEN_PROMPT 一次判定：排除不适合写故事/小说的，
    并按故事潜力排序。返回过滤后的候选列表（保序：合适的在前，
    best 置顶）；LLM 不可用/失败时原样返回（不阻断流程）。
    """
    from applications.zhihu_story.prompts import QUESTION_SCREEN_PROMPT
    from config import LLM_MODE

    if not candidates:
        return []
    if LLM_MODE != "api":
        # Web 模式不额外占窗口：筛选交给入口侧或跳过（见 WorkflowBase）
        log.info("问题池筛选：Web 模式跳过（生成阶段再判断），保留 %d 个", len(candidates))
        return candidates

    api_key, base_url, model, extra_body = resolve_kb_llm_config()
    if not api_key:
        log.warning("问题池筛选跳过（无 API Key）")
        return candidates

    preview = []
    for c in candidates:
        title = (c.get("title") or "")[:80]
        answer = (c.get("answer") or "")[:400]
        preview.append(f"[{c.get('index')}] 问题：{title}\n回答摘要：{answer}")
    prompt = QUESTION_SCREEN_PROMPT + "\n\n--- 候选列表 ---\n" + "\n---\n".join(preview)

    try:
        reply, _elapsed, error = call_llm_non_streaming(
            prompt, max_tokens=max(2000, len(candidates) * 120 + 200),
            temperature=0.1, timeout=120,
            api_key=api_key, base_url=base_url, model=model,
            extra_body=extra_body)
        if error:
            log.warning("问题池筛选请求失败：%s（沿用原候选）", error[:120])
            return candidates
        data = extract_json_block(reply)
        if not data:
            log.warning("问题池筛选返回无法解析（沿用原候选）")
            return candidates
        items = {int(it.get("index")): it for it in data.get("items", [])}
    except Exception as exc:
        log.warning("问题池筛选异常：%s（沿用原候选）", exc)
        return candidates

    kept = []
    dropped = 0
    for c in candidates:
        it = items.get(int(c.get("index") or -1)) or {}
        if it.get("keep"):
            it = dict(it)
            it["candidate"] = c
            kept.append(it)
        else:
            dropped += 1
    kept.sort(key=lambda it: int(it.get("score") or 0), reverse=True)
    best_index = data.get("best_index")
    if best_index is not None and kept:
        keep_idx = int(best_index)
        pos = next((i for i, it in enumerate(kept) if int(it["candidate"]["index"]) == keep_idx), None)
        if pos and pos > 0:
            kept.insert(0, kept.pop(pos))

    result = [it["candidate"] for it in kept]
    log.info("问题池筛选：%d → 保留 %d（排除 %d）%s",
             len(candidates), len(result), dropped,
             (f"，最佳 #{best_index}") if best_index is not None and best_index != -1 else "")
    if keep_best_only and result:
        return [result[0]]
    return result


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

    from config import (KB_LLM_API_KEY as _kb_key,
                         LLM_API_KEY as _root_key,
                         LLM_API_BASE_URL as _root_url,
                         LLM_API_MODEL as _root_model,
                         LLM_API_EXTRA_BODY as _root_extra_body)

    def _auth_error(error):
        return bool(error and re.search(
            r"401|403|authentication|api ?key|invalid", error, re.I))

    try:
        log.info("发送评分请求...")
        reply, elapsed, error = call_llm_non_streaming(
            prompt, max_tokens=max(4000, len(stories_data) * 350 + 500),
            temperature=0.3, timeout=120,
            api_key=api_key, base_url=base_url, model=_MODEL,
            extra_body=extra_body)
        # 知识库评分 key 失效（无效/过期）时，自动回退故事生成 key 重试一次
        if _auth_error(error) and _kb_key and _kb_key != _root_key:
            log.warning("评分请求被拒（key 无效/过期）：%s", (error or "")[:120])
            log.warning("知识库服务商 key 无效，改用故事生成服务商 key 重试一次")
            reply, elapsed, error = call_llm_non_streaming(
                prompt, max_tokens=max(4000, len(stories_data) * 350 + 500),
                temperature=0.3, timeout=120,
                api_key=_root_key, base_url=_root_url, model=_root_model,
                extra_body=dict(_root_extra_body or {}))
        if error:
            log.error("评分 API 失败：%s", error)
            log.error("提示：评分走服务商 API，请在 设置→生成通道 检查 API Key，"
                      "或检查 config/llm_providers.json 中对应服务商的 apiKey")
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
                    '自然度(去AI味)': s.get('natural', 0),
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
