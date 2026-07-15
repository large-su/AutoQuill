# ============================================================
# llm_api.py v2.1 - LLM API 调用模块
#
# 更新：
# - 支持流式输出（stream=True），生成内容实时打印到终端
# - clean_story_output() 清洗 LLM 废话前缀/后缀
# - filter_story_questions() 用 LLM 筛选故事领域问题
#
# 依赖：pip install requests
# ============================================================

import requests
import json
import re
import time
import math
import sys
import logging

log = logging.getLogger(__name__)

# ============================================================
# ★ 段落长度阈值（手动调节此值）
# ============================================================
# 超过此字数的段落被视为"长段落"
# 建议根据 plot_paragraph_distribution() 生成的分布图来调整
# 知乎故事风格参考：短句成段通常 15-40 字，对话+描写约 40-70 字
# 阈值设为 80 给对话+描写留足余量，避免正常叙事被误判为"文字墙"
PARA_LENGTH_THRESHOLD = 80


# ============================================================
# 段落长度分布分析 + 绘图
# ============================================================

def plot_paragraph_distribution(generated_materials, output_dir=None):
    """
    对所有已生成的故事做段落长度统计，绘制 KDE 分布图 + 输出统计信息到日志。

    参数：
        generated_materials: list of dict，每个 dict 包含 'story', 'title', 'index'
        output_dir: 图片保存目录，默认为脚本同级 output/

    返回：
        图片保存路径
    """
    import os
    import numpy as np

    if not generated_materials:
        log.warning("  没有可分析的故事")
        return None

    # --- 收集数据 ---
    all_stats = []
    for m in generated_materials:
        story = m.get('story', '')
        if not story:
            continue
        paras = [l for l in story.split('\n') if l.strip() and not l.strip().startswith('#')]
        lengths = [len(p.strip()) for p in paras]
        if not lengths:
            continue
        arr = np.array(lengths)
        stats = {
            'index': m.get('index', '?'),
            'title': m.get('title', '')[:15],
            'lengths': arr,
            'median': float(np.median(arr)),
            'mean': float(np.mean(arr)),
            'p90': float(np.percentile(arr, 90)),
            'max': float(np.max(arr)),
            'total_paras': len(arr),
            'over_threshold': int(np.sum(arr > PARA_LENGTH_THRESHOLD)),
            'over_ratio': float(np.mean(arr > PARA_LENGTH_THRESHOLD)),
        }
        all_stats.append(stats)

    if not all_stats:
        return None

    # --- 输出统计到日志 ---
    log.info(f"\n{'─'*50}")
    log.info(f"段落长度分析（阈值 {PARA_LENGTH_THRESHOLD} 字）")
    log.info(f"{'─'*50}")
    log.info(f"  {'#':>3s}  {'中位':>5s}  {'P90':>5s}  {'最长':>5s}  {'段落数':>5s}  {'超标':>4s}  {'占比':>5s}  标题")
    log.info(f"  {'---':>3s}  {'---':>5s}  {'---':>5s}  {'---':>5s}  {'---':>5s}  {'---':>4s}  {'---':>5s}  ---")

    for s in sorted(all_stats, key=lambda x: x['index']):
        flag = '✗' if s['over_ratio'] > 0.10 else ('△' if s['over_ratio'] > 0.05 else '✓')
        log.info(f"  {s['index']:>3d}  {s['median']:>5.0f}  {s['p90']:>5.0f}  {s['max']:>5.0f}  "
                 f"{s['total_paras']:>5d}  {s['over_threshold']:>4d}  {s['over_ratio']:>5.0%}  "
                 f"{flag} {s['title']}")

    # 汇总
    all_lengths = np.concatenate([s['lengths'] for s in all_stats])
    log.info(f"\n  全局统计（{len(all_stats)} 篇，共 {len(all_lengths)} 段）：")
    log.info(f"    中位数={np.median(all_lengths):.0f}  均值={np.mean(all_lengths):.0f}  "
             f"P75={np.percentile(all_lengths, 75):.0f}  P90={np.percentile(all_lengths, 90):.0f}  "
             f"P95={np.percentile(all_lengths, 95):.0f}  最大={np.max(all_lengths):.0f}")
    log.info(f"    超 {PARA_LENGTH_THRESHOLD} 字段落：{np.sum(all_lengths > PARA_LENGTH_THRESHOLD)}/{len(all_lengths)} "
             f"（{np.mean(all_lengths > PARA_LENGTH_THRESHOLD):.1%}）")

    # --- 绘图 ---
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator
    except ImportError:
        log.warning("  matplotlib 未安装，跳过绘图（pip install matplotlib）")
        return None

    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'Arial', 'Helvetica'],
        'font.size': 9,
        'axes.linewidth': 0.8,
        'axes.edgecolor': '#333333',
        'axes.labelcolor': '#333333',
        'xtick.color': '#333333',
        'ytick.color': '#333333',
        'figure.dpi': 150,
    })

    fig, (ax_main, ax_box) = plt.subplots(
        2, 1, figsize=(10, 7), height_ratios=[3, 1],
        gridspec_kw={'hspace': 0.25}
    )

    # -- 上图：KDE 分布 --
    colors = plt.cm.tab20(np.linspace(0, 1, len(all_stats)))

    for i, s in enumerate(sorted(all_stats, key=lambda x: x['index'])):
        lengths = s['lengths']
        # 手动 KDE（高斯核）
        x_grid = np.linspace(0, min(300, np.max(lengths) + 20), 200)
        bw = max(np.std(lengths) * 0.4, 3)  # 带宽
        kde = np.zeros_like(x_grid)
        for val in lengths:
            kde += np.exp(-0.5 * ((x_grid - val) / bw) ** 2)
        kde /= (len(lengths) * bw * np.sqrt(2 * np.pi))

        label = f"#{s['index']} (med={s['median']:.0f})"
        ax_main.plot(x_grid, kde, color=colors[i], alpha=0.7, linewidth=1.2, label=label)

    # 阈值线
    ax_main.axvline(x=PARA_LENGTH_THRESHOLD, color='#e74c3c', linestyle='--',
                     linewidth=1.5, alpha=0.8, label=f'阈值 {PARA_LENGTH_THRESHOLD} 字')

    ax_main.set_xlabel('段落长度（字符数）')
    ax_main.set_ylabel('密度')
    ax_main.set_title('各篇故事段落长度分布', fontsize=12, fontweight='bold', pad=10)
    ax_main.set_xlim(0, min(300, np.percentile(all_lengths, 99) + 20))
    ax_main.grid(True, alpha=0.3, linewidth=0.5)
    ax_main.legend(fontsize=7, loc='upper right', ncol=2,
                    framealpha=0.9, edgecolor='#cccccc')

    # -- 下图：箱线图 --
    box_data = [s['lengths'] for s in sorted(all_stats, key=lambda x: x['index'])]
    box_labels = [f"#{s['index']}" for s in sorted(all_stats, key=lambda x: x['index'])]

    try:
        bp = ax_box.boxplot(box_data, tick_labels=box_labels, vert=False, patch_artist=True,
                             showfliers=False, widths=0.6,
                             medianprops=dict(color='#e74c3c', linewidth=1.5),
                             boxprops=dict(linewidth=0.8))
    except TypeError:
        # matplotlib < 3.9 兼容
        bp = ax_box.boxplot(box_data, labels=box_labels, vert=False, patch_artist=True,
                             showfliers=False, widths=0.6,
                             medianprops=dict(color='#e74c3c', linewidth=1.5),
                             boxprops=dict(linewidth=0.8))

    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.5)

    ax_box.axvline(x=PARA_LENGTH_THRESHOLD, color='#e74c3c', linestyle='--',
                    linewidth=1.5, alpha=0.8)
    ax_box.set_xlabel('段落长度（字符数）')
    ax_box.set_xlim(ax_main.get_xlim())
    ax_box.grid(True, axis='x', alpha=0.3, linewidth=0.5)
    ax_box.set_title('各篇段落长度箱线图', fontsize=10, pad=5)

    plt.tight_layout()

    # 保存
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)

    from datetime import datetime
    filename = f"para_dist_{datetime.now():%Y%m%d_%H%M%S}.png"
    filepath = os.path.join(output_dir, filename)
    fig.savefig(filepath, bbox_inches='tight', facecolor='white')
    plt.close(fig)

    log.info(f"\n  分布图已保存：{filepath}")
    return filepath


