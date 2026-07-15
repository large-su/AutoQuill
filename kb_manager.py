# ============================================================
# kb_manager.py — 自生长知识库管理器 v2.1
#
# v2.1 更新：
#   - 配方新增 perspective（叙事视角）和 tone（情感基调）维度
#   - 配方选取从纯随机改为基于评分的加权随机
#   - 新增评分回写功能（update_recipe_scores）
#
# 用法：
#   python kb_manager.py --stats          查看知识库统计
#   python kb_manager.py --cold-start     自动采集文章并冷启动
#   python kb_manager.py --compress       手动触发压缩合并
#   python kb_manager.py --show [题材]    查看指定题材的配方
#   python kb_manager.py --ranking        查看配方评分排行
#   python kb_manager.py --rebuild        从原始素材重建知识库

# 也可被主流程 import 调用：
#   from kb_manager import extract_and_store, get_recipe, load_kb
#   from kb_manager import classify_genre, update_recipe_scores
# ============================================================

import json
import os
import re
import random
import math
import time
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ============================================================
# 知识库文件操作
# ============================================================

KB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "knowledge_base.json")

_EMPTY_KB = {
    "version": 2,
    "last_updated": None,
    "stats": {
        "total_recipes": 0,
        "genres": 0,
        "sources_analyzed": 0,
    },
    "recipes": []
}

def load_kb():
    """加载知识库，不存在则返回空知识库"""
    if not os.path.exists(KB_FILE):
        return json.loads(json.dumps(_EMPTY_KB))
    try:
        with open(KB_FILE, 'r', encoding='utf-8') as f:
            kb = json.load(f)
        return kb
    except Exception as e:
        log.error(f"知识库加载失败：{e}")
        return json.loads(json.dumps(_EMPTY_KB))

def save_kb(kb):
    """保存知识库"""
    kb["last_updated"] = datetime.now().isoformat()
    kb["stats"]["total_recipes"] = len(kb.get("recipes", []))
    genres = set(r.get("genre", "未分类") for r in kb.get("recipes", []))
    kb["stats"]["genres"] = len(genres)

    with open(KB_FILE, 'w', encoding='utf-8') as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)
    log.info(f"知识库已保存：{kb['stats']['total_recipes']} 条配方，{kb['stats']['genres']} 个题材")

# ============================================================
# 配方选取（加权随机）
# ============================================================

def get_recipe(genre, kb=None):
    """
    从知识库中获取一个配方。

    选取策略：epsilon-greedy 加权随机。
    - 90% 概率按评分加权选取（exploitation）
    - 10% 概率均匀随机选取（exploration，防止陷入局部最优）
    - 有评分的配方：权重 = avg_score（分数越高越容易被选中）
    - 无评分的配方：权重 = DEFAULT_WEIGHT（满分 60 的 ~47%，给新配方机会但不占优）
    - 同时确保低分配方不被完全排除（设最低权重 10）

    优先从指定题材中选取；题材为空时兜底到全库。
    """
    if kb is None:
        kb = load_kb()

    recipes = kb.get("recipes", [])
    if not recipes:
        return None

    # 按优先级找候选池
    genre_recipes = [r for r in recipes if r.get("genre") == genre]
    if not genre_recipes:
        genre_recipes = [r for r in recipes if r.get("genre") == "其他"]
    if not genre_recipes:
        genre_recipes = recipes  # 最终兜底：全库

    # epsilon-greedy：10% 概率均匀随机探索
    EPSILON = 0.1
    if random.random() < EPSILON:
        chosen = random.choice(genre_recipes)
        chosen["times_used"] = chosen.get("times_used", 0) + 1
        score_info = f"（评分 {chosen['avg_score']:.1f}，使用 {chosen['times_used']} 次）" if chosen.get("avg_score") else f"（未评分，使用 {chosen['times_used']} 次）"
        log.info(f"  配方选取（探索）：[{chosen.get('genre')}] {chosen.get('hook', '')[:25]}... {score_info}")
        return chosen

    # 计算加权
    DEFAULT_WEIGHT = 28
    MIN_WEIGHT = 10

    weights = []
    for r in genre_recipes:
        avg = r.get("avg_score")
        if avg is not None and avg > 0:
            weights.append(max(avg, MIN_WEIGHT))
        else:
            weights.append(DEFAULT_WEIGHT)

    # 加权随机选取
    chosen = random.choices(genre_recipes, weights=weights, k=1)[0]
    chosen["times_used"] = chosen.get("times_used", 0) + 1

    # 记录选取日志
    score_info = f"（评分 {chosen['avg_score']:.1f}，使用 {chosen['times_used']} 次）" if chosen.get("avg_score") else f"（未评分，使用 {chosen['times_used']} 次）"
    log.info(f"  配方选取：[{chosen.get('genre')}] {chosen.get('hook', '')[:25]}... {score_info}")

    return chosen

