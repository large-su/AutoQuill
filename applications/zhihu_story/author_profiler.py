# ============================================================
# applications/zhihu_story/author_profiler.py — 作者写作技能提炼
#
# 从已采集的作者故事文本中提炼"写作技能签名"，存为
# data/authors/{作者名}.json，供生成链路注入（--author 模式）。
#
# 两步走：
#   1. 确定性文本统计（纯文本计算，无需 LLM）
#   2. LLM 剖析（统计信号 + 故事片段 → 结构化技能签名）
#
# 对外接口：
#   profile_author(author)      → 完整流程，产出并保存技能签名
#   load_author_profile(author) → 读取已保存签名（生成链路用）
#   load_author_stories(...)    → 从采集库读取某作者的故事
#   compute_text_stats(...)     → 确定性文本统计（可单测）
#
# 架构位置：Layer 5 (Applications) — 作者维度技能库
# ============================================================

import argparse
import datetime
import json
import logging
import math
import os
import re
import sys
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path, sanitize_filename
from core.story_text import extract_json_block, strip_json_fences
from applications.zhihu_story.collector import iter_collected_stories

AUTHORS_DIR = _data_path("data", "authors")
STORY_LIB = _data_path("data", "collected_stories.jsonl")

# 喂给 LLM 的每篇故事截取量（开头 + 中段 + 结尾），控制 prompt 体积
_SAMPLE_HEAD = 1200
_SAMPLE_MIDDLE = 600
_SAMPLE_TAIL = 1200

# 剖析时喂给 LLM 的代表作上限：按经验权重（点赞×新鲜度）取前 N 篇。
# 全量塞入会让 prompt 膨胀到十几万字符，模型易给出残缺/无效 JSON、且
# 质量不稳；聚焦高赞与近期代表作，既提高解析成功率也提高风格可信度。
MAX_PROFILE_STORIES = 12


# ============================================================
# 1. 读取采集库
# ============================================================

# 发表时间格式：'2026-02-20 11:03·广东' 或 '2025-10-28 02:48・广东'
_PUBLISH_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# 新鲜度权重：近 FRESH_DAYS 天内发表 = 1.0，之后线性衰减到 _RECENCY_FLOOR
FRESH_DAYS = 90
_RECENCY_DECAY_DAYS = 730
_RECENCY_FLOOR = 0.3


def parse_publish_date(footer):
    """从 footer 解析发表日期，无法解析返回 None。"""
    pt = (footer or {}).get("publish_time") or ""
    m = _PUBLISH_TIME_RE.search(pt)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def story_weight(footer, today=None):
    """经验权重 = 点赞对数 × 发表新鲜度（高质量经验优先）。

    - 点赞对数压缩：1000 赞 ≈ 7，10000 赞 ≈ 9.2，爆款不碾压
    - 新鲜度：近 FRESH_DAYS 天内发表权重满值，之后线性衰减
    - 无发表时间按中等新鲜度（0.6）计，无点赞按 0
    """
    likes = (footer or {}).get("likes")
    if not isinstance(likes, (int, float)) or likes < 0:
        likes = 0
    w_likes = math.log1p(likes)
    if w_likes <= 0:
        return 0.0
    today = today or datetime.date.today()
    pub = parse_publish_date(footer)
    if pub is None:
        w_recency = 0.6
    else:
        days = max((today - pub).days, 0)
        if days <= FRESH_DAYS:
            w_recency = 1.0
        else:
            w_recency = max(
                _RECENCY_FLOOR,
                1.0 - (days - FRESH_DAYS) / _RECENCY_DECAY_DAYS)
    return round(w_likes * w_recency, 3)


def load_author_stories(author, min_likes=0, source=None):
    """从采集库读取某作者的故事，按经验权重降序。

    参数：
        author:    作者名（精确匹配 record.author）
        min_likes: 只保留赞同数 >= 此值的故事（无互动数据的视为 0）
        source:    采集库 JSONL 路径（默认 data/collected_stories.jsonl）

    返回：
        [{title, answer, footer, chars, weight, publish_date}]，
        无正文或过短记录被剔除；weight 为点赞×新鲜度的经验权重。
    """
    source = source or STORY_LIB
    if not os.path.exists(source):
        log.warning("author_profiler: 采集库不存在：%s", source)
        return []

    stories = []
    for rec in iter_collected_stories(source):
        if rec.get("author") != author:
            continue
        answer = (rec.get("answer") or "").strip()
        if len(answer) < 100:
            continue
        footer = rec.get("footer") or {}
        likes = footer.get("likes")
        if not isinstance(likes, (int, float)) or likes < 0:
            likes = 0
        if likes < min_likes:
            continue
        stories.append({
            "title": (rec.get("title") or "").strip(),
            "answer": answer,
            "footer": footer,
            "chars": len(answer),
            "weight": story_weight(footer),
            "publish_date": parse_publish_date(footer),
        })

    stories.sort(key=lambda s: s["weight"], reverse=True)
    return stories