# ============================================================
# LLM 输出清洗
# ============================================================

def clean_story_output(text):
    """
    清洗 LLM 生成的故事文本：
    1. 去除开头的废话（"收到""好的""以下是为您创作的故事"等）
    2. 去除结尾的废话（"希望您喜欢""如有修改需求"等）
    3. 去除 DeepSeek R1 的 <think> 标签
    """
    if not text:
        return text

    # 去除 <think>...</think> 标签
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

    lines = text.split('\n')

    # --- 去除开头废话 ---
    start_noise = [
        re.compile(r'^(收到|好的|明白|了解|没问题|OK|ok)[\s！!。.，,]*$', re.IGNORECASE),
        re.compile(r'^(以下是|下面是|接下来|我来|让我|现在开始|那么)'),
        re.compile(r'(为您|给您|为你|给你)(创作|撰写|编写|写作)'),
        re.compile(r'^(根据您|根据你|按照您|按照你)'),
        re.compile(r'^[-=*]{3,}$'),  # 分隔线
    ]

    start_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        is_noise = any(p.search(stripped) for p in start_noise)
        if is_noise:
            start_idx = i + 1
        else:
            break

    # --- 去除结尾废话 ---
    end_noise = [
        re.compile(r'(希望您|希望你|如果您|如果你).*(喜欢|满意|需要|修改)'),
        re.compile(r'(如有|如需|需要).*(修改|调整|意见|建议)'),
        re.compile(r'^[-=*]{3,}$'),
        re.compile(r'(以上就是|以上是|故事到此)'),
        re.compile(r'(期待您|欢迎).*(反馈|评论|点赞)'),
    ]

    end_idx = len(lines)
    for i in range(len(lines) - 1, start_idx - 1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        is_noise = any(p.search(stripped) for p in end_noise)
        if is_noise:
            end_idx = i
        else:
            break

    cleaned = '\n'.join(lines[start_idx:end_idx]).strip()
    if len(cleaned) < len(text) * 0.5 and len(text) > 200:
        log.warning("清洗后内容大幅缩短，使用原文")
        return text.strip()

    return cleaned


# ============================================================
# 格式后处理（生成后、检测前自动修复）
# ============================================================

def enforce_short_sentences(text):
    """
    状态机驱动的智能断句：仅在非嵌套区域内对句末标点插入换行。

    避免朴素正则"见到。！？就切"的问题——引号「」、括号（）、
    方括号【】、书名号《》『』内的标点不会被误切。

    算法：用一个栈追踪嵌套的成对标点。
    遇到开符号 → 压入对应的闭符号。
    遇到闭符号 → 若匹配栈顶则弹出。
    遇到句末标点（。！？）且栈为空 → 在此断句（插入 \\n\\n）。
    """
    if not text:
        return text

    PAIRS = {
        '「': '」',   # 「 → 」
        '（': '）',   # （ → ）
        '【': '】',   # 【 → 】
        '《': '》',   # 《 → 》
        '『': '』',   # 『 → 』
    }
    SENTENCE_ENDS = {'。', '！', '？'}  # 。！？

    result = []
    stack = []       # 期望的闭合符号栈
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # 开符号 → 进入嵌套
        if ch in PAIRS:
            stack.append(PAIRS[ch])
            result.append(ch)
            i += 1
            continue

        # 闭符号 → 若匹配栈顶则退出嵌套
        if stack and ch == stack[-1]:
            stack.pop()
            result.append(ch)
            i += 1
            continue

        # 句末标点且不在嵌套内 → 断句
        if ch in SENTENCE_ENDS and not stack:
            result.append(ch)
            # 检查后续是否已有换行
            j = i + 1
            if j < n:
                if text[j] == '\n':
                    # 已有换行，确保至少两个
                    newline_count = 0
                    while j < n and text[j] == '\n':
                        newline_count += 1
                        j += 1
                    if newline_count == 1:
                        result.append('\n')  # 补一个 → 双换行
                    # >=2 个换行时不追加
                else:
                    # 没有换行 → 插入双换行
                    result.append('\n\n')
            # 文本末尾的标点不需要追加任何东西
            i += 1
            continue

        result.append(ch)
        i += 1

    return ''.join(result)


def replace_em_dashes(text):
    """
    将非对话区域内的破折号 —— 替换为逗号。

    AI 生成的中文故事常常过度使用 —— 作为插入语连接符，
    这是最容易被识别的 AI 写作痕迹。对话「」内的 ——
    （如「等等——」表示打断）予以保留，其余全部替换为 ，。
    """
    if not text or '——' not in text:
        return text

    PAIRS = {
        '「': '」',
        '（': '）',
        '【': '】',
        '《': '》',
        '『': '』',
    }

    result = []
    stack = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch in PAIRS:
            stack.append(PAIRS[ch])
            result.append(ch)
            i += 1
            continue

        if stack and ch == stack[-1]:
            stack.pop()
            result.append(ch)
            i += 1
            continue

        # 破折号 ——（U+2014 U+2014，两个连续的 em dash）
        if ch == '—' and i + 1 < n and text[i + 1] == '—' and not stack:
            result.append('，')
            i += 2  # 跳过两个字符
            continue

        # 单个 —— 也处理（有些 AI 输出只用一个）
        if ch == '—' and i + 1 < n and text[i + 1] == '—' and stack:
            # 在对话内，保留
            result.append('——')
            i += 2
            continue

        result.append(ch)
        i += 1

    return ''.join(result)


def fix_story_format(text):
    """
    对 LLM 生成的故事做格式后处理，自动修复常见格式问题。

    修复项：
    1. 中文引号 "" "" → 「」
    2. 标题行检测与删除（第一行是 # 标题 或 **标题** → 移除）
    3. AI 废话前缀删除
    3.5 状态机智能断句：句末标点后插入换行（引号/括号内不受影响）
    3.6 破折号替换：非对话区域 —— → ，（去 AI 化）
    3.7 分割线清除：删除章节内的 --- / *** / ~~~ 等装饰性分隔线
    4. 孤立单换行 → 双换行
    5. 压缩多余空行

    返回修复后的文本。
    """
    if not text or not text.strip():
        return text

    # --- 1. 中文引号替换 ---
    # 成对替换：左引号→「，右引号→」
    text = text.replace('\u201c', '「').replace('\u201d', '」')
    text = text.replace('\u201e', '「').replace('\u201f', '」')
    # 半角双引号也替换（常出现在 AI 输出中）
    # 用状态机配对：奇数次出现的 " → 「，偶数次 → 」
    result_chars = []
    dq_count = 0
    for ch in text:
        if ch == '"':
            dq_count += 1
            result_chars.append('「' if dq_count % 2 == 1 else '」')
        else:
            result_chars.append(ch)
    text = ''.join(result_chars)

    # --- 2. 标题行检测与删除 ---
    # 检测第一非空行是否为标题（# 标题 / **标题**），是则删除整行
    lines = text.split('\n')
    first_non_empty_idx = None
    for i, line in enumerate(lines):
        if line.strip():
            first_non_empty_idx = i
            break
    if first_non_empty_idx is not None:
        first_line = lines[first_non_empty_idx].strip()
        # H1 标题：# 某某某（但排除空 # 和 ## **N** 章节标题）
        is_h1 = bool(re.match(r'^#\s+(?!\*\*\d+\*\*).+', first_line))
        # 纯加粗标题行：**某某某**（整行只有一组加粗）
        is_bold_title = bool(re.match(r'^\*\*[^*]+\*\*$', first_line))
        if is_h1 or is_bold_title:
            lines.pop(first_non_empty_idx)
            text = '\n'.join(lines).lstrip()

    # --- 3. AI 废话前缀删除 ---
    ai_prefixes = ['好的，', '好的!', '好的！', '收到，', '收到！', '明白，', '明白！',
                   '以下是', '根据您', '当然可以', '没问题',
                   '好的\n', '收到\n', '明白\n']
    stripped = text.lstrip()
    for prefix in ai_prefixes:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].lstrip()
            break
    text = stripped

    # --- 3.5 状态机智能断句 ---
    # 在引号/括号/书名号外的句号、问号、感叹号后强制插入双换行
    text = enforce_short_sentences(text)

    # --- 3.6 破折号替换 ---
    # AI 生成的故事经常过度使用 ——，这是最明显的 AI 写作痕迹之一。
    # 对话「」内的 ——（如「等等——」）保留，其余替换为逗号。
    text = replace_em_dashes(text)

    # --- 3.7 分割线清除 ---
    # 模型在章节内常插入装饰性分隔线，章节是连续叙事，不应有分割线
    # 匹配独立成行的：---  ***  ___  ~~~  ───  ……
    text = re.sub(r'^\s*[-*=_~─]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[.。…]{4,}\s*$', '', text, flags=re.MULTILINE)
    # 清除因此产生的连续空行（后面 step 5 会统一压缩）

    # --- 4. 孤立单\n → \n\n（知乎需要空行才能正确分段）---
    # (?<!\n)\n(?!\n) 匹配前后都不是\n的孤立换行符
    text = re.sub(r'(?<!\n)\n(?!\n)', '\n\n', text)

    # --- 5. 压缩3个以上连续换行为2个（清理多余空行）---
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# ============================================================
# 格式合规检测
# ============================================================