def get_all_genres(kb=None):
    """获取知识库中所有题材列表"""
    if kb is None:
        kb = load_kb()
    genres = set(r.get("genre", "未分类") for r in kb.get("recipes", []))
    return sorted(genres)

# ============================================================
# 评分回写
# ============================================================

def update_recipe_scores(score_map):
    """
    批量更新配方评分。

    参数：
        score_map: {recipe_id: score, ...}
            例如 {"recipe_012": 42, "recipe_035": 28}

    机制：
    - 每个配方记录 score_history（历史得分列表）
    - avg_score = 历史得分的加权平均（近期权重更高）
    - score_count = 评分次数
    """
    if not score_map:
        return

    kb = load_kb()
    updated = 0

    for recipe in kb.get("recipes", []):
        rid = recipe.get("id")
        if rid in score_map:
            score = score_map[rid]
            if not isinstance(score, (int, float)) or score <= 0:
                continue

            # 追加到历史
            history = recipe.get("score_history", [])
            history.append(score)
            recipe["score_history"] = history
            recipe["score_count"] = len(history)

            # 计算加权平均（近期权重更高）
            # 权重：最近一次=1.0，倒数第二次=0.85，倒数第三次=0.72...
            decay = 0.85
            weighted_sum = 0
            weight_sum = 0
            for i, s in enumerate(reversed(history)):
                w = decay ** i
                weighted_sum += s * w
                weight_sum += w
            recipe["avg_score"] = round(weighted_sum / weight_sum, 1)

            updated += 1
            log.info(f"  评分回写：{rid} ← {score} 分（累计 {len(history)} 次，均分 {recipe['avg_score']}）")

    if updated > 0:
        save_kb(kb)
        log.info(f"  ✓ 已更新 {updated} 个配方的评分")

    return updated

# ============================================================
# 配方提炼（调用 LLM）
# ============================================================

def _call_llm(prompt, max_tokens=2000, temperature=0.3, timeout=120):
    """内部 LLM 调用（非流式）

    优先使用 KB 专属的 API Key / Base URL / Model（知识库任务可独立于故事生成配置）。
    若 KB 专属配置缺失，回退到故事生成的配置。
    """
    import requests
    from config import LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL

    # ★ KB 专属配置优先，缺失时回退到故事生成配置
    try:
        from config import KB_LLM_API_KEY as _kb_key
    except ImportError:
        _kb_key = LLM_API_KEY
    try:
        from config import KB_LLM_BASE_URL as _kb_url
    except ImportError:
        _kb_url = LLM_API_BASE_URL
    try:
        from config import KB_LLM_MODEL as _kb_model
    except ImportError:
        _kb_model = LLM_API_MODEL
    try:
        from config import KB_LLM_EXTRA_BODY as _kb_extra_body
    except ImportError:
        _kb_extra_body = {}

    api_key = _kb_key or LLM_API_KEY
    base_url = _kb_url or LLM_API_BASE_URL
    model = _kb_model or LLM_API_MODEL
    extra_body = dict(_kb_extra_body or {})

    if not api_key or api_key == "密":
        log.error("API Key 未配置")
        return None

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if extra_body:
        payload.update(extra_body)

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.encoding = "utf-8"  # 强制 UTF-8，避免响应头无 charset 时中文乱码
        if resp.status_code != 200:
            log.error(f"LLM 请求失败：HTTP {resp.status_code}")
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # ★ Token 用量上报
        usage = data.get("usage")
        if usage:
            try:
                from llm_token_tracker import tracker
                tracker.report(model, usage)
            except Exception:
                pass

        return content
    except Exception as e:
        log.error(f"LLM 调用异常：{e}")
        return None