# ============================================================
# 2. 确定性文本统计
# ============================================================

_SENT_SPLIT = re.compile(r"[。！？…]+")

# 对话行判定：知乎故事常见「」/“”/直引号三种引号风格
_DIALOGUE_QUOTES = ("「", "”", "“", '"')


def _has_dialogue(line):
    return any(q in line for q in _DIALOGUE_QUOTES)


# ============================================================
# 2b. 升级维度（借鉴"蒸馏作者文风"方法论，适配知乎单行文本）
# ============================================================

# 句长分位数：衡量句长分布形态（P10 短句下限 / P50 基线 / P90 长句上限）
_LONG_SENTENCE_CHARS = 40


def _percentile(sorted_vals, p):
    """线性插值分位数（标准库实现，无 numpy 依赖）。"""
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    if f + 1 >= len(sorted_vals):
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[f + 1] - sorted_vals[f]) * (k - f)


# 感官词表：视觉/听觉/触觉/嗅觉/味觉——判断作者感官描写偏好
_SENSE_WORDS = {
    "视觉": ("看", "看到", "看见", "目光", "眼神", "眼睛", "脸", "脸色",
             "背影", "身影", "模样", "漂亮", "美", "俊", "苍白", "红"),
    "听觉": ("听", "听到", "听见", "声音", "嗓音", "喊", "叫", "吼",
             "脚步", "安静", "沉默", "哭", "笑"),
    "触觉": ("手", "抱", "握", "触", "摸", "碰", "贴", "烫", "凉",
             "冷", "热", "暖", "疼", "痛", "紧", "颤抖", "发抖"),
    "嗅觉": ("香", "气息", "味", "臭", "清新", "清冽"),
    "味觉": ("甜", "苦", "酸", "辣", "涩", "咸", "味道"),
}

# 比喻指纹：比喻/拟物连接词——衡量修辞密度
_METAPHOR_WORDS = ("像", "仿佛", "好像", "犹如", "宛如", "好似", "如同", "似的")

# 对话标签：说/道/问/喊……衡量对话衔接方式
_DIALOGUE_TAGS = ("说", "道", "问", "答", "喊", "叫", "吼", "骂", "哭",
                  "笑", "叹", "哄", "劝", "应", "念", "低语", "嘟囔",
                  "嘀咕", "反问", "质问", "哀求", "恳求", "回应",
                  "解释", "承认", "拒绝", "打断", "附和", "开口")
# 高频误报词先剔除再统计标签（"知道/味道/小说"里的"道/说"不算）
_TAG_NOISE = re.compile("|".join(
    ["知道", "味道", "道理", "道路", "报道", "霸道", "厚道", "门道",
     "小说", "传说", "听说", "比如说", "也就是说", "叫作", "叫做", "名叫"]))

# 数字编号小节：知乎故事常用"1 2 3…"硬切小节（行级结构在单行文本中的替代信号）
_NUMBERED_SECTION = re.compile(
    r"(?:[。！？…])\s*(\d{1,3})(?![0-9年月份日点时分秒个十百千万])")

# 结尾钩子：连载式"未完待续"——知乎故事高赞常见收尾策略
_CLIFFHANGER_MARKERS = ("未完待续", "未完", "待续", "下回分解")


def _count_sense_words(text):
    counts = {}
    for sense, words in _SENSE_WORDS.items():
        counts[sense] = sum(text.count(w) for w in words)
    return counts


def _count_metaphors(text):
    # 先替换多字词，避免"好像"里的"像"被重复计数
    total = 0
    for w in sorted(_METAPHOR_WORDS, key=len, reverse=True):
        total += text.count(w)
        text = text.replace(w, "＊")
    return total


def _count_dialogue_tags(text):
    cleaned = _TAG_NOISE.sub("", text)
    return {tag: cleaned.count(tag) for tag in _DIALOGUE_TAGS}


def _count_numbered_sections(text):
    return len(_NUMBERED_SECTION.findall(text))


def _has_cliffhanger(text):
    return any(m in text[-40:] for m in _CLIFFHANGER_MARKERS)