def validate_story_format(text):
    """
    对故事文本做格式合规检测，返回 (score, is_valid, details)。

    评分规则（基础分 10，及格线 >= 6）：
    1. 章节标题 ## **N** 至少 6 个，少 1 个减 1 分，封顶减 4 分
    2. 长段落检测（阈值=PARA_LENGTH_THRESHOLD 字）：
       >5% 减 2 分，>10% 减 3 分，>20% 减 5 分
    3. 对话引号：中文引号 "" "" 出现 >= 5 次减 5 分
    4. 字数：<4000 减 2 分；<2000 额外减 3 分
    5. AI 废话前缀：出现减 2 分
    """
    if not text or not text.strip():
        return 0, False, {"章节": -10, "字数": 0, "原因": "空文本"}

    score = 10
    details = {}

    # --- 1. 章节标题检测 ---
    chapter_count = len(re.findall(r'##\s*\*\*\d+\*\*', text))
    if chapter_count < 6:
        penalty = min(6 - chapter_count, 4)  # 封顶减 4 分
        score -= penalty
        details["章节"] = f"{chapter_count}个(-{penalty})"

    # --- 2. 长段落检测（三级分档，避免正常叙事被误杀）---
    paras = [l for l in text.split('\n') if l.strip() and not l.strip().startswith('#')]
    if paras:
        long_paras = sum(1 for p in paras if len(p.strip()) > PARA_LENGTH_THRESHOLD)
        ratio = long_paras / len(paras)
        if ratio > 0.20:
            score -= 5
            details["长段"] = f"{ratio:.0%}({long_paras}段>{PARA_LENGTH_THRESHOLD}字)(-5)"
        elif ratio > 0.10:
            score -= 3
            details["长段"] = f"{ratio:.0%}({long_paras}段>{PARA_LENGTH_THRESHOLD}字)(-3)"
        elif ratio > 0.05:
            score -= 2
            details["长段"] = f"{ratio:.0%}({long_paras}段>{PARA_LENGTH_THRESHOLD}字)(-2)"

    # --- 3. 对话引号检测 ---
    cn_quotes = len(re.findall(r'["\u201c\u201d\u201e\u201f]', text))
    if cn_quotes >= 5:
        score -= 5
        details["引号"] = f"{cn_quotes}次(-5)"

    # --- 4. 字数检测（门槛对齐 8节×500字=4000字）---
    char_count = len(text)
    if char_count < 4000:
        score -= 2
        details["字数"] = f"{char_count}字(-2)"
    if char_count < 2000:
        score -= 3
        details["字数"] = f"{char_count}字(-2-3)"

    # --- 5. AI 废话前缀 ---
    first_100 = text[:100]
    ai_prefixes = ['好的', '收到', '明白', '以下是', '根据您', '当然可以', '没问题']
    if any(p in first_100 for p in ai_prefixes):
        score -= 2
        details["废话"] = "-2"

    score = max(score, 0)
    is_valid = score >= 6

    log.info(f"  格式检测：{score}/10 {'✓合规' if is_valid else '✗不合规'}"
             f"{' (' + ', '.join(f'{k}:{v}' for k, v in details.items()) + ')' if details else ''}")

    return score, is_valid, details


# ============================================================
# Prompt 构建（三种素材模式统一入口）
# ============================================================

def _resolve_meta_content(meta_knowledge, recipe):
    """
    解析要注入的元知识内容。

    有 recipe 且检索开关开启时，调用分层检索取最相关小节；
    无 recipe 或检索关闭/失败时，返回全量 meta。

    返回：
        (meta_text, was_retrieved): 元知识文本 和 是否实际做了检索
    """
    if not meta_knowledge or not str(meta_knowledge).strip():
        return "", False
    if not recipe:
        return str(meta_knowledge).strip(), False

    try:
        from config import META_RETRIEVAL_ENABLE, META_RETRIEVAL_TOP_K
    except ImportError:
        return str(meta_knowledge).strip(), False

    if not META_RETRIEVAL_ENABLE:
        return str(meta_knowledge).strip(), False

    try:
        from meta_learner import retrieve_meta_sections
        retrieved = retrieve_meta_sections(
            meta_knowledge, recipe, top_k=META_RETRIEVAL_TOP_K
        )
        if retrieved and len(retrieved.strip()) > 50:
            log.info(
                f"  [元知识检索] 从 {len(str(meta_knowledge))} 字符中"
                f" 检索出 top-{META_RETRIEVAL_TOP_K} 相关小节"
                f"（{len(retrieved)} 字符）"
            )
            return retrieved, True
    except Exception as e:
        log.warning(f"  [元知识检索] 检索失败，回退全量注入：{e}")

    return str(meta_knowledge).strip(), False


