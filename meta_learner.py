# ============================================================
# meta_learner.py — 跨任务元知识自学习
#
# 核心理念：
#   每次批量任务结束后（阶段3评分发布完成），把【全部】配方 + 本轮评分
#   存入 pool_pending.jsonl。当池子累积到阈值（默认 20 条），
#   按评分降序取前 N%（默认 60%）作为蒸馏材料，和【旧心法】一起喂给 LLM
#   做有机融合，产出新版的《知乎作者创作手册》。
#
# 数据文件布局：
#   data/meta/
#     ├─ pool_pending.jsonl      待蒸馏池（每行一条 recipe 记录）
#     ├─ pool_consumed.jsonl     已蒸馏过的归档（只追加，不清空）
#     ├─ meta_knowledge.md       当前版本的心法手册
#     └─ history/
#        ├─ meta_knowledge_v1_20260419_143000.md
#        ├─ meta_knowledge_v2_20260422_091500.md
#        └─ ...
#
# 对外接口：
#   enqueue_full_batch(scored_materials) → 阶段3后调用，入池
#   check_and_distill()                  → 检查阈值，到了就蒸馏
#   load_meta_knowledge()                → 生成故事时调用，返回正文
#   get_pool_stats()                     → 观察用，返回池子状态
# ============================================================

import json
import os
import re
import time
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ============================================================
# 路径配置
# ============================================================

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
META_DIR = os.path.join(_PROJECT_ROOT, "data", "meta")
POOL_PENDING_FILE = os.path.join(META_DIR, "pool_pending.jsonl")
POOL_CONSUMED_FILE = os.path.join(META_DIR, "pool_consumed.jsonl")
META_KNOWLEDGE_FILE = os.path.join(META_DIR, "meta_knowledge.md")
HISTORY_DIR = os.path.join(META_DIR, "history")


def _ensure_dirs():
    """确保所有元学习相关目录存在"""
    os.makedirs(META_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)


# ============================================================
# JSONL 读写工具
# ============================================================

def _read_jsonl(path):
    """读一个 JSONL 文件，返回 list[dict]，文件不存在返回 []"""
    if not os.path.exists(path):
        return []
    items = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning(f"meta_learner: JSONL 行解析失败（跳过）：{e}")
    return items