def compute_text_stats(stories):
    """计算故事集合的确定性文本特征（全文计算，不截断）。

    返回 dict：基础数值信号（v1 字段保持向后兼容）
    + 升级维度（句长分布/感官/比喻/对话标签/编号小节/跨篇一致性）。
    """
    if not stories:
        return {}

    total_chars = sum(s["chars"] for s in stories)

    sentences = []
    exclamations = 0
    questions = 0
    first_person = 0
    em_dashes = 0
    sense_total = {"视觉": 0, "听觉": 0, "触觉": 0, "嗅觉": 0, "味觉": 0}
    metaphor_total = 0
    tag_total = {}

    for s in stories:
        text = s["answer"]
        sentences += [seg for seg in _SENT_SPLIT.split(text) if seg.strip()]
        exclamations += text.count("！")
        questions += text.count("？")
        first_person += text.count("我")
        em_dashes += text.count("——")
        for sense, n in _count_sense_words(text).items():
            sense_total[sense] += n
        metaphor_total += _count_metaphors(text)
        for tag, n in _count_dialogue_tags(text).items():
            tag_total[tag] = tag_total.get(tag, 0) + n

    per_1000 = lambda n: round(n * 1000.0 / total_chars, 1)

    sentence_lens = sorted(len(s) for s in sentences)
    per_story = []
    for s in stories:
        ss = [seg for seg in _SENT_SPLIT.split(s["answer"]) if seg.strip()]
        pub = s.get("publish_date")
        per_story.append({
            "title": s.get("title", "未命名"),
            "likes": (s.get("footer") or {}).get("likes") or 0,
            "chars": s["chars"],
            "weight": s.get("weight") or 0,
            "publish_date": pub.strftime("%Y-%m-%d") if pub else None,
            "short_sentence_ratio": round(
                sum(1 for x in ss if len(x) <= 20) / len(ss), 2) if ss else 0,
            "dialogue_ratio": round(
                sum(1 for x in ss if _has_dialogue(x)) / len(ss), 2) if ss else 0,
            "numbered_sections": _count_numbered_sections(s["answer"]),
            "has_cliffhanger": _has_cliffhanger(s["answer"]),
        })
    with_sections = [p for p in per_story if p["numbered_sections"] > 0]
    with_hook = [p for p in per_story if p["has_cliffhanger"]]

    stats = {
        "stories_count": len(stories),
        "total_chars": total_chars,
        "avg_chars_per_story": round(total_chars / len(stories)),
        "avg_sentence_len": round(
            total_chars / len(sentences), 1) if sentences else 0,
        "short_sentence_ratio": round(
            sum(1 for s in sentences if len(s) <= 20) / len(sentences), 2
        ) if sentences else 0,
        # 对话密度按句子计（知乎正文提取后常为无换行的单行文本，
        # 行级统计会退化）；引号风格兼容 「」/“”/直引号
        "dialogue_ratio": round(
            sum(1 for s in sentences if _has_dialogue(s)) / len(sentences), 2
        ) if sentences else 0,
        "exclamation_per_1000": per_1000(exclamations),
        "question_per_1000": per_1000(questions),
        "first_person_per_1000": per_1000(first_person),
        "em_dash_count": em_dashes,
        "openings": [_first_n_sentences(s["answer"], 3) for s in stories],
        "endings": [_last_n_sentences(s["answer"], 2) for s in stories],
        # ---- 升级维度 ----
        "sentence_len_p10": round(_percentile(sentence_lens, 10)),
        "sentence_len_p50": round(_percentile(sentence_lens, 50)),
        "sentence_len_p90": round(_percentile(sentence_lens, 90)),
        "long_sentence_ratio": round(
            sum(1 for s in sentences if len(s) > _LONG_SENTENCE_CHARS)
            / len(sentences), 2) if sentences else 0,
        "sense_words_per_1000": {
            k: per_1000(v) for k, v in sense_total.items()},
        "metaphor_per_1000": per_1000(metaphor_total),
        "dialogue_tags_per_1000": {
            k: per_1000(v) for k, v in sorted(
                tag_total.items(), key=lambda kv: kv[1], reverse=True)
            if v > 0},
        "section_ratio": round(
            len(with_sections) / len(stories), 2) if stories else 0,
        "avg_sections_per_story": round(
            sum(p["numbered_sections"] for p in per_story) / len(per_story), 1)
        if per_story else 0,
        "cliffhanger_ratio": round(
            len(with_hook) / len(stories), 2) if stories else 0,
        "per_story": per_story,
    }
    return stats


def _first_n_sentences(text, n):
    parts = [p for p in _SENT_SPLIT.split(text) if p.strip()]
    return "。".join(parts[:n]) + ("。" if len(parts) > 0 else "")


def _last_n_sentences(text, n):
    parts = [p for p in _SENT_SPLIT.split(text) if p.strip()]
    if not parts:
        return ""
    tail = "。".join(parts[-n:])
    return tail + "。"