def build_story_prompt(question_title, reference_answer=None, recipe=None,
                       meta_knowledge=None):
    """
    根据 STORY_MATERIAL_MODE 构建故事生成 prompt。

    三种模式：
      - "recipe"               配方驱动（从当前文章提炼配方，不附参考原文）
      - "reference"            参考文章模式（旧逻辑）
      - "recipe_and_reference" 配方 + 参考文章结合

    参数：
        question_title:    问题标题
        reference_answer:  参考回答文本
        recipe:            配方 dict（包含 hook/conflict/... 字段）
        meta_knowledge:    跨任务积累的元知识文本（可选）。
                          若 STORY_RECIPE_PROMPT 内含 {meta_knowledge} 占位符，
                          会直接填入；否则作为一个独立的"心法节"追加到 prompt 末尾。
                          建议只传经过 meta_learner.load_meta_knowledge()
                          处理后的正文（已剥除元数据块）。

    返回：(user_message, mode_str)
    """
    from config import STORY_SYSTEM_PROMPT

    try:
        from config import STORY_MATERIAL_MODE
    except ImportError:
        STORY_MATERIAL_MODE = "reference"

    # 预先格式化 meta 节（占位符注入 + 追加节 两种路径共用）
    _meta_text_for_placeholder = ""  # 填进 {meta_knowledge} 占位符的完整文本
    _meta_section_for_append = ""    # 用作追加节的完整文本

    # 分层检索（有 recipe 时取最相关小节，否则全量）
    _meta_content, _meta_retrieved = _resolve_meta_content(
        meta_knowledge, recipe
    )
    _has_meta = bool(_meta_content)

    if _has_meta:
        try:
            from config import META_STORY_INJECT_SECTION
            # 渲染好的完整心法节，含前导标题
            rendered_section = META_STORY_INJECT_SECTION.format(
                meta_knowledge=_meta_content
            )
            _meta_text_for_placeholder = rendered_section
            _meta_section_for_append = rendered_section
        except ImportError:
            # 兜底：META_STORY_INJECT_SECTION 未配置时，退回到裸文本
            _meta_text_for_placeholder = (
                "\n\n## 创作心法（来自跨篇作品的积累）\n\n"
                + str(_meta_content).strip() + "\n"
            )
            _meta_section_for_append = _meta_text_for_placeholder

    def _format_recipe(template, recipe, reference_section=""):
        """
        格式化 recipe 到 prompt。

        reference_section：
          - "recipe" 模式 → ""
          - "recipe_and_reference" 模式 → 参考文章指引块
        """
        return template.format(
            hook=recipe.get("hook", "自由发挥"),
            conflict=recipe.get("conflict", "自由发挥"),
            pacing=recipe.get("pacing", "自由发挥"),
            style=recipe.get("style", "自由发挥"),
            character=recipe.get("character", "自由发挥"),
            perspective=recipe.get("perspective", "不限"),
            tone=recipe.get("tone", "不限"),
            meta_knowledge=_meta_text_for_placeholder,
            reference_section=reference_section,
        )

    def _maybe_append_meta(prompt_body, template_source):
        """
        若 meta 存在且 template 中没有 {meta_knowledge} 占位符
        （说明 meta 没有被 _format_recipe 注入），则追加心法节到 prompt 末尾。

        返回：(最终 prompt, 是否实际注入了 meta)
        """
        if not _has_meta:
            return prompt_body, False
        # 检查原 template 是否含占位符
        if "{meta_knowledge}" in template_source:
            # 已在 _format_recipe 中填入，不再追加
            return prompt_body, True
        # 没占位符 → 追加
        return prompt_body + _meta_section_for_append, True

    # === 模式1：纯配方 ===
    if STORY_MATERIAL_MODE == "recipe" and recipe:
        from config import STORY_RECIPE_PROMPT
        recipe_prompt = _format_recipe(STORY_RECIPE_PROMPT, recipe,
                                       reference_section="")
        recipe_prompt, injected = _maybe_append_meta(
            recipe_prompt, STORY_RECIPE_PROMPT
        )
        user_message = f"{recipe_prompt}\n\n请为以下知乎问题创作一个全新的故事：\n\n{question_title}"
        meta_tag = " +心法" if injected else ""
        mode_str = f"配方模式{meta_tag} [{recipe.get('genre', '?')}] {recipe.get('perspective', '?')} hook={recipe.get('hook', '?')[:15]}"

    # === 模式2：配方 + 参考文章 ===
    elif STORY_MATERIAL_MODE == "recipe_and_reference" and recipe:
        from config import STORY_RECIPE_PROMPT
        ref_section = (
            "\n## 参考文章\n\n"
            "以下\"高赞文章\"仅供风格借鉴，感受其语感、节奏和氛围即可。\n"
            "注意：必须是全新构思的故事，情节设定必须完全避开参考文章！"
            "绝不允许搬运任何情节或角色！\n"
        )
        recipe_prompt = _format_recipe(STORY_RECIPE_PROMPT, recipe,
                                       reference_section=ref_section)
        recipe_prompt, injected = _maybe_append_meta(
            recipe_prompt, STORY_RECIPE_PROMPT
        )
        user_message = f"""{recipe_prompt}

以下是"全新文章主题"（知乎问题）：
{question_title}

以下是"高赞文章"（仅供风格借鉴）：
{reference_answer or '（无参考文章）'}

请根据以上创作指引和风格参考，创作一个全新的故事。"""
        meta_tag = " +心法" if injected else ""
        mode_str = (f"配方+参考{meta_tag} [{recipe.get('genre', '?')}] {recipe.get('perspective', '?')} "
                    f"hook={recipe.get('hook', '?')[:15]}（参考{len(reference_answer or '')}字）")

    # === 模式3：纯参考文章（旧逻辑 / 兜底） ===
    else:
        # 参考文章模式：STORY_SYSTEM_PROMPT 里没有 recipe 占位符，
        # 直接追加心法节即可
        system_body = STORY_SYSTEM_PROMPT
        injected = False
        if _has_meta:
            system_body = system_body + _meta_section_for_append
            injected = True
        user_message = f"""{system_body}

以下是"全新文章主题"（知乎问题）：
{question_title}

以下是"高赞文章"（参考风格）：
{reference_answer}

请根据以上内容，按照要求，开始创作全新的故事。"""
        meta_tag = " +心法" if injected else ""
        mode_str = f"参考文章模式{meta_tag}（{len(reference_answer or '')} 字符）"

    return user_message, mode_str


# ============================================================
# 长文模式 — Prompt 构建（盐选投稿专用）
# ============================================================



# ============================================================
# 通用流式 LLM 调用（长文模式 + 短文模式共用）
# ============================================================

def _call_llm_streaming(user_message, max_tokens, temperature=None,
                         on_chunk=None, label="LLM"):
    """
    通用的流式 chat.completions 调用。

    参数：
        user_message: 完整的用户消息文本
        max_tokens:   max_tokens 参数
        temperature:  温度（None 则用 config.LLM_API_TEMPERATURE）
        on_chunk:     可选回调 fn(content_chunk: str)。
                      若为 None：不打印不回调（静默累积）；
                      若为 sys.stdout.write：实时打印到终端。
        label:        日志标签

    返回：(full_content: str, elapsed: float, error: str or None)
    """
    from config import (
        LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL,
        LLM_API_TEMPERATURE, LLM_API_TIMEOUT,
        LLM_API_FREQUENCY_PENALTY, LLM_API_PRESENCE_PENALTY,
        LLM_API_EXTRA_BODY,
    )
    try:
        from config import (
            LLM_API_CONNECT_TIMEOUT, LLM_API_STREAM_READ_TIMEOUT,
            LLM_API_STREAM_FIRST_TOKEN_TIMEOUT,
            LLM_API_STREAM_IDLE_TIMEOUT,
        )
    except ImportError:
        LLM_API_CONNECT_TIMEOUT = 20
        LLM_API_STREAM_READ_TIMEOUT = LLM_API_TIMEOUT
        LLM_API_STREAM_FIRST_TOKEN_TIMEOUT = 45
        LLM_API_STREAM_IDLE_TIMEOUT = 60

    if not LLM_API_KEY:
        return "", 0.0, "API Key 未配置"

    url = f"{LLM_API_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": user_message}],
        "max_tokens": max_tokens,
        "temperature": temperature if temperature is not None else LLM_API_TEMPERATURE,
        "frequency_penalty": LLM_API_FREQUENCY_PENALTY,
        "presence_penalty": LLM_API_PRESENCE_PENALTY,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)

    start = time.time()
    full_content = ""
    last_usage = None
    response = None
    stream_stop = None
    stream_watchdog = None
    stream_state = {
        'first_content_at': None,
        'last_content_at': None,
        'timeout_reason': None,
    }

    try:
        response = requests.post(
            url, headers=headers, json=payload,
            timeout=(LLM_API_CONNECT_TIMEOUT, LLM_API_STREAM_READ_TIMEOUT),
            stream=True,
        )
        response.encoding = "utf-8"

        if response.status_code != 200:
            return full_content, time.time() - start, \
                f"HTTP {response.status_code}: {response.text[:300]}"

        # Socket read timeout cannot distinguish SSE heartbeats from real text.
        # Watch the arrival of actual content tokens in a separate timer.
        import threading
        stream_started = time.time()
        stream_stop = threading.Event()

        def _watch_stream_content():
            while not stream_stop.wait(0.5):
                now = time.time()
                first_content_at = stream_state['first_content_at']
                if first_content_at is None:
                    if now - stream_started < LLM_API_STREAM_FIRST_TOKEN_TIMEOUT:
                        continue
                    stream_state['timeout_reason'] = (
                        f"?????{LLM_API_STREAM_FIRST_TOKEN_TIMEOUT}s "
                        "????? token?"
                    )
                elif now - stream_state['last_content_at'] >= LLM_API_STREAM_IDLE_TIMEOUT:
                    stream_state['timeout_reason'] = (
                        f"???????{LLM_API_STREAM_IDLE_TIMEOUT}s "
                        "???? token?"
                    )
                else:
                    continue

                try:
                    response.close()
                except Exception:
                    pass
                return

        stream_watchdog = threading.Thread(
            target=_watch_stream_content,
            name=f"llm-stream-watchdog-{label}",
            daemon=True,
        )
        stream_watchdog.start()

        for line in response.iter_lines(decode_unicode=True):
            if stream_state['timeout_reason']:
                break
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                if "usage" in chunk and chunk.get("usage"):
                    last_usage = chunk["usage"]
                    continue
                content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if content:
                    now = time.time()
                    if stream_state['first_content_at'] is None:
                        stream_state['first_content_at'] = now
                    stream_state['last_content_at'] = now
                    full_content += content
                    if on_chunk:
                        try:
                            on_chunk(content)
                        except Exception:
                            pass
            except json.JSONDecodeError:
                continue

        if stream_state['timeout_reason']:
            return (
                full_content,
                time.time() - start,
                stream_state['timeout_reason'],
            )

        if last_usage:
            try:
                from llm_token_tracker import tracker
                tracker.report(LLM_API_MODEL, last_usage)
            except Exception:
                pass

        return full_content, time.time() - start, None

    except requests.exceptions.Timeout:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return (
            full_content,
            time.time() - start,
            f"Timeout?{LLM_API_STREAM_READ_TIMEOUT}s ?????"
        )
    except requests.exceptions.ConnectionError as e:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return full_content, time.time() - start, f"ConnectionError: {e}"
    except Exception as e:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return full_content, time.time() - start, f"Exception: {e}"
    finally:
        if stream_stop is not None:
            stream_stop.set()
        if stream_watchdog is not None:
            stream_watchdog.join(timeout=1)
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


