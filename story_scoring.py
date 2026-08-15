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
import time

import requests

from core.story_text import parse_score_json

log = logging.getLogger(__name__)


def _resolve_kb_config():
    """
    解析知识库任务用的 API 配置（KB 优先，故事生成回退）。

    返回: (api_key: str, base_url: str, model: str, extra_body: dict)
    """
    from config import LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL
    from config import KB_LLM_API_KEY as _kb_key
    from config import KB_LLM_BASE_URL as _kb_url
    from config import KB_LLM_MODEL as _model
    from config import KB_LLM_EXTRA_BODY as _extra_body
    return (
        (_kb_key or LLM_API_KEY),
        (_kb_url or LLM_API_BASE_URL),
        _model,
        dict(_extra_body or {}),
    )


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
    api_key, base_url, _MODEL, extra_body = _resolve_kb_config()

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

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max(4000, len(stories_data) * 350 + 500),
        "temperature": 0.3,  # 低温度保证评分稳定
        "stream": False
    }
    if extra_body:
        payload.update(extra_body)

    try:
        log.info("发送评分请求...")
        start = time.time()
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.encoding = "utf-8"  # 强制 UTF-8
        elapsed = time.time() - start

        if resp.status_code != 200:
            log.error(f"评分 API 失败：{resp.status_code}")
            return stories_data

        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()

        # ★ Token 用量上报
        try:
            from llm_token_tracker import tracker
            tracker.report(_MODEL, data.get("usage", {}))
        except Exception:
            pass

        log.info(f"评分完成（{elapsed:.1f}s）")

        # 解析 JSON
        # 清理可能的 markdown 代码块
        clean_reply = reply.strip()
        if clean_reply.startswith("```"):
            clean_reply = clean_reply.split("\n", 1)[1] if "\n" in clean_reply else clean_reply[3:]
        if clean_reply.endswith("```"):
            clean_reply = clean_reply[:-3]
        clean_reply = clean_reply.strip()

        scores = parse_score_json(clean_reply, len(stories_data))

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