# ============================================================
# 3. LLM 剖析
# ============================================================

def _sample_story(answer, head=_SAMPLE_HEAD, middle=_SAMPLE_MIDDLE, tail=_SAMPLE_TAIL):
    """截取故事片段：开头 + 中段 + 结尾。短文原样返回。"""
    if len(answer) <= head + middle + tail:
        return answer
    return (answer[:head] + "\n\n……（中段略）……\n\n"
            + answer[len(answer) // 2 - middle // 2: len(answer) // 2 + middle // 2]
            + "\n\n……（后段略）……\n\n" + answer[-tail:])


def _format_stats_for_prompt(stats):
    keys = [
        "stories_count", "total_chars", "avg_chars_per_story",
        "avg_sentence_len", "short_sentence_ratio", "dialogue_ratio",
        "exclamation_per_1000", "question_per_1000",
        "first_person_per_1000", "em_dash_count",
        # 升级维度
        "sentence_len_p10", "sentence_len_p50", "sentence_len_p90",
        "long_sentence_ratio",
        "section_ratio", "avg_sections_per_story", "cliffhanger_ratio",
    ]
    lines = []
    for k in keys:
        v = stats.get(k)
        if v is not None:
            lines.append(f"- {k}: {v}")
    sense = stats.get("sense_words_per_1000") or {}
    if sense:
        lines.append("- sense_words_per_1000: " + str(sense))
    tags = stats.get("dialogue_tags_per_1000") or {}
    if tags:
        top_tags = " / ".join(f"{k}={v}" for k, v in list(tags.items())[:6])
        lines.append(f"- dialogue_tags_per_1000(前6): {top_tags}")
    meta = stats.get("metaphor_per_1000")
    if meta is not None:
        lines.append(f"- metaphor_per_1000: {meta}")
    return "\n".join(lines)


def _format_consistency_for_prompt(stats):
    """跨篇一致性：逐篇信号 + 波动范围，供 LLM 判断技法稳定性。"""
    per = stats.get("per_story") or []
    if len(per) < 2:
        return "（故事不足 2 篇，无法做跨篇一致性判断）"
    lines = ["### 跨篇一致性（逐篇信号：判断哪些技法稳定、哪些随题材变化；"
             "经验权重=点赞×发表新鲜度，权重高的篇目更可信）"]
    for p in per:
        hook = "未完待续钩子" if p["has_cliffhanger"] else "完整收尾"
        lines.append(
            f"- 《{p['title']}》（{p['likes']}赞，{p['chars']}字，"
            f"权重 {p.get('weight', 0)}）："
            f"短句比例 {p['short_sentence_ratio']}，对话比例 {p['dialogue_ratio']}，"
            f"编号小节 {p['numbered_sections']} 节，{hook}")
    shorts = [p["short_sentence_ratio"] for p in per]
    diags = [p["dialogue_ratio"] for p in per]
    lines.append(
        f"- 短句比例跨篇范围：{min(shorts):.2f} ~ {max(shorts):.2f}，"
        f"跨度 {max(shorts) - min(shorts):.2f}（跨度小=稳定风格）")
    lines.append(
        f"- 对话比例跨篇范围：{min(diags):.2f} ~ {max(diags):.2f}，"
        f"跨度 {max(diags) - min(diags):.2f}")
    sec = stats.get("section_ratio")
    if sec is not None:
        lines.append(f"- 使用数字编号小节的篇数占比：{sec:.0%}")
    return "\n".join(lines)


def _format_stories_for_prompt(stories):
    blocks = []
    for i, s in enumerate(stories, 1):
        f = s["footer"]
        likes = f.get("likes") or 0
        pub = s.get("publish_date")
        pub_str = pub.strftime("%Y-%m-%d") if pub else "未知"
        # 经验权重 = 点赞对数×新鲜度，权重高的代表作技法更可信；
        # 无 weight 字段的旧数据回退到点赞分级
        weight = s.get("weight")
        if weight is None:
            level = ("高" if likes >= 300 else "中" if likes >= 100 else "低")
            weight_str = "未知"
        else:
            level = ("高" if weight >= 3.0 else "中" if weight >= 1.0 else "低")
            weight_str = f"{weight}"
        blocks.append(
            f"=== 故事 {i}：{s['title']} "
            f"（{s['chars']}字，赞同={likes}，评论={f.get('comments')}，"
            f"发表于 {pub_str}，经验权重={weight_str} [{level}]）===\n"
            + _sample_story(s["answer"])
        )
    return "\n\n".join(blocks)


def _parse_profile_json(text):
    """从 LLM 回复中解析技能签名 JSON。先公共整块解析，失败再剥围栏
    取首尾大括号切片兜底；两者都要求 dict 且含 style 键。"""
    profile = extract_json_block(text)
    # extract_json_block 已做剥围栏 + 字符串感知平衡块 + strict=False
    # （嵌套大括号、字符串内换行/控制字符、JSON 后尾随内容都能处理）。
    # 成功标准：dict 且含 style 键。
    if not isinstance(profile, dict) or "style" not in profile:
        return None
    return profile


def _call_profile_llm(prompt, max_tokens=20000):
    """调用 LLM 剖析，按生成通道（LLM_MODE）分发；JSON 解析失败重试一次。

    API 通道：流式调用（思维链/正文阶段持续打心跳日志，webui 前端
    实时显示字符数，不再「卡住无反馈」）。
    Web 通道：复用 DeepSeek 网页版驱动（wait_complete 自带双阶段心跳）。

    注意：DeepSeek v4 系列是推理模型，思维链（reasoning_content）会先
    消耗输出预算，故 max_tokens 需远大于产出文本量，否则 content 为空。
    """
    for attempt in (1, 2):
        reply = _call_profile_llm_once(prompt, max_tokens)
        profile = _parse_profile_json(reply)
        if profile:
            return profile
        # 失败时带证据（长度 + 首尾片段）：剖析失败难复现时日志可直接
        # 判断是「读回残缺」还是「LLM 输出无效 JSON」
        log.warning("author_profiler: 第 %d 次剖析结果解析失败（重试），"
                    "回复长度=%d 首80=%r 尾80=%r", attempt,
                    len(reply or ""), (reply or "")[:80], (reply or "")[-80:])
        time.sleep(2)
    return None


def _call_profile_llm_once(prompt, max_tokens):
    """单次剖析：API 通道流式 / Web 通道网页版驱动。返回原始文本或 None。"""
    from config import LLM_MODE
    if LLM_MODE == "web":
        return _call_profile_llm_web(prompt)
    return _call_profile_llm_api(prompt, max_tokens)


def _resolve_profile_llm_config():
    """剖析用 LLM 配置：KB 专属配置优先，回退根 config（与 kb_manager 同语义）。"""
    from config import LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL
    from config import KB_LLM_API_KEY as _kb_key
    from config import KB_LLM_BASE_URL as _kb_url
    from config import KB_LLM_MODEL as _kb_model
    return (_kb_key or LLM_API_KEY, _kb_url or LLM_API_BASE_URL,
            _kb_model or LLM_API_MODEL)


def _call_profile_llm_api(prompt, max_tokens):
    """API 通道剖析（流式）：思维链心跳由 llm_client 统一输出，正文输出
    按累计 2000 字符打心跳，前端进度条全程可见（不再静默数分钟）。"""
    from llm_client import _call_llm_streaming
    api_key, base_url, model = _resolve_profile_llm_config()
    if not api_key or api_key == "密":
        log.error("author_profiler: API Key 未配置（剖析依赖生成通道配置）")
        return None

    # ★ 展示累计总量而非窗口计数：窗口计数会反复显示 ~2000，观感卡住
    heartbeat = {"n": 0, "total": 0}

    def _on_chunk(c):
        heartbeat["n"] += len(c)
        heartbeat["total"] += len(c)
        if heartbeat["n"] >= 2000:
            log.info("模型思考中… 已思考 %d 字符", heartbeat["total"])
            heartbeat["n"] = 0

    full_content, _elapsed, error = _call_llm_streaming(
        prompt, max_tokens=max_tokens, temperature=0.3,
        api_key=api_key, base_url=base_url, model=model,
        on_chunk=_on_chunk, label="剖析",
    )
    if error:
        log.error("author_profiler: 剖析 API 调用失败：%s", error)
        return None
    return full_content


def _call_profile_llm_web(prompt):
    """Web 通道剖析：复用 DeepSeek 网页版驱动完整生成流程。

    generate() 内部 wait_complete 自带双阶段心跳（思考/正文字符数），
    webui 前端进度条自动激活，与故事生成观感一致。
    """
    from web_drivers import get_driver
    try:
        return get_driver().generate(prompt)
    except Exception as exc:
        log.error("author_profiler: 网页版剖析失败：%s", exc)
        return None


def _report(progress, text, pct=None):
    """进度回调辅助：webui 传入时输出阶段文本（剖析中无百分比）。"""
    if progress:
        progress(text, pct)


def profile_author(author, min_likes=0, out_dir=None, progress=None):
    """完整流程：读故事 → 统计 → LLM 剖析 → 保存 → 返回 profile。

    参数：
        progress: 可选回调 progress(text, pct)；pct=None 表示不确定
        进度（剖析中，无法给出完成百分比）。

    返回：
        dict（含 text_stats / signature / source_stories）
        故事不足或剖析失败时返回 None
    """
    from applications.zhihu_story.prompts import AUTHOR_PROFILE_PROMPT

    _report(progress, f"读取作者「{author}」的采集样本…")
    stories = load_author_stories(author, min_likes=min_likes)
    if len(stories) < 2:
        log.warning("author_profiler: 作者「%s」可用故事不足（%d 篇），"
                    "至少需要 2 篇", author, len(stories))
        return None
    # 聚焦代表作：只取权重最高的前 N 篇，避免 prompt 过大导致 LLM
    # 输出残缺 JSON、解析失败（历史 bug：35 篇 → 108KB prompt，两连败）。
    if len(stories) > MAX_PROFILE_STORIES:
        stories = stories[:MAX_PROFILE_STORIES]

    _report(progress, f"已读取 {len(stories)} 篇样本，正在做文本统计…", 15)
    stats = compute_text_stats(stories)
    prompt = AUTHOR_PROFILE_PROMPT.format(
        author=author,
        text_stats=_format_stats_for_prompt(stats),
        consistency=_format_consistency_for_prompt(stats),
        stories=_format_stories_for_prompt(stories),
    )

    log.info("author_profiler: 剖析「%s」（%d 篇，共 %d 字）...",
             author, len(stories), stats["total_chars"])
    start = time.time()
    _report(progress, "大模型剖析中（分析文风与技法，通常 1-3 分钟）…")
    signature = _call_profile_llm(prompt)
    if not signature:
        log.error("author_profiler: 剖析失败（LLM 未返回有效 JSON）")
        return None
    log.info("author_profiler: 剖析完成（%.1fs）", time.time() - start)

    profile = {
        "author": author,
        "profiled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_stories": [
            {"title": s["title"], "likes": s["footer"].get("likes"),
             "chars": s["chars"]}
            for s in stories
        ],
        "text_stats": stats,
        "signature": signature,
    }

    _report(progress, "剖析完成，正在保存签名…", 95)
    path = save_profile(profile, out_dir=out_dir)
    _report(progress, f"签名已保存 → {os.path.basename(path)}", 100)
    log.info("author_profiler: 技能签名已保存 → %s", path)
    return profile


# ============================================================
# 3b. 通用写作风格（顶层，跨作者）
# ============================================================

GENERAL_PROFILE_FILE = "_general.json"


def load_general_stories(min_likes=0, source=None, authors=None):
    """读取采集库全部作者的精华故事（用于提炼通用写作风格）。

    min_likes 过滤低质样本（无互动数据的视为 0）；authors 为 None 时
    跨全部作者，否则仅限列出的作者。返回 [{author, title, answer,
    footer, chars, weight, publish_date}]，按经验权重降序。
    """
    source = source or STORY_LIB
    if not os.path.exists(source):
        log.warning("author_profiler: 采集库不存在：%s", source)
        return []

    stories = []
    for rec in iter_collected_stories(source):
        author = rec.get("author") or ""
        if authors is not None and author not in authors:
            continue
        answer = (rec.get("answer") or "").strip()
        if len(answer) < 100:
            continue
        footer = rec.get("footer") or {}
        likes = footer.get("likes")
        if not isinstance(likes, (int, float)) or likes < 0:
            likes = 0
        if likes < min_likes:
            continue
        stories.append({
            "author": author,
            "title": (rec.get("title") or "").strip(),
            "answer": answer,
            "footer": footer,
            "chars": len(answer),
            "weight": story_weight(footer),
            "publish_date": parse_publish_date(footer),
        })

    stories.sort(key=lambda s: s["weight"], reverse=True)
    return stories


def profile_general(min_likes=0, out_dir=None, authors=None, max_stories=30,
                    progress=None):
    """提炼【通用写作风格签名】（顶层）：跨作者的精华作品共同技法。

    与 profile_author 的差异：样本来自多位作者，提炼的是知乎高赞
    故事的通用写作规律（开局/节奏/对话/收尾的行业级共性），供
    生成链路在注入作者专用签名之前先注入。

    返回 dict（author="通用"）或 None。
    """
    from applications.zhihu_story.prompts import GENERAL_PROFILE_PROMPT

    _report(progress, "读取采集库样本（跨全部作者）…")
    stories = load_general_stories(
        min_likes=min_likes, source=None, authors=authors)
    stories = stories[:max_stories] if max_stories else stories
    if len(stories) < 3:
        log.warning("author_profiler: 通用风格样本不足（%d 篇，"
                    "至少 3 篇），跳过", len(stories))
        return None

    _report(progress, f"已读取 {len(stories)} 篇样本，正在做文本统计…", 15)
    # 通用统计需要 author 字段参与，compute_text_stats 不关心作者名
    stats = compute_text_stats(stories)
    prompt = GENERAL_PROFILE_PROMPT.format(
        authors="、".join(sorted({s["author"] for s in stories})),
        text_stats=_format_stats_for_prompt(stats),
        consistency=_format_consistency_for_prompt(stats),
        stories=_format_stories_for_prompt(stories),
    )

    log.info("author_profiler: 提炼通用写作风格（%d 位作者，%d 篇，"
             "共 %d 字）...", len({s["author"] for s in stories}),
             len(stories), stats["total_chars"])
    start = time.time()
    _report(progress, "大模型剖析中（分析高赞故事共性，通常 1-3 分钟）…")
    signature = _call_profile_llm(prompt)
    if not signature:
        log.error("author_profiler: 通用风格剖析失败（LLM 未返回有效 JSON）")
        return None
    log.info("author_profiler: 通用风格提炼完成（%.1fs）", time.time() - start)

    profile = {
        "author": "通用",
        "profiled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_stories": [
            {"author": s["author"], "title": s["title"],
             "likes": s["footer"].get("likes"), "chars": s["chars"]}
            for s in stories
        ],
        "text_stats": stats,
        "signature": signature,
    }

    _report(progress, "剖析完成，正在保存签名…", 95)
    path = save_profile(profile, out_dir=out_dir, filename=GENERAL_PROFILE_FILE)
    _report(progress, "签名已保存", 100)
    log.info("author_profiler: 通用技能签名已保存 → %s", path)
    return profile


def load_general_profile(out_dir=None):
    """读取通用写作风格签名；本地未提炼时回退内置通用规则。

    优先读 data/authors/_general.json（提炼产物）；不存在或解析失败
    时回退 config/builtin_general_profile.json（随安装包分发，保证
    新环境开箱即有可用的通用文风）。仍失败返回 None。
    """
    profile = load_author_profile(
        GENERAL_PROFILE_FILE.rstrip(".json"), out_dir=out_dir,
        filename=GENERAL_PROFILE_FILE)
    if profile:
        return profile
    try:
        from core.paths import program as _program_path
        path = _program_path("config", "builtin_general_profile.json")
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
        if isinstance(profile, dict) and profile.get("signature"):
            log.info("author_profiler: 使用内置通用写作规则（%s）", path)
            return profile
    except Exception as exc:
        log.warning("author_profiler: 读取内置通用规则失败：%s", exc)
    return None


# ============================================================
# 4. 存取
# ============================================================

def save_profile(profile, out_dir=None, filename=None):
    """保存技能签名到 data/authors/{作者名}.json，返回路径。"""
    out_dir = out_dir or AUTHORS_DIR
    os.makedirs(out_dir, exist_ok=True)
    if filename is None:
        name = profile.get("author") or "unknown"
        filename = sanitize_filename(name) + ".json"
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    return path


def load_author_profile(author, out_dir=None, filename=None):
    """读取已保存的作者技能签名；不存在或解析失败返回 None。

    返回 dict（含 signature / text_stats / source_stories）。
    """
    out_dir = out_dir or AUTHORS_DIR
    if filename is None:
        filename = f"{sanitize_filename(author)}.json"
    path = os.path.join(out_dir, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("author_profiler: 读取技能签名失败 %s：%s", path, exc)
        return None
    if not isinstance(profile, dict) or "signature" not in profile:
        return None
    return profile


# ============================================================
# 5. 注入渲染（生成链路用）
# ============================================================

def render_general_section(profile):
    """把通用写作风格签名渲染为注入节（置于作者签名之前）。

    渲染失败时返回空串（不阻断作者签名注入）。
    """
    if not profile:
        return ""
    try:
        from applications.zhihu_story.prompts import GENERAL_STYLE_INJECT_SECTION
        sig = profile.get("signature") or {}
        return GENERAL_STYLE_INJECT_SECTION.format(
            style=sig.get("style", "（未提炼）"),
            opening_patterns="\n".join(
                f"- {item}" for item in (sig.get("opening_patterns") or [])),
            narrative_techniques="\n".join(
                f"- {item}" for item in (sig.get("narrative_techniques") or [])),
            dialogue_style=sig.get("dialogue_style", "（未提炼）"),
            tone=sig.get("tone", "（未提炼）"),
            sentence_rhythm=sig.get("sentence_rhythm", "（未提炼）"),
            avoid="\n".join(
                f"- {item}" for item in (sig.get("avoid") or [])),
        )
    except Exception as exc:
        log.warning("author_profiler: 通用风格渲染失败，跳过：%s", exc)
        return ""


def render_style_section(profile):
    """把作者技能签名渲染为注入生成 prompt 的风格节文本。

    由 llm_api.build_story_prompt 在 author_profile 模式下追加。
    v2 新增维度缺失时优雅降级为"（未提炼）"，旧签名照常渲染。
    """
    sig = profile.get("signature") or {}

    def bullets(items):
        if not items:
            return "（未提炼）"
        return "\n".join(f"- {item}" for item in items)

    def taboos(items):
        """禁忌清单渲染：dict 形式带来源分级，字符串列表兼容旧版。"""
        if not items:
            return "（未提炼）"
        lines = []
        for item in items:
            if isinstance(item, dict):
                rule = item.get("rule", "")
                source = item.get("source", "")
                tag = f" [{source}]" if source else ""
                lines.append(f"- {rule}{tag}")
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)

    excerpts = sig.get("excerpts") or {}
    excerpt_text = "\n".join(
        f"**{label}**：{text}"
        for label, text in [("开头", excerpts.get("opening")),
                            ("高潮", excerpts.get("climax")),
                            ("结尾", excerpts.get("ending"))]
        if text
    ) or "（未提炼）"

    from applications.zhihu_story.prompts import AUTHOR_STYLE_INJECT_SECTION
    return AUTHOR_STYLE_INJECT_SECTION.format(
        author=profile.get("author", "未知作者"),
        style=sig.get("style", "（未提炼）"),
        sentence_rhythm=sig.get("sentence_rhythm", "（未提炼）"),
        sensory_preference=sig.get("sensory_preference", "（未提炼）"),
        metaphor_fingerprint=sig.get("metaphor_fingerprint", "（未提炼）"),
        dialogue_tag_style=sig.get("dialogue_tag_style", "（未提炼）"),
        opening_patterns=bullets(sig.get("opening_patterns")),
        narrative_techniques=bullets(sig.get("narrative_techniques")),
        character_patterns=bullets(sig.get("character_patterns")),
        dialogue_style=sig.get("dialogue_style", "（未提炼）"),
        tone=sig.get("tone", "（未提炼）"),
        signature_phrases=bullets(sig.get("signature_phrases")),
        taboo_list=taboos(sig.get("taboo_list")),
        tension_conflicts=bullets(sig.get("tension_conflicts")),
        cross_story_consistency=sig.get("cross_story_consistency", "（未提炼）"),
        avoid=bullets(sig.get("avoid")),
        excerpts=excerpt_text,
    )


# ============================================================
# CLI
# ============================================================

def _main():
    parser = argparse.ArgumentParser(
        description="作者写作技能提炼（--general 提炼顶层通用风格）")
    parser.add_argument("author", nargs="?", default=None,
                        help="作者名（须与采集库中的 author 字段一致）")
    parser.add_argument("--general", action="store_true",
                        help="提炼通用写作风格（顶层，跨作者），无需作者名")
    parser.add_argument("--min-likes", type=int, default=0,
                        help="只剖析赞同数 >= N 的故事（默认 0）")
    parser.add_argument("--max-stories", type=int, default=30,
                        help="通用风格最多使用多少篇样本（默认 30）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )

    if args.general:
        profile = profile_general(
            min_likes=args.min_likes, max_stories=args.max_stories)
        if not profile:
            sys.exit(1)
        sig = profile["signature"]
        print("\n=== 通用风格签名摘要 ===")
        print(f"文风：{sig.get('style', '')[:80]}")
        print(f"基调：{sig.get('tone', '')[:50]}")
        print(f"读者偏好：{len(sig.get('reader_preferences', []))} 条")
        print(f"开头技法：{len(sig.get('opening_patterns', []))} 条")
        print(f"叙事技法：{len(sig.get('narrative_techniques', []))} 条")
        sys.exit(0)

    if not args.author:
        parser.error("需要作者名，或使用 --general 提炼通用风格")

    profile = profile_author(args.author, min_likes=args.min_likes)
    if not profile:
        sys.exit(1)
    sig = profile["signature"]
    print("\n=== 技能签名摘要 ===")
    print(f"文风：{sig.get('style', '')[:80]}")
    print(f"基调：{sig.get('tone', '')[:50]}")
    print(f"开头技法：{len(sig.get('opening_patterns', []))} 条")
    print(f"叙事技法：{len(sig.get('narrative_techniques', []))} 条")
    print(f"惯用句式：{len(sig.get('signature_phrases', []))} 条")


if __name__ == "__main__":
    _main()