def _parse_json_response(text):
    """解析 LLM 返回的 JSON（多层容错处理）"""
    if not text:
        return []

    clean = text.strip()
    # 剥离 markdown 代码块
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    clean = clean.strip()

    # 第1层：直接解析
    try:
        result = json.loads(clean)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return []
    except json.JSONDecodeError:
        pass

    # 第2层：正则提取 [...] 后解析
    m = re.search(r'\[.*\]', clean, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # 第3层：修复常见 JSON 格式错误后重试
    repaired = _repair_json(clean)
    if repaired != clean:
        try:
            result = json.loads(repaired)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return [result]
        except json.JSONDecodeError:
            pass

    # 第4层：从修复后的文本中提取 [...] 再试
    m2 = re.search(r'\[.*\]', repaired, re.DOTALL)
    if m2:
        try:
            return json.loads(m2.group())
        except json.JSONDecodeError:
            pass

    # 第5层：逐对象提取（处理嵌套大括号）
    objects = _extract_json_objects(repaired if repaired != clean else clean)
    if objects:
        return objects

    return []


def _repair_json(text):
    """修复常见 JSON 格式错误：尾部逗号、未闭合括号等。"""
    repaired = text.strip()
    # 移除 } 或 ] 前的尾部逗号（最常见错误）
    repaired = re.sub(r',\s*(\}|\])', r'\1', repaired)
    # 移除字符串内未转义的控制字符
    repaired = repaired.replace('\t', ' ').replace('\r', '')
    return repaired


def _extract_json_objects(text):
    """从文本中逐层提取 JSON 对象（处理嵌套大括号）。"""
    objects = []
    # 找所有顶级 { ... } 块（跟踪嵌套深度）
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i+1])
                    objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return objects