# ============================================================
# 长文模式 — 章节完整性检测
# ============================================================

def _ensure_chapter_complete(text):
    """
    检测章节文本是否在完整句子处结束。

    如果末尾在句末标点（。！？……」》】）处结束 → 视为完整。
    如果末尾被截断（不在句末标点处）→ 回退到最后一个句末标点截断。

    返回：(text, is_complete)
      - is_complete=True:  文本末尾完整，无需处理
      - is_complete=False: 文本被截断，已回截到最后一个完整句
    """
    if not text or len(text) < 50:
        return text, len(text) >= 10

    last_50 = text[-50:]
    if re.search(r'[。！？……」》】]', last_50):
        return text, True

    # 被截断：回退到最后一个句末标点
    match = re.search(r'^(.*[。！？……」》】])[^。！？……」》】]*$', text, re.DOTALL)
    if match:
        truncated = match.group(1)
        return truncated, False

    # 全文找不到句末标点（极端情况），返回原文
    return text, False


def _normalize_chapter_headers(text, chapter_num):
    """
    归一化章节标题：清除模型自行生成的所有 ## **N** 变体，
    然后统一在章首补入 ## **{chapter_num}**。

    模型输出的标题格式极不稳定，常见变体：
      - ## **1**（标准）
      - ## **1** 标题文字
      - ## ** 1 **
      - **1**（缺 #）
      - 干脆不写

    本函数暴力清除后统一补入，确保每章标题格式一致。
    """
    # 清除所有 ## **N** 及其变体（含后面的标题文字）
    text = re.sub(r'#*\s*\*{1,2}\s*\d+\s*\*{1,2}[^\n]*\n?', '', text)
    # 清除单独的 **N** 行（无 # 前缀）
    text = re.sub(r'^\*{1,2}\s*\d+\s*\*{1,2}\s*\n', '', text, flags=re.MULTILINE)
    # 清除行首孤立的数字标题如 "1." "1、" "1）"（但排除正文中的数字列举）
    text = re.sub(r'^\s*\d+[\.、）\)]\s*\n', '', text, flags=re.MULTILINE)

    # 统一补入章节标题
    header = f"## **{chapter_num}**\n\n"
    text = header + text.strip()
    return text


# ============================================================
# 滚动大纲 — 新增函数（Foundation + Macro Beats + Arc 管理）
# ============================================================

def _build_recipe_context(recipe):
    """从 recipe dict 提取创作技法引用的格式化字符串。"""
    recipe = recipe or {}
    return {
        "hook": recipe.get("hook", "自由发挥"),
        "conflict": recipe.get("conflict", "自由发挥"),
        "pacing": recipe.get("pacing", "自由发挥"),
        "style": recipe.get("style", "自由发挥"),
        "character": recipe.get("character", "自由发挥"),
        "tone": recipe.get("tone", "自由发挥"),
        "perspective": recipe.get("perspective", "第一人称"),
    }


def _generate_foundation(question_title, recipe=None, stream_to_terminal=True):
    """
    阶段 -1：生成故事基石（人物、背景、风格、核心冲突）。

    只接收 question_title + recipe，不接收 reference_answer。
    """
    from config import FOUNDATION_PROMPT

    ctx = _build_recipe_context(recipe)
    prompt = FOUNDATION_PROMPT.format(
        question_title=question_title,
        hook=ctx["hook"],
        conflict=ctx["conflict"],
        pacing=ctx["pacing"],
        style=ctx["style"],
        character=ctx["character"],
        tone=ctx["tone"],
    )

    log.info("[Foundation] 开始生成故事基石...")
    on_chunk = None
    if stream_to_terminal:
        def on_chunk(c):
            sys.stdout.write(c)
            sys.stdout.flush()

    text, elapsed, error = _call_llm_streaming(
        prompt, max_tokens=2048, temperature=0.8,
        on_chunk=on_chunk, label="Foundation",
    )

    if stream_to_terminal and text:
        print()  # 流式输出后换行

    if error:
        log.error(f"[Foundation] 失败：{error}")
        if not text:
            return None
    log.info(f"[Foundation] 完成（{elapsed:.1f}s, {len(text) if text else 0} 字符）")
    return text


# ============================================================
# 长文模式 — 批量大纲生成
# ============================================================

def _generate_batch_outline(ws, recipe=None, stream_to_terminal=True):
    """
    基于 foundation + 上一批全文，生成下一批 N 章大纲。

    ws: StoryWorkspace 实例
    返回：大纲文本，失败返回 None
    """
    from config import BATCH_OUTLINE_PROMPT, LONG_FORM_OUTLINE_MAX_TOKENS, BATCH_CHAPTER_COUNT

    foundation = ws.foundation
    progress = ws.progress or {}
    last_written = progress.get("last_chapter_written", 0)
    total_chapters = progress.get("total_chapters", 20)

    start_chapter = last_written + 1
    remaining = total_chapters - last_written
    batch_chapter_count = min(BATCH_CHAPTER_COUNT, remaining)

    previous_text = ws.previous_batch_text(last_written)

    ctx = _build_recipe_context(recipe)
    prompt = BATCH_OUTLINE_PROMPT.format(
        foundation=foundation,
        previous_batch_text=previous_text,
        batch_chapter_count=batch_chapter_count,
        start_chapter=start_chapter,
        hook=ctx["hook"],
        conflict=ctx["conflict"],
        pacing=ctx["pacing"],
        style=ctx["style"],
        character=ctx["character"],
        tone=ctx["tone"],
    )

    end_chapter = start_chapter + batch_chapter_count - 1
    label = f"大纲({start_chapter}-{end_chapter})"
    log.info(f"[BatchOutline] 生成第{start_chapter}-{end_chapter}章大纲...")

    on_chunk = None
    if stream_to_terminal:
        def on_chunk(c):
            sys.stdout.write(c)
            sys.stdout.flush()

    text, elapsed, error = _call_llm_streaming(
        prompt, max_tokens=LONG_FORM_OUTLINE_MAX_TOKENS, temperature=0.7,
        on_chunk=on_chunk, label=label,
    )

    if stream_to_terminal and text:
        print()  # 流式输出后换行

    if error:
        log.error(f"[BatchOutline] 失败：{error}")
        if not text:
            return None

    ws.batch_outline = text
    log.info(f"[BatchOutline] 完成（{elapsed:.1f}s, {len(text)} 字符）")
    return text


# ============================================================
# 长文模式 — 批量章节生成
# ============================================================

