# ============================================================
# core/story_text.py — 故事文本管线
#
# 从 llm_api.py 剥离的纯文本处理层：清洗、断句、格式修复、
# 格式校验、章节拆分、评分 JSON 解析。零网络依赖，
# 全部函数可脱离 LLM 单独测试。
#
# 架构位置：Layer 2 (Core Capabilities) — 创作核心
# ============================================================

import json
import logging
import re

# 兼容 re-export：历史调用方仍从 story_text 导入量化检测
from core.detectors import check_quant_density  # noqa: F401

log = logging.getLogger(__name__)

# ============================================================
# ★ 段落长度阈值（手动调节此值）
# ============================================================
# 超过此字数的段落被视为"长段落"
# 建议根据 tools/story_plots.py 的分布图来调整
# 知乎故事风格参考：短句成段通常 15-40 字，对话+描写约 40-70 字
# 阈值设为 80 给对话+描写留足余量，避免正常叙事被误判为"文字墙"
PARA_LENGTH_THRESHOLD = 80


# 量化密度检测已收敛至 core/detectors.py；此处保留兼容 re-export。
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
    text = text.replace('“', '「').replace('”', '」')
    text = text.replace('„', '「').replace('‟', '」')
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
    cn_quotes = len(re.findall(r'["“”„‟]', text))
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

    # --- 6. 量化密度（防数字堆砌 AI 味）：软性减分 + 反馈提示 ---
    qd = check_quant_density(text)
    if qd.get("flagged"):
        if qd.get("stack_ratio", 0) > QUANT_STACK_RATIO:
            score -= 2
            details["量化"] = f"{qd.get('reason', '')}(-2)"
        else:
            score -= 1
            details["量化"] = f"{qd.get('reason', '')}(-1)"

    score = max(score, 0)
    is_valid = score >= 6

    log.info(f"  格式检测：{score}/10 {'✓合规' if is_valid else '✗不合规'}"
             f"{' (' + ', '.join(f'{k}:{v}' for k, v in details.items()) + ')' if details else ''}")

    return score, is_valid, details


# ============================================================
# 参考文章片段采样（采样模式素材）
# ============================================================

def sample_reference_sections(answer, max_chars=3000):
    """截取参考回答前 max_chars 字作为注入素材（零 LLM 调用）。

    最常见套路：直接注入开头 max_chars 字，超过就在段落/句末
    边界截断，避免半句话注入破坏语感；不足则全量保留。

    返回截取后的文本（原样保留段落），answer 为空返回空串。
    """
    if not answer or not str(answer).strip():
        return ""
    text = str(answer).strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    # 在最后一个段落/句末边界回退截断（只在余量足够时，避免回退过狠）
    for delim in ("\n\n", "。", "！", "？"):
        cut = head.rfind(delim)
        if cut >= max_chars * 0.6:
            return head[: cut + 1]
    return head

# ============================================================
# LLM 输出 JSON 容错解析（公共提取 + 各域 schema 解析）
# ============================================================

def strip_json_fences(text):
    """剥掉 LLM 回复包裹 JSON 的 ``` / ```json 围栏；无围栏原样返回。"""
    if not text:
        return ""
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    return clean.strip()


def _find_balanced_json_blocks(text):
    """字符串感知地扫描文本，返回所有平衡的 JSON 块（最外层优先）。

    遇到 `{`/`[` 起一块，用栈计数定位匹配的 `}`/`]`；在字符串内（`"…"`
    且未被 `\\"` 转义）遇到的括号不计入深度，避免字符串值里的 `}` 提前
    截断。返回按出现顺序排列的块列表，调用方按长度倒序尝试即可拿到
    最外层整块，也能忽略 JSON 之后的尾随内容（如多余的 `}`）。"""
    blocks = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "{[":
            depth, j, in_str, esc = 0, i, False, False
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c in "[{":
                        depth += 1
                    elif c in "]}":
                        depth -= 1
                        if depth == 0:
                            blocks.append(text[i:j + 1])
                            break
                j += 1
            i = j
        else:
            i += 1
    return blocks


def extract_json_block(text):
    """从 LLM 回复提取 JSON：剥围栏后整体解析（单次尝试）。

    成功返回解析结果（list/dict/标量），失败返回 None。
    调用方按各自 schema 做二次校验或降级容错（正则/修复等）。
    """
    try:
        return json.loads(strip_json_fences(text))
    except (json.JSONDecodeError, TypeError):
        pass
    # 整体解析失败：用字符串感知的平衡块提取（忽略尾随内容/字符串内括号），
    # strict=False 允许字符串值里的控制字符（LLM 常把原文换行直接塞进
    # 字符串而未转义）。按块长从大到小尝试，先拿最外层整块。
    blocks = sorted(_find_balanced_json_blocks(text), key=len, reverse=True)
    for block in blocks:
        try:
            return json.loads(block, strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


_SCORE_OBJ_RE = re.compile(
    r'\{\s*"index"\s*:\s*(\d+)\s*,\s*"hook"\s*:\s*(\d+)\s*,\s*"plot"\s*:\s*(\d+)\s*,'
    r'\s*"emotion"\s*:\s*(\d+)\s*,\s*"authenticity"\s*:\s*(\d+)\s*,\s*"ending"\s*:\s*(\d+)\s*,'
    r'\s*"format"\s*:\s*(\d+)\s*,\s*"total"\s*:\s*(\d+)\s*,\s*"comment"\s*:\s*"([^"]*)'
)


def parse_score_json(reply_text, expected_count):
    """
    解析评分 JSON，优先直解，失败后用正则逐个提取对象。
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