def _sample_article(text, head=500, mid_samples=3, mid_each=300, tail=500):
    """
    对长文章做多点智能采样，保留开头、中间、结尾的结构信息。

    避免纯截断导致 LLM 看不到中段节奏和结尾收束方式。
    短文（< head + tail + 300）直接返回全文。
    """
    total = len(text)
    min_len = head + tail + 300
    if total <= min_len:
        return text

    parts = [text[:head]]

    # 中间均匀采样 mid_samples 段
    mid_start = head + (total - head - tail) // (mid_samples + 1)
    for k in range(1, mid_samples + 1):
        center = head + k * (total - head - tail) // (mid_samples + 1)
        begin = max(head, center - mid_each // 2)
        end = min(total - tail, center + mid_each // 2)
        parts.append(f"\n...[片段{ k}]...\n" + text[begin:end])

    parts.append("\n...[结尾]...\n" + text[-tail:])
    return "".join(parts)


def extract_single_recipe(title, answer, timeout=90):
    """
    从单篇文章提炼配方（用于异步流水线，输入小、响应快、不超时）。

    与 extract_recipes 的批量模式互补：批量模式适合离线补炼，
    单篇模式适合采集过程中实时异步提炼。

    参数：
        title: 问题标题
        answer: 高赞回答全文
        timeout: HTTP 超时秒数（单篇 90s，留足余量应对服务端波动）

    返回：
        recipe dict 或 None
    """
    import sys
    try:
        from config import RECIPE_EXTRACT_PROMPT
    except Exception as e:
        log.error(f"[async] RECIPE_EXTRACT_PROMPT 导入失败：{e}")
        return None

    try:
        from config import RECIPE_VERBOSE_MODE
    except ImportError:
        RECIPE_VERBOSE_MODE = False

    # 单篇提炼需要足够的输出 token：8 个字段 × ~100 汉字 × ~2 token/字 ≈ 1600
    # 之前的 250 严重不足，导致 JSON 被截断，解析全部失败
    per_recipe_tokens = 2400 if RECIPE_VERBOSE_MODE else 1000
    tokens_needed = per_recipe_tokens + 200

    prompt = RECIPE_EXTRACT_PROMPT + "\n\n"
    answer_preview = _sample_article(answer)
    prompt += f"--- 文章 1（问题：{title[:60]}）---\n"
    prompt += answer_preview + "\n\n"

    log.info(f"[async] 开始提炼：{title[:40]}...（{len(prompt)} 字符，"
             f"timeout={timeout}s）")

    reply = None
    for attempt in range(2):
        try:
            reply = _call_llm(prompt, max_tokens=tokens_needed,
                              temperature=0.3 if attempt == 0 else 0.5,
                              timeout=timeout)
        except Exception as e:
            log.error(f"[async] _call_llm 异常（第{attempt+1}次）：{e}")

        if reply and reply.strip():
            break
        if attempt == 0:
            log.warning(f"[async] 空响应，3s 后重试一次：{title[:40]}...")
            import time as _time
            _time.sleep(3)

    if reply is None or not reply.strip():
        log.warning(f"[async] _call_llm 返回空（重试后仍失败）：{title[:40]}...")
        return None

    recipes = _parse_json_response(reply)
    if not recipes:
        log.warning(f"[async] JSON 解析失败（{len(reply)} 字符）："
                    f"{title[:40]}...  ← 前200字符: {reply[:200]}")
        return None

    recipe = recipes[0]
    recipe.pop("index", None)
    log.info(f"[async] ✓ 提炼成功：{title[:40]}... "
             f"[{recipe.get('genre', '?')}]")
    return recipe


def extract_recipes(articles, batch_size=5):
    """
    从参考文章中批量提炼配方。

    参数：
        articles: [{"title": "问题标题", "answer": "高赞回答文本"}, ...]
        batch_size: 每批处理几篇

    返回：
        [{"genre": "...", "hook": "...", ..., "perspective": "...", "tone": "..."}, ...]
    """
    from config import RECIPE_EXTRACT_PROMPT

    all_recipes = []

    for i in range(0, len(articles), batch_size):
        batch = articles[i:i + batch_size]
        log.info(f"  提炼第 {i + 1}-{min(i + batch_size, len(articles))}/{len(articles)} 篇...")

        prompt = RECIPE_EXTRACT_PROMPT + "\n\n"
        for j, article in enumerate(batch):
            answer_preview = _sample_article(article["answer"])
            prompt += f"--- 文章 {j + 1}（问题：{article['title'][:60]}）---\n"
            prompt += answer_preview
            prompt += "\n\n"

        # verbose 模式下配方字段长 3 倍左右，需要更大的 max_tokens
        try:
            from config import RECIPE_VERBOSE_MODE
        except ImportError:
            RECIPE_VERBOSE_MODE = False
        per_recipe_tokens = 900 if RECIPE_VERBOSE_MODE else 250
        tokens_needed = len(batch) * per_recipe_tokens + 300

        # === 首次尝试（temperature=0.3）===
        reply = _call_llm(prompt, max_tokens=tokens_needed, temperature=0.3)
        batch_recipes = _parse_json_response(reply) if reply else []

        # === 整批重试（成功率 < 50% 触发一次）===
        # 阈值设为一半，是为了捕捉"大面积失败"（通常是 LLM 整批返回了格式错误的内容）
        # 零星缺字段不触发重试——那种情况下面的 for 循环会跳过，不影响本批其他条目
        success_rate = len(batch_recipes) / max(len(batch), 1)
        if success_rate < 0.5:
            log.warning(
                f"  第 {i + 1} 批首次提炼成功率低"
                f"（{len(batch_recipes)}/{len(batch)}），重试一次"
            )
            retry_tokens = int(tokens_needed * 1.5)
            retry_reply = _call_llm(prompt, max_tokens=retry_tokens,
                                    temperature=0.1)
            retry_recipes = _parse_json_response(retry_reply) if retry_reply else []
            if len(retry_recipes) > len(batch_recipes):
                log.info(
                    f"  重试成功：{len(retry_recipes)} 个配方"
                    f"（原 {len(batch_recipes)}）"
                )
                batch_recipes = retry_recipes
            else:
                log.warning(
                    f"  重试仍未改善（{len(retry_recipes)} vs "
                    f"{len(batch_recipes)}），放弃本批"
                )

        if not batch_recipes:
            log.warning(f"  第 {i + 1} 批提炼失败，跳过")
            continue

        for k, recipe in enumerate(batch_recipes):
            # 必要字段检查（兼容新旧维度）
            required = ["genre", "hook", "conflict", "pacing", "style", "character"]
            if not all(key in recipe for key in required):
                log.warning(f"  配方缺少必要字段，跳过：{recipe}")
                continue

            # 确保新字段存在（如果 LLM 漏了就给默认值）
            if "perspective" not in recipe:
                recipe["perspective"] = "未指定"
            if "tone" not in recipe:
                recipe["tone"] = "未指定"

            # 补充元信息
            source_idx = i + recipe.get("index", k + 1) - 1
            if source_idx < len(articles):
                recipe["source_title"] = articles[source_idx]["title"][:80]
            recipe["_source_idx"] = source_idx  # 供调用方按索引精确绑定
            recipe["added_at"] = datetime.now().strftime("%Y-%m-%d")
            recipe["times_used"] = 0
            recipe["avg_score"] = None
            recipe["score_history"] = []
            recipe["score_count"] = 0

            recipe.pop("index", None)
            all_recipes.append(recipe)

        log.info(f"    提炼成功：{len(batch_recipes)} 个配方")

    return all_recipes

def extract_and_store(articles):
    """
    提炼配方并存入知识库（主流程调用的入口）。

    返回：新增配方数量
    """
    if not articles:
        return []

    log.info(f"{'=' * 50}")
    log.info(f"知识库：从 {len(articles)} 篇文章中提炼配方")
    log.info(f"{'=' * 50}")

    new_recipes = extract_recipes(articles)
    if not new_recipes:
        log.warning("未提炼出任何配方")
        return []

    kb = load_kb()

    # 生成 ID
    max_id = 0
    for r in kb.get("recipes", []):
        rid = r.get("id", "recipe_000")
        try:
            num = int(rid.split("_")[1])
            max_id = max(max_id, num)
        except (IndexError, ValueError):
            pass

    for recipe in new_recipes:
        max_id += 1
        recipe["id"] = f"recipe_{max_id:03d}"

    kb["recipes"].extend(new_recipes)
    kb["stats"]["sources_analyzed"] = kb["stats"].get("sources_analyzed", 0) + len(articles)

    save_kb(kb)

    log.info(f"  ✓ 新增 {len(new_recipes)} 个配方（总计 {len(kb['recipes'])} 个）")

    from config import KB_MERGE_TRIGGER
    if len(kb["recipes"]) >= KB_MERGE_TRIGGER:
        log.info(f"  知识库条目（{len(kb['recipes'])}）已达压缩阈值（{KB_MERGE_TRIGGER}），建议运行：")
        log.info(f"    python kb_manager.py --compress")

    return new_recipes

# ============================================================
# 题材判断（独立功能）
# ============================================================

def classify_genre(question_title):
    """用 LLM 判断问题所属的故事题材。"""
    from config import GENRE_CLASSIFY_PROMPT

    prompt = GENRE_CLASSIFY_PROMPT + f"\n\n问题标题：{question_title}\n\n请直接返回题材名称（2-6个字），不要其他文字。"

    reply = _call_llm(prompt, max_tokens=20, temperature=0.1)

    if reply:
        genre = reply.strip().strip('"""' + "''。.，,")
        if 2 <= len(genre) <= 10:
            log.info(f"  题材判断：「{question_title[:30]}...」→ {genre}")
            return genre

    log.warning(f"  题材判断失败，使用'其他'")
    return "其他"

def classify_genres_batch(titles):
    """批量题材判断。"""
    from config import GENRE_CLASSIFY_PROMPT

    if not titles:
        return []
    if len(titles) == 1:
        return [classify_genre(titles[0])]

    prompt = GENRE_CLASSIFY_PROMPT + "\n\n请对以下每个问题判断题材，按编号返回，每行一个（格式：编号. 题材名称）：\n"
    for i, title in enumerate(titles):
        prompt += f"{i + 1}. {title}\n"

    reply = _call_llm(prompt, max_tokens=len(titles) * 20 + 50, temperature=0.1)

    if not reply:
        return ["其他"] * len(titles)

    genres = ["其他"] * len(titles)
    for line in reply.strip().split("\n"):
        line = line.strip()
        m = re.match(r'(\d+)[.、\s]+(.+)', line)
        if m:
            idx = int(m.group(1)) - 1
            genre = m.group(2).strip().strip('"""' + "''。.，,")
            if 0 <= idx < len(titles) and 2 <= len(genre) <= 10:
                genres[idx] = genre

    for i, (title, genre) in enumerate(zip(titles, genres)):
        log.info(f"  题材：「{title[:25]}...」→ {genre}")

    return genres

# ============================================================
# 知识库压缩
# ============================================================

def compress_kb():
    """压缩知识库：合并同题材下相似的配方。"""
    kb = load_kb()
    recipes = kb.get("recipes", [])

    if not recipes:
        print("  知识库为空，无需压缩")
        return

    before_count = len(recipes)
    print(f"\n  压缩前：{before_count} 个配方")

    genre_groups = {}
    for r in recipes:
        genre = r.get("genre", "未分类")
        genre_groups.setdefault(genre, []).append(r)

    from config import KB_MAX_PER_GENRE

    merged_recipes = []

    for genre, group in genre_groups.items():
        if len(group) <= 3:
            merged_recipes.extend(group)
            continue

        print(f"\n  压缩「{genre}」（{len(group)} 条）...")

        prompt = f"""你是知识库管理员。以下是「{genre}」题材下的创作配方，请识别并合并相似的条目。

合并规则：
- 如果两个配方的 hook、conflict、character 三个维度中有两个以上高度相似，就合并为一条
- 合并时保留更精炼、更抽象的描述
- 不要改变条目的抽象程度，不要添加具体情节
- perspective 和 tone 字段也需要保留
- 最终每个题材保留不超过 {KB_MAX_PER_GENRE} 个配方

当前配方：
"""
        for i, r in enumerate(group):
            prompt += f"\n{i + 1}. hook={r.get('hook', '')} | conflict={r.get('conflict', '')} | "
            prompt += f"pacing={r.get('pacing', '')} | style={r.get('style', '')} | "
            prompt += f"character={r.get('character', '')} | perspective={r.get('perspective', '未指定')} | "
            prompt += f"tone={r.get('tone', '未指定')}"

        prompt += f"""

请返回合并后的配方列表，严格 JSON 格式（不要其他文字）：
[
  {{"hook": "...", "conflict": "...", "pacing": "...", "style": "...", "character": "...", "perspective": "...", "tone": "..."}}
]"""

        reply = _call_llm(prompt, max_tokens=len(group) * 250, temperature=0.2)
        merged = _parse_json_response(reply)

        if merged and len(merged) < len(group):
            for m in merged:
                m["genre"] = genre
                m["id"] = f"recipe_merged_{len(merged_recipes):03d}"
                m["added_at"] = datetime.now().strftime("%Y-%m-%d")
                m["times_used"] = 0
                m["avg_score"] = None
                m["score_history"] = []
                m["score_count"] = 0
            merged_recipes.extend(merged)
            print(f"    {len(group)} → {len(merged)} 条")
        else:
            merged_recipes.extend(group)
            print(f"    无法进一步压缩，保留原样")

    kb["recipes"] = merged_recipes
    save_kb(kb)

    after_count = len(merged_recipes)
    print(f"\n  压缩完成：{before_count} → {after_count} 条（减少 {before_count - after_count} 条）")

# ============================================================
# 冷启动 / 补充知识（复用批量采集流程）
# ============================================================

def cold_start():
    """
    冷启动 / 补充知识：自动采集知乎文章并提炼配方。

    采用和 zhihu_auto.py 批量模式完全一致的采集流程：
    打开推荐页 → OCR 识别多个问题 → 逐个进入采集回答 → 滚动刷新 → 继续采集
    """

    print(f"""
    ╔══════════════════════════════════════════╗
    ║   📚 知识库采集 + 提炼                    ║
    ╚══════════════════════════════════════════╝
    """)

    kb = load_kb()
    current = len(kb.get("recipes", []))
    print(f"  当前知识库：{current} 条配方")

    try:
        count_input = input("\n  请输入要采集的故事数量（推荐 10-20）：").strip()
        target_count = int(count_input)
    except (ValueError, EOFError):
        print("  输入无效，默认采集 10 篇")
        target_count = 10

    if target_count <= 0:
        print("  数量必须大于 0")
        return

    # ===== 初始化环境 =====
    try:
        from zhihu_auto import (collect_materials_batch, load_coords,
                                focus_edge, get_bounds)
    except ImportError as e:
        print(f"  导入失败：{e}")
        print("  请确保 zhihu_auto.py 在同一目录下")
        return

    if not load_coords():
        print("  ❌ 坐标未校准！请先运行：python zhihu_auto.py --calibrate")
        return

    print("  加载 OCR...")
    from ocr_utils import _get_engine
    _get_engine()
    print("  ✓ 就绪")

    input(f"\n  将采集 {target_count} 篇故事，按 Enter 开始...")

    focus_edge()
    time.sleep(0.5)

    # ===== 批量采集（复用 zhihu_auto 的成熟流程） =====
    lx, rx, ty, by = get_bounds()
    materials = collect_materials_batch(target_count, lx, rx, ty, by)

    if not materials:
        print("\n  未采集到任何素材")
        return

    print(f"\n  采集完成：共 {len(materials)} 篇")
    for m in materials:
        print(f"    {m['index']}. 「{m['title'][:40]}...」（{len(m['answer'])} 字）")

    # ===== 提炼配方并存入知识库 =====
    print(f"\n  开始提炼配方...")
    articles = [{"title": m["title"], "answer": m["answer"]} for m in materials]
    count = extract_and_store(articles)

    print(f"\n  ✓ 完成！新增 {count} 个配方")
    show_stats()


# ============================================================
# 从原始素材重建知识库
# ============================================================

RAW_MATERIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw_materials.jsonl")

def rebuild_from_raw():
    """
    从 data/raw_materials.jsonl 重新提炼配方，重建知识库。

    用途：
    - 提炼 prompt 升级后，用旧素材重新跑一遍，无需再去知乎采集
    - 快速迭代知识库的结构和分类体系

    支持两种模式：
    - 全量重建（清空现有知识库，从头提炼）
    - 增量追加（保留现有知识库，只处理新素材）
    """

    print(f"""
    ╔══════════════════════════════════════════╗
    ║   🔄 从原始素材重建知识库                 ║
    ╚══════════════════════════════════════════╝
    """)

    if not os.path.exists(RAW_MATERIALS_FILE):
        print(f"  ❌ 未找到素材存档：{RAW_MATERIALS_FILE}")
        print(f"  请先运行 --cold-start 或正常写作流程采集素材")
        return

    # 读取素材
    articles = []
    try:
        with open(RAW_MATERIALS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("title") and record.get("answer"):
                        articles.append({
                            "title": record["title"],
                            "answer": record["answer"],
                        })
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"  读取素材失败：{e}")
        return

    if not articles:
        print("  素材存档为空")
        return

    # 去重（按标题）
    seen = set()
    unique = []
    for a in articles:
        key = a["title"].strip()
        if key not in seen:
            seen.add(key)
            unique.append(a)
    articles = unique

    kb = load_kb()
    current = len(kb.get("recipes", []))

    print(f"  素材存档：{len(articles)} 篇（去重后）")
    print(f"  当前知识库：{current} 条配方")

    # 选择模式
    print(f"\n  请选择重建模式：")
    print(f"    1. 全量重建（清空现有知识库，全部重新提炼）")
    print(f"    2. 增量追加（保留现有，只提炼新素材）")

    try:
        mode = input("\n  请选择（1/2，默认1）：").strip()
    except EOFError:
        mode = "1"

    if mode == "2":
        # 增量模式：过滤掉已经在知识库中有记录的素材
        existing_titles = set()
        for r in kb.get("recipes", []):
            t = r.get("source_title", "")
            if t:
                existing_titles.add(t[:50])  # source_title 存储时截断过

        new_articles = []
        for a in articles:
            if a["title"][:50] not in existing_titles:
                new_articles.append(a)

        if not new_articles:
            print(f"\n  所有素材都已提炼过，没有新素材")
            return

        print(f"\n  增量模式：{len(new_articles)} 篇新素材待提炼")
        articles = new_articles
    else:
        # 全量模式：清空知识库
        confirm = input(f"\n  ⚠ 将清空现有 {current} 条配方并重新提炼，确认？(y/n)：").strip().lower()
        if confirm != 'y':
            print("  已取消")
            return
        kb["recipes"] = []
        kb["stats"]["sources_analyzed"] = 0
        save_kb(kb)
        print(f"  已清空知识库")

    # 提炼
    print(f"\n  开始提炼 {len(articles)} 篇素材...")
    count = extract_and_store(articles)

    print(f"\n  ✓ 重建完成！新增 {count} 个配方")
    show_stats()

# ============================================================
# 统计与展示
# ============================================================

def show_stats():
    """显示知识库统计信息"""
    kb = load_kb()
    recipes = kb.get("recipes", [])

    print(f"\n  ╔══════════════════════════════════════╗")
    print(f"  ║   📚 知识库统计                       ║")
    print(f"  ╚══════════════════════════════════════╝")
    print(f"  总配方数：{len(recipes)}")
    print(f"  分析过的文章数：{kb['stats'].get('sources_analyzed', 0)}")
    print(f"  最后更新：{kb.get('last_updated', '从未')}")

    if not recipes:
        print(f"\n  知识库为空，请运行 --cold-start 初始化")
        return

    # 按题材统计
    genre_counts = {}
    for r in recipes:
        genre = r.get("genre", "未分类")
        genre_counts[genre] = genre_counts.get(genre, 0) + 1

    print(f"\n  题材分布：")
    for genre, count in sorted(genre_counts.items(), key=lambda x: -x[1]):
        bar = "█" * min(count, 30)
        print(f"    {genre:<12s} {count:>3d} 条 {bar}")

    # 评分统计
    scored = [r for r in recipes if r.get("avg_score") is not None]
    if scored:
        avg_all = sum(r["avg_score"] for r in scored) / len(scored)
        print(f"\n  已评分配方：{len(scored)}/{len(recipes)}")
        print(f"  全局平均分：{avg_all:.1f}")

    # 视角分布
    perspective_counts = {}
    for r in recipes:
        p = r.get("perspective", "未指定")
        perspective_counts[p] = perspective_counts.get(p, 0) + 1
    if perspective_counts:
        print(f"\n  叙事视角分布：")
        for p, count in sorted(perspective_counts.items(), key=lambda x: -x[1]):
            pct = count * 100 // len(recipes)
            print(f"    {p:<20s} {count:>3d} 条 ({pct}%)")

    # 基调分布
    tone_counts = {}
    for r in recipes:
        t = r.get("tone", "未指定")
        tone_counts[t] = tone_counts.get(t, 0) + 1
    if tone_counts:
        print(f"\n  情感基调分布：")
        for t, count in sorted(tone_counts.items(), key=lambda x: -x[1])[:10]:
            pct = count * 100 // len(recipes)
            print(f"    {t:<16s} {count:>3d} 条 ({pct}%)")

def show_ranking():
    """显示配方评分排行"""
    kb = load_kb()
    scored = [r for r in kb.get("recipes", []) if r.get("avg_score") is not None]

    if not scored:
        print("\n  还没有任何配方被评分过。运行一轮写作流程后再来看。")
        return

    scored.sort(key=lambda r: r["avg_score"], reverse=True)

    print(f"\n  ╔══════════════════════════════════════╗")
    print(f"  ║   🏆 配方评分排行                     ║")
    print(f"  ╚══════════════════════════════════════╝")

    print(f"\n  Top 10：")
    for i, r in enumerate(scored[:10]):
        print(f"  {i+1:>2}. [{r.get('genre','')}] 均分 {r['avg_score']:.1f} "
              f"（{r.get('score_count',0)} 次） {r.get('hook','')[:30]}...")

    if len(scored) > 3:
        print(f"\n  Bottom 3：")
        for r in scored[-3:]:
            print(f"      [{r.get('genre','')}] 均分 {r['avg_score']:.1f} "
                  f"（{r.get('score_count',0)} 次） {r.get('hook','')[:30]}...")

def show_genre(genre_name):
    """展示指定题材下的所有配方"""
    kb = load_kb()
    recipes = [r for r in kb.get("recipes", []) if r.get("genre") == genre_name]

    if not recipes:
        print(f"\n  「{genre_name}」下没有配方")
        return

    print(f"\n  「{genre_name}」共 {len(recipes)} 个配方：\n")
    for i, r in enumerate(recipes):
        score_str = f"均分 {r['avg_score']:.1f}" if r.get("avg_score") else "未评分"
        print(f"  {i + 1}. [{r.get('id', '?')}] {score_str}")
        print(f"     视角：{r.get('perspective', '未指定')}")
        print(f"     基调：{r.get('tone', '未指定')}")
        print(f"     开头：{r.get('hook', '-')}")
        print(f"     冲突：{r.get('conflict', '-')}")
        print(f"     节奏：{r.get('pacing', '-')}")
        print(f"     文风：{r.get('style', '-')}")
        print(f"     人设：{r.get('character', '-')}")
        print(f"     来源：{r.get('source_title', '-')}  使用：{r.get('times_used', 0)} 次")
        print()

# ============================================================
# CLI 入口
# ============================================================

def main():
    import sys

    if len(sys.argv) < 2:
        print("""
    用法：
      python kb_manager.py --stats           查看知识库统计
      python kb_manager.py --cold-start      自动采集文章并提炼配方
    python kb_manager.py --rebuild         从 data/raw_materials.jsonl 重建知识库
      python kb_manager.py --compress        压缩合并知识库
      python kb_manager.py --show <题材>     查看指定题材的配方
      python kb_manager.py --show-all        查看所有配方
      python kb_manager.py --ranking         查看配方评分排行
        """)
        return

    cmd = sys.argv[1]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler()]
    )

    if cmd == "--stats":
        show_stats()
    elif cmd == "--cold-start":
        cold_start()
    elif cmd == "--rebuild":
        rebuild_from_raw()
    elif cmd == "--compress":
        compress_kb()
    elif cmd == "--show" and len(sys.argv) > 2:
        show_genre(sys.argv[2])
    elif cmd == "--show-all":
        kb = load_kb()
        for genre in get_all_genres(kb):
            show_genre(genre)
    elif cmd == "--ranking":
        show_ranking()
    else:
        print(f"  未知命令：{cmd}")

if __name__ == "__main__":
    main()