def _generate_batch_chapters(ws, recipe=None, meta_knowledge=None,
                              stream_to_terminal=True):
    """
    一次性生成一批章节（N 章），返回合并文本。

    ws: StoryWorkspace 实例
    返回：合并的 N 章文本，失败返回 None
    """
    from config import BATCH_CHAPTERS_PROMPT, LONG_FORM_CHAPTER_MAX_TOKENS, BATCH_CHAPTER_COUNT

    foundation = ws.foundation
    if not foundation:
        log.error("[BatchChapters] foundation 不存在，无法生成章节")
        return None

    batch_outline = ws.batch_outline
    if not batch_outline:
        log.error("[BatchChapters] batch_outline 不存在，请先生成大纲")
        return None

    progress = ws.progress or {}
    last_written = progress.get("last_chapter_written", 0)
    total_chapters = progress.get("total_chapters", 20)

    start_chapter = last_written + 1
    remaining = total_chapters - last_written
    batch_count = min(BATCH_CHAPTER_COUNT, remaining)
    end_chapter = start_chapter + batch_count - 1

    previous_text = ws.previous_batch_text(last_written)

    ctx = _build_recipe_context(recipe)
    prompt = BATCH_CHAPTERS_PROMPT.format(
        foundation=foundation,
        batch_outline=batch_outline,
        previous_batch_text=previous_text,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        hook=ctx["hook"],
        conflict=ctx["conflict"],
        pacing=ctx["pacing"],
        style=ctx["style"],
        character=ctx["character"],
        tone=ctx["tone"],
        perspective=ctx["perspective"],
    )

    # 注入元知识
    if meta_knowledge:
        meta_content, _ = _resolve_meta_content(meta_knowledge, recipe)
        if meta_content:
            try:
                from config import META_STORY_INJECT_SECTION
                meta_section = META_STORY_INJECT_SECTION.format(
                    meta_knowledge=meta_content
                )
            except (ImportError, KeyError):
                meta_section = (
                    "\n\n## 创作心法（来自跨篇作品的积累）\n\n"
                    + str(meta_content).strip() + "\n"
                )
            prompt += meta_section

    label = f"第{start_chapter}-{end_chapter}章"
    log.info(f"[BatchChapters] 开始生成{label}...")

    on_chunk = None
    if stream_to_terminal:
        def on_chunk(c):
            sys.stdout.write(c)
            sys.stdout.flush()

    text, elapsed, error = _call_llm_streaming(
        prompt, max_tokens=LONG_FORM_CHAPTER_MAX_TOKENS,
        on_chunk=on_chunk, label=label,
    )

    if stream_to_terminal and text:
        print()  # 流式输出后换行

    if error:
        if not text:
            log.error(f"[BatchChapters] 失败：{error}")
            return None
        log.warning(f"[BatchChapters] 部分失败：{error}")

    log.info(f"[BatchChapters] 完成（{elapsed:.1f}s, {len(text)} 字符）")
    return text


# ============================================================
# 批量章节拆分
# ============================================================

def _split_batch_chapters(text):
    """
    按 ## **N** 分隔批量输出为 [(chapter_num: int, text: str), ...]。
    同时也支持 ## N. 和 ## 第N章 格式。
    """
    # 先按 ## **N** 拆分
    pattern = re.compile(
        r'(?:^|\n)(##\s*\*{1,2}\s*(\d+)\s*\*{1,2}[^\n]*)',
        re.MULTILINE
    )
    splits = list(pattern.finditer(text))

    if not splits:
        # 尝试 ## N. 格式
        pattern = re.compile(
            r'(?:^|\n)(##\s*(\d+)[\.、\s][^\n]*)',
            re.MULTILINE
        )
        splits = list(pattern.finditer(text))

    if not splits:
        # 尝试 ## 第N章 格式
        pattern = re.compile(
            r'(?:^|\n)(##\s*第\s*(\d+)\s*章[^\n]*)',
            re.MULTILINE
        )
        splits = list(pattern.finditer(text))

    if not splits:
        log.warning("[SplitChapters] 无法从批量输出中解析章节分隔符")
        return []

    # 第一个章节标题之前的文字视为引言，并入第一章
    intro_text = text[:splits[0].start()].strip()

    chapters = []
    for i, m in enumerate(splits):
        num = int(m.group(2))
        start = m.start()
        if i + 1 < len(splits):
            next_start = splits[i + 1].start()
            body = text[start:next_start].strip()
        else:
            body = text[start:].strip()

        if body.startswith('\n'):
            body = body[1:]

        # 引言并入第一章开头
        if i == 0 and intro_text:
            body = intro_text + "\n\n" + body

        chapters.append((num, body))

    return chapters


# ============================================================
# 长文模式主函数（大纲→批量写作交替流水线）
# ============================================================

def generate_long_form_story(question_title, recipe=None,
                              meta_knowledge=None, workspace=None):
    """
    长文模式（大纲→批量写作交替）：Foundation → 大纲⇄批量写作 循环。

    流水线：
      阶段 -1：生成故事基石（foundation）
      阶段 1-N：批量大纲 → 批量章节 → 批量大纲 → 批量章节 → ...
      阶段 N+1：拼接全文 + 全局格式修复 + 校验

    参数：
        workspace: 可选 StoryWorkspace 实例。用于 --resume 恢复，跳过 foundation
                   直接从批量循环继续。

    返回：完整故事文本，失败返回 None
    """
    from config import LONG_FORM_CHAPTER_COUNT, BATCH_CHAPTER_COUNT
    from core.story_workspace import StoryWorkspace

    # --resume: 使用已有 workspace 恢复
    if workspace is not None:
        ws = workspace
        resume = True
    else:
        ws = StoryWorkspace()
        resume = False

    total = LONG_FORM_CHAPTER_COUNT
    total_batches = (total + BATCH_CHAPTER_COUNT - 1) // BATCH_CHAPTER_COUNT

    if not resume:
        total_steps = 1 + total_batches * 2  # foundation + (大纲+正文)×批数
    else:
        p = ws.progress
        if not p:
            log.error("[长文模式-恢复] _progress.json 缺失或损坏，无法继续")
            return None
        remaining_chapters = total - p['last_chapter_written']
        remaining_batches = (remaining_chapters + BATCH_CHAPTER_COUNT - 1) // BATCH_CHAPTER_COUNT
        total_steps = remaining_batches * 2
        print(f"\n  ⏮ 恢复: {p['last_chapter_written']}/{total} 章已完成，"
              f"剩余 {remaining_chapters} 章（{remaining_batches} 批）\n")

    print(f"  ══ 长文模式 · {total} 章 · 每批 {BATCH_CHAPTER_COUNT} 章 · {total_batches} 批 ══")
    print(f"  Story ID: {ws.story_id}")
    print()

    step = 1

    # ================================================================
    # 阶段 -1：故事基石
    # ================================================================
    if not resume:
        if ws.progress is None:
            ws.progress = {
                'title': question_title[:50],
                'total_chapters': total,
                'last_chapter_written': 0,
                'status': 'in_progress',
            }

        print(f"  [{step}/{total_steps}] 生成故事基石...")
        foundation = _generate_foundation(question_title, recipe,
                                          stream_to_terminal=True)
        if not foundation:
            print(f"  ✗ 故事基石生成失败")
            log.error("[长文模式] 故事基石生成失败")
            return None
        ws.foundation = foundation
        chars = len(foundation)
        print(f"  [{step}/{total_steps}] 故事基石 ✓ ({chars} 字)")
        step += 1
    else:
        foundation = ws.foundation
        if not foundation:
            log.error("[长文模式-恢复] foundation.md 不存在或为空，无法继续")
            return None
        log.info(f"[长文模式-恢复] 跳过 Foundation，从第 {ws.progress['last_chapter_written'] + 1} 章继续")

    # ================================================================
    # 批量交替循环
    # ================================================================
    batch_num = (ws.progress['last_chapter_written'] // BATCH_CHAPTER_COUNT) + 1 if resume else 1

    while ws.progress['last_chapter_written'] < total:
        last_written = ws.progress['last_chapter_written']
        remaining = total - last_written
        batch_count = min(BATCH_CHAPTER_COUNT, remaining)
        start_ch = last_written + 1
        end_ch = start_ch + batch_count - 1

        # —— 大纲 ——
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章大纲...")
        batch_outline = _generate_batch_outline(ws, recipe, stream_to_terminal=True)
        if not batch_outline:
            print(f"  ✗ 大纲生成失败")
            log.error(f"[长文模式] 第{batch_num}批大纲生成失败")
            return None
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章大纲 ✓ "
              f"({len(batch_outline)} 字)")
        step += 1

        # —— 正文 ——
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章正文...")
        batch_text = _generate_batch_chapters(
            ws, recipe, meta_knowledge=meta_knowledge, stream_to_terminal=True
        )
        if not batch_text:
            print(f"  ✗ 章节生成失败")
            log.error(f"[长文模式] 第{batch_num}批章节生成失败")
            return None
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章正文 ✓ "
              f"({len(batch_text)} 字)")
        step += 1

        # 拆分章节
        chapters = _split_batch_chapters(batch_text)
        if not chapters:
            log.error(f"[长文模式] 无法从第{batch_num}批输出中拆分章节")
            return None

        # 逐章清洗、格式修复、归一化、保存
        ch_stats = []
        for ch_num, ch_text in chapters:
            ch_text = clean_story_output(ch_text)
            ch_text = fix_story_format(ch_text)
            ch_text, is_complete = _ensure_chapter_complete(ch_text)
            if not is_complete:
                log.warning(f"[长文-第{ch_num}章] 末尾不完整，已回截")
            ch_text = _normalize_chapter_headers(ch_text, ch_num)
            if len(ch_text) < 80:
                log.warning(f"[长文-第{ch_num}章] 内容过短（{len(ch_text)} 字）")
            elif not re.search(r'[。！？」』]', ch_text):
                log.warning(f"[长文-第{ch_num}章] 缺少句末标点，可能格式异常")
            ws.save_chapter(ch_num, ch_text)
            ch_stats.append(f"第{ch_num}章 {len(ch_text)}字")

        print(f"         {' · '.join(ch_stats)}")

        # 保存大纲快照
        ws.snapshot_outline(batch_num)

        # 更新进度
        last_ch = chapters[-1][0]
        ws.progress['last_chapter_written'] = last_ch
        batch_num += 1

    # ================================================================
    # 拼接 + 校验 + 导出
    # ================================================================
    print(f"\n  拼接全文 + 格式校验...")
    full_story = ws.assemble()
    full_story = fix_story_format(full_story)

    score, is_valid, details = validate_story_format(full_story)
    if not is_valid:
        log.warning(f"[长文模式] 全文格式校验不通过（{score}/10），"
                    f"详情：{details}")

    total_chars = len(full_story)
    ws.export_final()

    status = "✓" if is_valid else "⚠"
    print(f"\n  ══ {status} 完成！{total} 章 · {total_chars} 字 · 格式 {score}/10 ══")
    print(f"  输出目录: {ws._dir}")
    print()

    return full_story