def _append_jsonl(path, records):
    """把 records（list[dict]）追加到 JSONL 文件末尾"""
    _ensure_dirs()
    with open(path, 'a', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_jsonl(path, records):
    """把 records（list[dict]）写到 JSONL 文件（覆盖）"""
    _ensure_dirs()
    with open(path, 'w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ============================================================
# reader_score 计算
# ============================================================

def compute_reader_score(footer, now=None):
    """
    基于 footer 的真实读者互动 + 发表时间计算 reader_score。

    公式：
        raw   = Σ 权重_i × 互动数_i   （权重从 config 读）
        decay = (REF / max(age, REF)) ** EXPONENT  （age < REF 时 decay=1.0）
        reader_score = raw × decay

    参数：
        footer: dict，需包含 likes/comments/collects/hearts/publish_time
                任一关键字段缺失 → 返回 None
        now:    可选的"当前时间"，便于单测注入；不传则用 datetime.now()

    返回：
        float 保留 2 位小数；footer 不完整或时间解析失败时返回 None
    """
    if not footer:
        return None

    likes = footer.get('likes')
    comments = footer.get('comments')
    collects = footer.get('collects')
    hearts = footer.get('hearts')
    publish_time = footer.get('publish_time')

    # 互动四项任一缺失 → 无法计算
    if None in (likes, comments, collects, hearts):
        return None
    if not publish_time:
        return None

    try:
        from config import (
            READER_SCORE_W_LIKES, READER_SCORE_W_COMMENTS,
            READER_SCORE_W_COLLECTS, READER_SCORE_W_HEARTS,
            READER_SCORE_REF_AGE_DAYS, READER_SCORE_DECAY_EXPONENT,
        )
    except ImportError as e:
        log.warning(f"meta_learner: reader_score 参数缺失（{e}），无法计算")
        return None

    # 解析 publish_time 为 datetime
    try:
        if isinstance(publish_time, str):
            pub_dt = datetime.fromisoformat(publish_time)
        else:
            pub_dt = publish_time
    except (ValueError, TypeError) as e:
        log.warning(f"meta_learner: publish_time 解析失败 '{publish_time}': {e}")
        return None

    cur = now or datetime.now()
    age_days = max(0, (cur - pub_dt).days)

    raw = (
        READER_SCORE_W_LIKES    * float(likes)
        + READER_SCORE_W_COMMENTS * float(comments)
        + READER_SCORE_W_COLLECTS * float(collects)
        + READER_SCORE_W_HEARTS   * float(hearts)
    )

    ref = READER_SCORE_REF_AGE_DAYS
    effective_age = max(age_days, ref)
    decay = (ref / effective_age) ** READER_SCORE_DECAY_EXPONENT

    return round(raw * decay, 2)


# ============================================================
# 入池：阶段3后调用
# ============================================================

def enqueue_full_batch(scored_materials):
    """
    把本轮 material 按"有 reader_score 才入池"的策略写入 pool_pending。

    策略：
      - 有 footer 且能算出 reader_score → 入池（排序键）
      - footer 采集失败或 reader_score 无法计算 → 跳过（故事照常生成，
        只是这一条不参与元学习蒸馏）
      - llm_score 仍保留在 rec 里作诊断字段，用来观察 LLM 评分偏差

    同时，通过 kb_manager.update_recipe_scores() 把 llm_score 回写到
    knowledge_base.json 对应的 recipe（保持现有 kb 学习通路不变）。

    参数：
        scored_materials: list[dict]，来自 score_stories 的返回值
                         每项需包含：recipe / score (llm) / title / footer
                         footer 从 collect_materials_batch 一路透传下来

    返回：
        实际入池的条数
    """
    if not scored_materials:
        log.info("meta_learner: 无素材可入池")
        return 0

    _ensure_dirs()

    now = datetime.now().isoformat(timespec='seconds')
    records = []
    score_map = {}  # 给 kb_manager.update_recipe_scores 用
    skipped_no_signal = 0   # 无 footer / reader_score 无法算 → 跳过入池
    skipped_no_recipe = 0   # 无 recipe

    for m in scored_materials:
        recipe = m.get("recipe")
        llm_score = m.get("score")
        footer = m.get("footer")

        if not recipe:
            skipped_no_recipe += 1
            continue

        # 即使没 reader_score，llm_score 有效也依然回写 kb（保持旧通路）
        if isinstance(llm_score, (int, float)) and llm_score > 0:
            rid = recipe.get("id")
            if rid:
                score_map[rid] = llm_score

        # 计算 reader_score
        reader_score = compute_reader_score(footer) if footer else None

        if reader_score is None:
            skipped_no_signal += 1
            log.info(f"  [META] 跳过入池（无读者信号）："
                     f"{m.get('title', '')[:40]}")
            continue

        # 组装池记录
        llm_score_val = (float(llm_score)
                         if isinstance(llm_score, (int, float)) and llm_score > 0
                         else None)
        rec = {
            "recipe": recipe,
            "reader_score": reader_score,
            "llm_score": llm_score_val,      # 诊断用，不参与排序
            "source_title": m.get("title", "")[:80],
            "recipe_id": recipe.get("id"),
            "genre": recipe.get("genre", "未分类"),
            "footer": footer,                 # 原始互动数据
            "publish_time": footer.get("publish_time"),
            "enqueued_at": now,
        }
        records.append(rec)

    if not records:
        log.info(f"meta_learner: 本轮无合格记录入池"
                 f"（无 recipe: {skipped_no_recipe}，无读者信号: {skipped_no_signal}）")
        # 即便不入池，kb 回写通路仍然执行
        if score_map:
            try:
                from kb_manager import update_recipe_scores
                update_recipe_scores(score_map)
            except Exception as e:
                log.warning(f"meta_learner: 回写评分到 kb 失败（忽略）：{e}")
        return 0

    _append_jsonl(POOL_PENDING_FILE, records)
    log.info(f"meta_learner: 入池 {len(records)} 条（累计 "
             f"{_count_pool_pending()} 条）；"
             f"本轮跳过 无读者信号 {skipped_no_signal} 条"
             f"{('、无 recipe ' + str(skipped_no_recipe) + ' 条') if skipped_no_recipe else ''}")

    # llm_score 回写 kb（和旧逻辑一致）
    if score_map:
        try:
            from kb_manager import update_recipe_scores
            update_recipe_scores(score_map)
        except Exception as e:
            log.warning(f"meta_learner: 回写评分到 kb 失败（忽略）：{e}")

    return len(records)


def _count_pool_pending():
    """池子里当前条目数"""
    if not os.path.exists(POOL_PENDING_FILE):
        return 0
    with open(POOL_PENDING_FILE, 'r', encoding='utf-8') as f:
        return sum(1 for line in f if line.strip())


# ============================================================
# 蒸馏：池子达到阈值时触发
# ============================================================

def check_and_distill():
    """
    检查 pool_pending 是否达到阈值。到了就触发蒸馏。

    返回：
        True  成功触发并完成蒸馏
        False 未达阈值 或 未启用 或 蒸馏失败
    """
    try:
        from config import (
            META_LEARN_ENABLE, META_DISTILL_THRESHOLD
        )
    except ImportError:
        log.warning("meta_learner: config 中缺少元学习参数，跳过")
        return False

    if not META_LEARN_ENABLE:
        return False

    pending = _read_jsonl(POOL_PENDING_FILE)
    if len(pending) < META_DISTILL_THRESHOLD:
        log.info(f"meta_learner: 池子 {len(pending)}/{META_DISTILL_THRESHOLD}，"
                 f"未达蒸馏阈值")
        return False

    log.info(f"meta_learner: 池子 {len(pending)} 条 ≥ 阈值 "
             f"{META_DISTILL_THRESHOLD}，开始蒸馏")
    return _do_distill(pending)


def _do_distill(pending):
    """
    执行蒸馏：按评分降序取前 N%，调用 LLM 合并心法。

    流程：
      1. 按 score 降序排，取前 N%
      2. 读旧 meta_knowledge（若存在）
      3. 备份旧版到 history/
      4. 构造 prompt 调用 LLM
      5. 写入新 meta_knowledge.md
      6. 把整个 pending 池追加到 consumed 并清空 pending
    """
    try:
        from config import (
            META_DISTILL_PROMPT, META_DISTILL_TOP_RATIO,
            META_HIGH_SCORE_THRESHOLD
        )
    except ImportError as e:
        log.error(f"meta_learner: 缺少 prompt/参数配置：{e}")
        return False

    # ---- 1. 按 reader_score 降序取前 N% ----
    # 排序键：reader_score（真实读者信号，替代旧的 llm_score）
    # 极少数历史遗留记录可能没有 reader_score（老版本池中的旧数据）——
    # 这些记录排序时视为 0，自然沉底；不影响新逻辑推进。
    sorted_pool = sorted(
        pending,
        key=lambda x: x.get("reader_score") if x.get("reader_score") is not None else 0,
        reverse=True
    )
    top_count = max(1, int(len(sorted_pool) * META_DISTILL_TOP_RATIO))
    top_pool = sorted_pool[:top_count]
    log.info(f"  按 reader_score 降序取前 {top_count}/{len(sorted_pool)} 条做蒸馏材料")

    # 诊断：reader_score vs llm_score 偏差（观察 LLM 评分器系统性偏差）
    _log_score_correlation(pending)

    # ---- 2. 读旧 meta ----
    current_meta = ""
    if os.path.exists(META_KNOWLEDGE_FILE):
        with open(META_KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            current_meta = f.read().strip()

    # 新版本号：当前 meta 的 version + 1（没有 meta 时从 1 开始）
    current_meta_version = 0
    if current_meta:
        md = get_meta_metadata()
        v = md.get("version")
        if isinstance(v, int) and v > 0:
            current_meta_version = v
    new_version = current_meta_version + 1
    total_distills = new_version  # 这是第 new_version 次蒸馏

    # ---- 3. 备份旧版 ----
    if current_meta:
        # 备份文件名用【旧版本号】+ 时间戳
        backup_path = _backup_current_meta(current_meta_version)
        log.info(f"  已备份旧版到 {os.path.basename(backup_path)}")

    # ---- 4. 构造 prompt ----
    recipe_pool_str = _format_pool_for_prompt(top_pool)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt = META_DISTILL_PROMPT.format(
        high_score_threshold=META_HIGH_SCORE_THRESHOLD,
        new_version=new_version,
        timestamp=timestamp,
        total_distills=new_version,
        source_pool_size=len(top_pool),
        recipe_count=len(top_pool),
        current_meta=current_meta or "（空——这是这本手册的初稿）",
        recipe_pool=recipe_pool_str,
    )

    # ---- 5. 调用 LLM ----
    log.info("  开始蒸馏（LLM 调用中，约需 30-60 秒）...")
    start_time = time.time()

    new_meta = _call_llm_for_distill(prompt)

    elapsed = time.time() - start_time
    if not new_meta:
        log.error(f"  ✗ 蒸馏失败（LLM 未返回内容），耗时 {elapsed:.1f}s")
        return False

    log.info(f"  蒸馏完成（耗时 {elapsed:.1f}s，"
             f"生成 {len(new_meta)} 字符）")

    # ---- 6. 写入 + 归档 ----
    _ensure_dirs()
    with open(META_KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
        f.write(new_meta)

    # 把【整个 pending 池】（不只是 top_pool）归档到 consumed
    _append_jsonl(POOL_CONSUMED_FILE, pending)
    # 清空 pending
    _write_jsonl(POOL_PENDING_FILE, [])

    log.info(f"  ✓ 元知识已更新 → {META_KNOWLEDGE_FILE}")
    log.info(f"  ✓ 池子已归档（{len(pending)} 条 → consumed，pending 清空）")
    return True


def _format_pool_for_prompt(top_pool):
    """
    把 top_pool 格式化为喂给 LLM 的文本。

    每条配方的标签从【LLM 评分 X/60】改成【真实读者数据】——
    包含 reader_score（排序依据）以及原始的 赞同/评论/收藏/喜欢 明细，
    让 LLM 看到具体的互动分布，更好判断配方的真实吸引力方向。
    """
    lines = []
    for i, rec in enumerate(top_pool, 1):
        recipe = rec.get("recipe", {})
        reader_score = rec.get("reader_score", 0)
        footer = rec.get("footer") or {}
        genre = recipe.get("genre", "未分类")

        # 互动明细（缺失时显示 -，避免 0 看起来像"零人点赞"的误导）
        def _fmt(v):
            return str(v) if isinstance(v, (int, float)) else "-"
        likes    = _fmt(footer.get("likes"))
        comments = _fmt(footer.get("comments"))
        collects = _fmt(footer.get("collects"))
        hearts   = _fmt(footer.get("hearts"))

        lines.append(
            f"=== 配方 {i} [读者评分 {reader_score}｜"
            f"赞同 {likes}｜评论 {comments}｜收藏 {collects}｜喜欢 {hearts}｜"
            f"题材: {genre}] ==="
        )
        for key in ["hook", "conflict", "pacing", "style",
                    "character", "perspective", "tone"]:
            val = recipe.get(key, "").strip()
            if val:
                lines.append(f"【{key}】{val}")
        lines.append("")  # 空行分隔
    return "\n".join(lines)


def _log_score_correlation(pool):
    """
    诊断 reader_score 和 llm_score 的系统偏差。

    产出三个指标：
      - Pearson 相关系数：两套评分的线性相关程度。越接近 1 越一致，
        越接近 0 说明 LLM 评分和读者反馈几乎无关。
      - Top-K 重合度：两套评分各自排出的前 K 名中有多少条是重合的。
        低重合度意味着"LLM 觉得好的"和"读者觉得好的"是两批配方。
      - 均值/标准差：用于看两套评分的分布差异。

    样本不足（<3）时跳过诊断（避免统计无意义）。
    只打印日志，不返回值——纯观察工具。
    """
    paired = [
        (r.get("reader_score"), r.get("llm_score"))
        for r in pool
        if r.get("reader_score") is not None
        and r.get("llm_score") is not None
    ]
    if len(paired) < 3:
        log.info(f"  [偏差诊断] 成对评分样本过少（{len(paired)}），跳过")
        return

    n = len(paired)
    rs = [p[0] for p in paired]
    ls = [p[1] for p in paired]

    mean_r = sum(rs) / n
    mean_l = sum(ls) / n
    var_r = sum((v - mean_r) ** 2 for v in rs) / n
    var_l = sum((v - mean_l) ** 2 for v in ls) / n

    # Pearson
    num = sum((r - mean_r) * (l - mean_l) for r, l in paired)
    denom = (var_r * var_l) ** 0.5 * n
    corr = num / denom if denom > 0 else 0.0

    # Top-K 重合度（K 取 n 的 1/3 左右，至少 3，至多 10）
    top_k = max(3, min(10, n // 3 + 1))
    idx_by_reader = sorted(range(n), key=lambda i: rs[i], reverse=True)[:top_k]
    idx_by_llm    = sorted(range(n), key=lambda i: ls[i], reverse=True)[:top_k]
    overlap = len(set(idx_by_reader) & set(idx_by_llm))

    log.info(f"  [偏差诊断] reader_score vs llm_score（n={n}）:")
    log.info(f"    Pearson 相关系数：{corr:+.3f}"
             f"（越接近 1 说明两套评分越一致；接近 0 说明 LLM 偏差大）")
    log.info(f"    Top-{top_k} 重合度：{overlap}/{top_k}"
             f"（越低说明'LLM 认为好'和'读者认为好'越不一致）")
    log.info(f"    reader_score：均值 {mean_r:.1f} ± {var_r**0.5:.1f}")
    log.info(f"    llm_score   ：均值 {mean_l:.1f} ± {var_l**0.5:.1f}")


def _call_llm_for_distill(prompt):
    """
    调用 LLM 做蒸馏。

    复用 kb_manager 的 _call_llm（非流式、适合长输出）。
    元知识通常几千字，max_tokens 给 8000 留余量。
    """
    try:
        from kb_manager import _call_llm
    except ImportError as e:
        log.error(f"meta_learner: 无法导入 kb_manager._call_llm：{e}")
        return None

    # temperature 低一些，让蒸馏输出稳定
    reply = _call_llm(prompt, max_tokens=24000, temperature=0.4)
    if not reply:
        return None

    # LLM 可能意外附带 ```markdown / ``` 包裹，剥掉
    cleaned = reply.strip()
    if cleaned.startswith("```"):
        # 跳过第一行的 ```markdown 或 ```
        parts = cleaned.split("\n", 1)
        cleaned = parts[1] if len(parts) > 1 else ""
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()

    return cleaned


# ============================================================
# 历史版本管理
# ============================================================

def _count_history_files():
    """统计 history/ 里的历史版本数（= 以往成功蒸馏次数）"""
    if not os.path.exists(HISTORY_DIR):
        return 0
    return sum(1 for f in os.listdir(HISTORY_DIR)
               if f.startswith("meta_knowledge_v") and f.endswith(".md"))


def _backup_current_meta(old_version):
    """
    把当前 meta_knowledge.md 备份到 history/，
    文件名用【旧版本号】 + 时间戳命名。

    例：当前 meta_knowledge.md 是 v2，即将被 v3 覆盖，
    则本次备份文件名是 meta_knowledge_v2_时间戳.md。

    参数：
        old_version: 当前 meta_knowledge.md 的版本号（非负整数；0 表示元数据缺失）

    返回：备份文件的绝对路径
    """
    _ensure_dirs()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"meta_knowledge_v{old_version}_{timestamp}.md"
    backup_path = os.path.join(HISTORY_DIR, filename)

    with open(META_KNOWLEDGE_FILE, 'r', encoding='utf-8') as src:
        content = src.read()
    with open(backup_path, 'w', encoding='utf-8') as dst:
        dst.write(content)

    return backup_path


# ============================================================
# 加载：给 build_story_prompt 用
# ============================================================

def load_meta_knowledge():
    """
    加载当前的元知识文档，返回正文（剥除 YAML front matter 元数据块）。

    返回：
        str  正文内容（已剥元数据头），供注入到 prompt
        ""   文件不存在或被手动清空
    """
    if not os.path.exists(META_KNOWLEDGE_FILE):
        return ""

    with open(META_KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
        raw = f.read()

    if not raw.strip():
        return ""

    # 剥 YAML front matter: 文件以 --- 开头，再遇到 --- 结束
    body = raw
    lines = raw.split("\n")
    if lines and lines[0].strip() == "---":
        # 找第二个 ---
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is not None:
            body = "\n".join(lines[end_idx + 1:]).lstrip("\n")

    return body.strip()


# ============================================================
# 分层检索：按 recipe 从元知识中提取最相关小节
# ============================================================

def _parse_meta_sections(meta_body):
    """
    解析 meta_body，提取所有 ### 小节及其父 ## 章节上下文。

    返回：
        [{parent, title, content}, ...]
        parent: 所属 ## 章节标题（如 "开篇与钩子"）
        title:  ### 小节标题（如 "反套路与认知错位"）
        content: 小节正文（不含标题行）
    """
    if not meta_body or not meta_body.strip():
        return []

    lines = meta_body.split("\n")
    sections = []
    current_parent = ""
    current_title = ""
    current_lines = []

    def _flush():
        if current_title and current_lines:
            body = "\n".join(current_lines).strip()
            if len(body) > 30:  # 过滤无实质内容的空节
                sections.append({
                    "parent": current_parent,
                    "title": current_title,
                    "content": body,
                })

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            _flush()
            current_parent = stripped[3:].strip()
            current_title = ""
            current_lines = []
        elif stripped.startswith("### "):
            _flush()
            current_title = stripped[4:].strip()
            current_lines = []
        elif current_title:
            current_lines.append(line)

    _flush()  # 最后一个小节
    return sections


def _score_section(section, recipe):
    """
    对单个 meta 小节与 recipe 的相关性打分。

    三维评分：
    1. 维度映射（+3）：节所属 ## 父章节与 recipe 维度的直接对应
    2. N-gram 重叠（每命中 +1，上限 +10）：字段值中的 2-gram/3-gram
       在节标题+正文中出现的次数
    3. 题材命中（+2）：节文本包含 recipe.genre 的关键词
    """
    CATEGORY_MAP = {
        "hook":      "开篇与钩子",
        "conflict":  "核心冲突",
        "pacing":    "叙事节奏",
        "style":     "风格与文笔",
        "character": "角色设定",
    }

    # 题材关键词扩展（处理 meta 中的简称/别称）
    GENRE_KEYWORDS = {
        "古代言情":   ["古代", "古言", "古风"],
        "现代言情":   ["现代", "现言", "都市"],
        "甜宠文":    ["甜宠", "甜文", "治愈"],
        "虐文":      ["虐文", "虐心", "催泪"],
        "穿书重生":   ["穿书", "重生", "觉醒", "系统"],
        "悬疑脑洞":   ["悬疑", "脑洞", "恐怖", "规则", "怪谈"],
        "耽美":      ["耽美", "双男主"],
        "校园青春":   ["校园", "青春"],
        "奇幻仙侠":   ["修仙", "仙侠", "奇幻", "异能"],
    }

    score = 0.0
    parent = section.get("parent", "")
    title = section.get("title", "")
    text = title + "\n" + section.get("content", "")

    # ---- 1. 维度映射（+3） ----
    DIM_SCORE = 3
    for dim_key, cat_name in CATEGORY_MAP.items():
        field_val = recipe.get(dim_key, "")
        if field_val and cat_name in parent:
            score += DIM_SCORE
            break  # 一个节只属于一个父章节，无需重复加

    # ---- 2. N-gram 重叠（每命中 +1，上限 +10） ----
    NGRAM_CAP = 10
    ngram_hits = set()

    for field_name in ["genre", "hook", "conflict", "pacing",
                       "style", "character", "perspective", "tone"]:
        val = recipe.get(field_name, "")
        if not val:
            continue
        # 提取所有 2-gram 和 3-gram
        chars = val.replace(" ", "").replace("\n", "")
        for n in (2, 3):
            for i in range(len(chars) - n + 1):
                gram = chars[i:i + n]
                if gram in text and gram not in ngram_hits:
                    ngram_hits.add(gram)

    score += min(len(ngram_hits), NGRAM_CAP)

    # ---- 3. 题材命中（+2） ----
    GENRE_BONUS = 2
    genre = recipe.get("genre", "")
    if genre:
        keywords = GENRE_KEYWORDS.get(genre, [genre])
        for kw in keywords:
            if kw in text:
                score += GENRE_BONUS
                break

    return score


def retrieve_meta_sections(meta_body, recipe, top_k=3):
    """
    从元知识手册中检索与当前 recipe 最相关的 ### 小节。

    参数：
        meta_body: load_meta_knowledge() 返回的全文（已剥 YAML）
        recipe:    配方 dict（含 genre/hook/conflict/... 等 8 维字段）
        top_k:     返回最优的 K 个小节

    返回：
        拼接后的文本（含上下文头），供注入 prompt。
        无有效匹配或解析失败时返回 ""。
    """
    if not meta_body or not recipe:
        return ""

    sections = _parse_meta_sections(meta_body)
    if not sections:
        return ""

    # 评分 + 排序
    scored = []
    for sec in sections:
        s = _score_section(sec, recipe)
        if s > 0:
            scored.append((s, sec))

    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return ""

    # 取 top K
    top_sections = scored[:top_k]

    # 拼接
    parts = []
    for _, sec in top_sections:
        parts.append(
            f"## {sec['parent']}\n"
            f"### {sec['title']}\n"
            f"{sec['content']}"
        )

    return "\n\n".join(parts)


def get_meta_metadata():
    """
    解析当前 meta_knowledge.md 的 YAML front matter 元数据，
    返回 dict（键如 version / last_distilled / total_distills）。

    失败或文件不存在返回 {}.
    """
    if not os.path.exists(META_KNOWLEDGE_FILE):
        return {}

    with open(META_KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
        raw = f.read()

    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}

    # 收集 --- ... --- 之间的行
    meta_lines = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        meta_lines.append(line)

    # 简单 key: value 解析（不用 yaml 库）
    metadata = {}
    for line in meta_lines:
        m = re.match(r'^\s*([A-Za-z_][\w]*)\s*:\s*(.+?)\s*$', line)
        if m:
            key, val = m.group(1), m.group(2)
            # 尝试转为 int
            try:
                metadata[key] = int(val)
            except ValueError:
                metadata[key] = val
    return metadata


# ============================================================
# 观察 / 诊断
# ============================================================

def get_pool_stats():
    """返回池子和元知识的当前状态（用于日志展示和调试）"""
    pending_count = _count_pool_pending()
    try:
        from config import META_DISTILL_THRESHOLD
    except ImportError:
        META_DISTILL_THRESHOLD = "?"

    meta_exists = os.path.exists(META_KNOWLEDGE_FILE)
    history_count = _count_history_files()
    metadata = get_meta_metadata() if meta_exists else {}

    return {
        "pool_pending_count": pending_count,
        "distill_threshold": META_DISTILL_THRESHOLD,
        "meta_exists": meta_exists,
        "meta_version": metadata.get("version"),
        "meta_last_distilled": metadata.get("last_distilled"),
        "total_distills_history": history_count,
    }


def log_pool_stats():
    """把池子状态打印到日志（供 main/workflow 调用）"""
    s = get_pool_stats()
    if s["meta_exists"]:
        ver = s["meta_version"] or "?"
        last = s["meta_last_distilled"] or "?"
        log.info(f"meta_learner 状态：池子 {s['pool_pending_count']}/"
                 f"{s['distill_threshold']} | "
                 f"心法 v{ver}（上次蒸馏 {last}） | "
                 f"历史版本 {s['total_distills_history']} 个")
    else:
        log.info(f"meta_learner 状态：池子 {s['pool_pending_count']}/"
                 f"{s['distill_threshold']} | 心法尚未建立")