# ============================================================
# 长文模式并行版本
# ============================================================

def generate_long_form_story_parallel(question_title, task_id,
                                       progress, recipe=None, meta_knowledge=None):
    """
    长文模式并行版本：用于批量并行生成场景。
    与 generate_long_form_story 的区别：不流式打印，通过 progress dict 报告状态。
    """
    from config import LONG_FORM_CHAPTER_COUNT, BATCH_CHAPTER_COUNT
    from core.story_workspace import StoryWorkspace

    short_title = question_title[:20] + "..." if len(question_title) > 20 else question_title

    progress[task_id] = {
        'status': '生成中·基石',
        'chars': 0,
        'elapsed': 0,
        'title': short_title,
    }

    start_total = time.time()
    ws = StoryWorkspace(task_id=task_id)

    # === 阶段 -1：生成故事基石（静默） ===
    foundation = _generate_foundation(question_title, recipe, stream_to_terminal=False)
    if not foundation:
        progress[task_id]['status'] = '❌ 基石失败'
        return None
    ws.foundation = foundation

    ws.progress = {
        'title': question_title[:50],
        'total_chapters': LONG_FORM_CHAPTER_COUNT,
        'last_chapter_written': 0,
        'status': 'in_progress',
    }

    # === 批量交替循环 ===
    total = LONG_FORM_CHAPTER_COUNT
    batch_num = 1
    total_chars = 0

    while ws.progress['last_chapter_written'] < total:
        last_written = ws.progress['last_chapter_written']
        remaining = total - last_written
        batch_count = min(BATCH_CHAPTER_COUNT, remaining)
        start_ch = last_written + 1
        end_ch = start_ch + batch_count - 1

        # 批量大纲
        progress[task_id]['status'] = f'生成中·大纲({start_ch}-{end_ch})'
        batch_outline = _generate_batch_outline(ws, recipe, stream_to_terminal=False)
        if not batch_outline:
            progress[task_id]['status'] = f'❌ 大纲失败'
            return None

        # 批量章节
        progress[task_id]['status'] = f'生成中·{start_ch}-{end_ch}章'
        batch_text = _generate_batch_chapters(
            ws, recipe, meta_knowledge=meta_knowledge, stream_to_terminal=False
        )
        if not batch_text:
            progress[task_id]['status'] = f'❌ 第{start_ch}-{end_ch}章失败'
            return None

        # 拆分 + 清洗 + 保存
        chapters = _split_batch_chapters(batch_text)
        if not chapters:
            progress[task_id]['status'] = f'❌ 拆分失败'
            return None

        for ch_num, ch_text in chapters:
            ch_text = clean_story_output(ch_text)
            ch_text = fix_story_format(ch_text)
            ch_text, is_complete = _ensure_chapter_complete(ch_text)
            if not is_complete:
                log.warning(f"[长文-并行-第{ch_num}章] 末尾不完整，已回截")
            ch_text = _normalize_chapter_headers(ch_text, ch_num)
            if len(ch_text) < 80:
                log.warning(f"[长文-并行-第{ch_num}章] 内容过短（{len(ch_text)} 字）")
            ws.save_chapter(ch_num, ch_text)
            total_chars += len(ch_text)

        ws.snapshot_outline(batch_num)
        last_ch = chapters[-1][0]
        ws.progress['last_chapter_written'] = last_ch
        batch_num += 1

        progress[task_id].update({
            'status': f'生成中·{last_ch}/{total}章',
            'chars': total_chars,
            'elapsed': time.time() - start_total,
        })

    # === 拼接 + 校验 ===
    full_story = ws.assemble()
    full_story = fix_story_format(full_story)

    score, is_valid, details = validate_story_format(full_story)
    if not is_valid:
        log.warning(f"[长文-并行] 格式校验不通过（{score}/10），详情：{details}")

    ws.export_final()

    elapsed = time.time() - start_total
    progress[task_id].update({
        'status': f'✓ 完成({total}章)',
        'chars': len(full_story),
        'elapsed': elapsed,
    })

    return full_story


def generate_story(question_title, reference_answer=None, recipe=None,
                   meta_knowledge=None):
    """
    通过 API 生成故事，支持流式输出到终端。

    根据 config.LONG_FORM_MODE 分流：
      - True  → 长文模式（大纲→批量写作交替流水线）
      - False → 短文模式（默认）：单轮按 STORY_MATERIAL_MODE 生成

    短文模式根据 config.STORY_MATERIAL_MODE 自动选择 prompt 构建方式：
      - "recipe"               配方驱动
      - "reference"            参考文章模式
      - "recipe_and_reference" 配方 + 参考文章结合

    参数：
        question_title:    知乎问题标题
        reference_answer:  高赞回答文本
        recipe:            知识库配方 dict
        meta_knowledge:    跨任务积累的元知识文本（可选，用于 --use-meta 模式）

    返回：
        生成的故事文本（已清洗），失败返回 None
    """
    from config import LLM_API_KEY, LLM_API_MAX_TOKENS, LLM_API_MODEL

    if not LLM_API_KEY:
        log.error("API Key 未配置！请在 config.py 中设置 LLM_API_KEY")
        return None

    # ===== 长文模式分发（盐选投稿） =====
    try:
        from config import LONG_FORM_MODE
    except ImportError:
        LONG_FORM_MODE = False
    if LONG_FORM_MODE:
        return generate_long_form_story(
            question_title, recipe=recipe, meta_knowledge=meta_knowledge,
        )

    # ===== 统一 prompt 构建 =====
    user_message, mode_str = build_story_prompt(
        question_title, reference_answer, recipe,
        meta_knowledge=meta_knowledge,
    )

    log.info(f"API 流式调用开始")
    log.info(f"  模型：{LLM_API_MODEL} | 模式：{mode_str}")
    log.info(f"  问题：{question_title[:40]}...")
    print()
    print("  ── 生成内容开始 ──")

    def _on_chunk(c):
        sys.stdout.write(c)
        sys.stdout.flush()

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

def generate_story_parallel(question_title, reference_answer, task_id, progress,
                            recipe=None, meta_knowledge=None):
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

    # ===== 长文模式分发（盐选投稿） =====
    try:
        from config import LONG_FORM_MODE
    except ImportError:
        LONG_FORM_MODE = False
    if LONG_FORM_MODE:
        return generate_long_form_story_parallel(
            question_title, task_id, progress,
            recipe=recipe, meta_knowledge=meta_knowledge,
        )

    # ===== 统一 prompt 构建 =====
    user_message, _ = build_story_prompt(
        question_title, reference_answer, recipe,
        meta_knowledge=meta_knowledge,
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


# ============================================================
# KB 配置解析（DRY：filter_story_questions 和 score_stories 共用）
# ============================================================

def _resolve_kb_config():
    """
    解析知识库任务用的 API 配置（KB 优先，故事生成回退）。

    返回: (api_key: str, base_url: str, model: str, extra_body: dict)
    """
    from config import LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL
    try:
        from config import KB_LLM_API_KEY as _kb_key
    except ImportError:
        _kb_key = LLM_API_KEY
    try:
        from config import KB_LLM_BASE_URL as _kb_url
    except ImportError:
        _kb_url = LLM_API_BASE_URL
    try:
        from config import KB_LLM_MODEL as _model
    except ImportError:
        _model = LLM_API_MODEL
    try:
        from config import KB_LLM_EXTRA_BODY as _extra_body
    except ImportError:
        _extra_body = {}
    return (
        (_kb_key or LLM_API_KEY),
        (_kb_url or LLM_API_BASE_URL),
        _model,
        dict(_extra_body or {}),
    )


# ============================================================
# 故事领域筛选
# ============================================================

def filter_story_questions(questions):
    """
    用 LLM 判断候选问题中哪些属于故事/小说/文学创作领域。

    参数：
        questions: [{title, ...}, ...]

    返回：
        过滤后的问题列表（只保留故事领域的）
    """
    api_key, base_url, _MODEL, extra_body = _resolve_kb_config()

    if not api_key or not questions:
        return questions

    titles = [q['title'] for q in questions]

    from config import FILTER_PROMPT
    prompt = FILTER_PROMPT
    for i, t in enumerate(titles):
        prompt += f"{i+1}. {t}\n"

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.1,
        "stream": False
    }
    if extra_body:
        payload.update(extra_body)

    try:
        log.info("LLM 故事领域筛选...")
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.encoding = "utf-8"  # 强制 UTF-8，避免响应头无 charset 时中文乱码
        if resp.status_code != 200:
            log.warning(f"筛选 API 失败：{resp.status_code}")
            return questions

        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()

        # ★ Token 用量上报
        try:
            from llm_token_tracker import tracker
            tracker.report(_MODEL, data.get("usage", {}))
        except Exception:
            pass

        log.info(f"  LLM 回复：{reply}")

        # "无"或类似回复 → 没有适合的问题
        if reply.strip() == "无" or ("没有" in reply and len(reply) < 15):
            log.warning("  LLM 认为没有适合写故事的问题")
            for q in questions:
                q['is_story'] = False
            return []  # 返回空列表，外部会兜底

        # 提取保留的编号（正向逻辑）
        # 兼容多种格式：逗号分隔 "1,3,5" / 中文标点 "1、3、5" / 空格分隔 "1 3 5"
        #            / "保留1,2,3" / "编号1、3" 等
        numbers = re.findall(r'\d+', reply)
        keep_indices = set()
        for n in numbers:
            idx = int(n) - 1
            if 0 <= idx < len(questions):
                keep_indices.add(idx)

        # 兜底：如果按数字没解析到，尝试按中文大写数字或"全部保留"之类的文本
        if not keep_indices:
            if any(kw in reply for kw in ('全部保留', '全部适合', '都适合', '都保留',
                                           '都适合写故事', '均适合', '均保留')):
                log.info("  LLM 认为全部适合写故事")
                return questions
            log.warning("  未解析到有效编号，返回全部兜底")
            return questions

        # 正向保留
        filtered = [questions[i] for i in sorted(keep_indices)]
        excluded = [questions[i] for i in range(len(questions)) if i not in keep_indices]

        for q in filtered:
            q['is_story'] = True
        for q in excluded:
            q['is_story'] = False

        kept_titles = [q['title'][:25] for q in filtered]
        excluded_titles = [q['title'][:25] for q in excluded]
        log.info(f"  保留 {len(filtered)}/{len(questions)} 个故事问题：{kept_titles}")
        if excluded_titles:
            log.info(f"  排除 {len(excluded)} 个非故事问题：{excluded_titles}")

        return filtered if filtered else questions  # 兜底

    except Exception as e:
        log.warning(f"筛选出错：{e}，返回全部")
        return questions


# ============================================================
# API 连接测试
# ============================================================

def test_api_connection():
    """测试 API 连接"""
    from config import (
        LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL,
        LLM_API_EXTRA_BODY,
    )

    if not LLM_API_KEY:
        print("  ❌ API Key 未配置！")
        return False

    print(f"  测试 API 连接...")
    print(f"  地址：{LLM_API_BASE_URL}")
    print(f"  模型：{LLM_API_MODEL}")
    print(f"  Key：{LLM_API_KEY[:8]}...{LLM_API_KEY[-4:]}")

    url = f"{LLM_API_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": "请回复：连接成功"}],
        "max_tokens": 20,
        "stream": False
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)

    try:
        start = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.encoding = "utf-8"  # 强制 UTF-8
        elapsed = time.time() - start

        if response.status_code == 200:
            reply = response.json()["choices"][0]["message"]["content"]
            print(f"  ✓ 连接成功！（{elapsed:.1f}s）回复：{reply}")
            return True
        else:
            print(f"  ❌ HTTP {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ {e}")
        return False


# ============================================================
# 评分 JSON 容错解析
# ============================================================

_SCORE_OBJ_RE = re.compile(
    r'\{\s*"index"\s*:\s*(\d+)\s*,\s*"hook"\s*:\s*(\d+)\s*,\s*"plot"\s*:\s*(\d+)\s*,'
    r'\s*"emotion"\s*:\s*(\d+)\s*,\s*"authenticity"\s*:\s*(\d+)\s*,\s*"ending"\s*:\s*(\d+)\s*,'
    r'\s*"format"\s*:\s*(\d+)\s*,\s*"total"\s*:\s*(\d+)\s*,\s*"comment"\s*:\s*"([^"]*)'
)

def _parse_score_json(reply_text, expected_count):
    """
    解析评分 JSON，优先 stright parse，失败后用正则逐个提取对象。
    返回解析出的评分列表 [{index, hook, plot, ...}, ...]
    """
    try:
        scores = json.loads(reply_text)
        if isinstance(scores, list) and len(scores) > 0:
            return scores
    except json.JSONDecodeError:
        pass

    # 正则回退：逐个提取 {"index": N, ...} 对象
    scores = []
    for m in _SCORE_OBJ_RE.finditer(reply_text):
        scores.append({
            'index': int(m.group(1)),
            'hook': int(m.group(2)),
            'plot': int(m.group(3)),
            'emotion': int(m.group(4)),
            'authenticity': int(m.group(5)),
            'ending': int(m.group(6)),
            'format': int(m.group(7)),
            'total': int(m.group(8)),
            'comment': m.group(9),
        })

    if scores:
        log.info(f"  正则回退解析成功：{len(scores)}/{expected_count} 篇")
        return scores

    # 都失败，抛异常让外层兜底
    raise json.JSONDecodeError("无法解析评分 JSON", reply_text, 0)


# ============================================================
# 文章质量评分
# ============================================================

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
    from config import SCORE_PROMPT
    prompt = SCORE_PROMPT
    try:
        from config import SCORE_STORY_HEAD_CHARS, SCORE_STORY_TAIL_CHARS
    except ImportError:
        SCORE_STORY_HEAD_CHARS = 1000
        SCORE_STORY_TAIL_CHARS = 500

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
        import time as _time
        start = _time.time()
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.encoding = "utf-8"  # 强制 UTF-8
        elapsed = _time.time() - start

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

        scores = _parse_score_json(clean_reply, len(stories_data))

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
