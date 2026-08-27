# ============================================================
# core/detectors.py — 检测器统一层
#
# 提示词守则(DEAI_STYLE_RULE 各小节)与本地质检信号的单一对应点：
#   PROMPT_RULE_MAP[提示词小节名] = [detector_id, ...]
# 新增写作质量规则时：在此注册检测器并在 map 里挂上提示词小节，
# 保证「生成前有约束 <-> 生成后有体检」永不脱节。
#
# 现役检测器：
#   quant_density 量化堆砌(跨单位换算堆叠 / 同句>=4处数量罗列)
#   ai_flavor     AI味指数(连接词/修饰语/排比/句式/长句率/句首重复)
# 待建 naming_burst(人名轰炸)：中文分词方案未定，暂列 map 备忘
# ============================================================

import logging
import re
import statistics

log = logging.getLogger(__name__)


# ============================================================
# ★ 检测器一：量化密度（防"数字堆砌"AI 味）
# ============================================================
# 人类作者基准（实测采集库：中文数字+量词约 8/千字、堆砌句少）：
#   - 中文量化密度阈值：> 20/千字 才提示（人类 8-11，留足余量）
#   - 病态堆砌专测：跨单位换算堆叠（一年，三百六十五天）或同句 ≥4 处数量罗列
#   - 阿拉伯数量表达：> 12/千字 单独提示
# 只做软性减分提示（进 details → 重试反馈），不否决格式合规。
QUANT_CN_RE = re.compile(
    r"[一二两三四五六七八九十百千万]+\s*(?:个|只|条|张|把|位|名|岁|次|回|遍|趟|人|桌|碗|杯|盘|瓶|盒|包|"
    r"件|双|副|束|朵|棵|根|颗|粒|层|楼|间|户|页|份|笔|单|圈|步|米|公里|斤|克|毫升|元|角|分|天|日|夜|周|月|"
    r"年|小时|分钟|秒|通|封|本|辆|架|台|部|套|组|批|对|种|家|店)")
QUANT_ARAB_RE = re.compile(r"\d+\s*(?:个|只|条|张|把|位|名|岁|次|回|人|桌|碗|杯|件|双|元|块|天|年|小时|分钟|秒|层|斤)")
QUANT_DENSITY_CN = 20.0      # 中文量化表达 每千字 阈值（人类约 8-11，20 才提示）
QUANT_DENSITY_ARAB = 12.0    # 阿拉伯数量表达 每千字 阈值
QUANT_STACK_RATIO = 0.02     # 堆砌句比例阈值（validate_story_format 减分用）
# 病态堆砌专测（AI 特征，区别于正常单数使用）：
#   1) 跨单位换算/并列堆叠：一年，三百六十五天 / 三千二百块，四十七斤
#   2) 同句 ≥4 处量化（含量词）：数量罗列成清单
# 序数先行剥离（第一名/第二次/第七层 不是数量堆砌）
_QUANT_ORDINAL_RE = re.compile(r"第\s*[一二两三四五六七八九十百千万\d]+")
# 同量词重复列举：X岁，X岁 / X次，X次 / X年，X年（紧凑相邻）
QUANT_DUP_RE = re.compile(
    r"(?:[一二两三四五六七八九十百千万]+|\d+)\s*(岁|年|天|次|回|个|只|块|元)\s*[，、,\s]+"
    r"(?:[一二两三四五六七八九十百千万]+|\d+)\s*\1")
# 跨单位换算/并列堆叠：一年，三百六十五天 / 三千二百块，四十七斤
QUANT_CONVERT_RE = re.compile(
    r"(?:[一二两三四五六七八九十百千万]+|\d+)\s*(?:年|天|小时|分钟|块|元)\s*[，、,]+"
    r"(?:[一二两三四五六七八九十百千万]+|\d+)\s*(?:天|小时|分钟|秒|块|元)")
# 同句 ≥4 处普通量化（数量罗列成清单）
QUANT_TRIPLE_RE = re.compile(
    r"(?:[一二两三四五六七八九十百千万]+|\d+)\s*(?:个|只|条|张|位|次|回|人|桌|碗|杯|件|双|元|块|天|年|"
    r"小时|分钟|秒|层|斤|岁)")



# ============================================================
# ★ 人物命名密度检测（防"人名轰炸"——开头一堆名字的 AI 味）
# ============================================================
# 启发式：常用姓氏 + 1~2 字名 粗识别（噪声较大，仅用于"开头人名过载"
# 与"一次性全名"两个保守信号，阈值取高避免误伤）。
# 阈值（吸取人工抽查经验：人类开头 600 字通常 0~2 个真名）：
#   - 开头 1500 字真名 > 5 个 → 提示（一眼压力大）
#   - 全文出现 ≤2 次的全名占比 > 70% → 提示（一堆一次性名字）
def check_quant_density(text):
    """检查故事文本的"量化表达密度"（数字堆砌 AI 味信号）。

    返回 dict：
        quant_cn:        中文数字+量词 次数
        quant_arab:      阿拉伯数字+量词 次数
        density_cn:      中文量化 / 千字
        density_arab:    阿拉伯量化 / 千字
        stack_ratio:     含 ≥2 处量化表达的句子占比
        flagged:         是否命中任一阈值（True 则需要提示重写）
        reason:          人类可读的提示语（供 details/重试反馈）
        examples:        最多 3 个堆砌句示例
    """
    if not text or not text.strip():
        return {"flagged": False, "reason": "", "examples": []}
    # 去章节标题行，避免 "## **1**" 的编号被计入
    body_lines = [ln for ln in text.split("\n")
                  if ln.strip() and not re.match(r"^#{1,3}\s", ln.strip())]
    body = "\n".join(body_lines)
    total = len(re.sub(r"\s+", "", body))
    if total < 100:
        return {"flagged": False, "reason": "", "examples": []}

    # 逐句统计 + 病态堆砌专测
    sents = [s for s in re.split(r"[。！？…\n]+", body) if s.strip()]
    density_cn = len(QUANT_CN_RE.findall(body)) * 1000.0 / total
    density_arab = len(QUANT_ARAB_RE.findall(body)) * 1000.0 / total

    n_pair = 0
    n_triple = 0
    examples = []
    for s in sents:
        stripped = _QUANT_ORDINAL_RE.sub("", s)   # 先剥序数
        if not stripped.strip():
            continue
        conv = QUANT_CONVERT_RE.search(stripped)   # 跨单位换算堆叠（AI 典型病）
        triple = QUANT_TRIPLE_RE.findall(stripped) # 同句罗列（≥3 处）
        if conv:
            n_pair += 1
            if len(examples) < 3:
                examples.append(s[:50])
        elif len(triple) >= 4:
            n_triple += 1
            if len(examples) < 3:
                examples.append(s[:50])
    stack_ratio = (n_pair + n_triple) / max(len(sents), 1)

    reasons = []
    if density_cn > QUANT_DENSITY_CN:
        reasons.append(f"量化表达偏密（{density_cn:.1f}/千字）")
    if density_arab > QUANT_DENSITY_ARAB:
        reasons.append(f"阿拉伯数量表达偏多（{density_arab:.1f}/千字）")
    if n_pair > 0 or n_triple > 0:
        reasons.append(f"数字堆砌句 {n_pair + n_triple} 处"
                       f"（重复列举/换算/罗列式量化）")

    return {
        "quant_cn": len(QUANT_CN_RE.findall(body)),
        "quant_arab": len(QUANT_ARAB_RE.findall(body)),
        "density_cn": round(density_cn, 1),
        "density_arab": round(density_arab, 1),
        "stack_ratio": round(stack_ratio, 2),
        "flagged": bool(reasons),
        "reason": "；".join(reasons),
        "examples": examples,
    }


# ============================================================
# 段落长度分布分析 + 绘图
# ============================================================

# ============================================================
# AI 味指数（自 tools/ai_flavor_check.py 收敛；CLI 只是外壳）
# ============================================================

CONNECTORS = [
    "然而", "因此", "与此同时", "总而言之", "不可否认", "综上所述",
    "由此可见", "在这个", "让我们", "不禁让人", "意味深长",
    "值得一提的是", "不难发现", "这不仅仅是",
]
# 中文 AI 高频句式（正则）
PATTERN_SCORES = [
    (re.compile(r"一旦[^。，！？]{0,14}就"), 5),         # 一旦…就
    (re.compile(r"只有[^。，！？]{0,12}才"), 5),         # 只有…才
    (re.compile(r"遮羞布|面具|画皮|烟幕弹|锦囊|伪装成"), 5),  # 揭露式比喻
    (re.compile(r"最[^。，！？]{0,12}的地方在于|真正[^。，！？]{0,8}的是"), 5),  # 极值判断
]
FILLERS = [
    "仿佛", "似乎", "瞬间", "终于", "深深地", "默默地", "缓缓地",
    "淡淡地", "轻轻", "愣住了", "颤抖着", "微微",
    "喃喃", "猛然", "倏地", "低声", "心口", "眼底",
]
LONG_SENT_CHARS = 45
MAX_WINDOW_CHARS = 6000  # 检查前 6000 字（开头 AI 味最密集）


def _clean(text):
    text = re.sub(r"^#{1,6}\s*.*$", "", text, flags=re.M)  # 去除标题/章节头
    text = re.sub(r"^[\s]*[-*_]{3,}[\s]*$", "", text, flags=re.M)  # 分隔线
    return text


def _sentences(text):
    return [s.strip() for s in re.split(r"[。！？!?；;\n]", text) if s.strip()]


def check_ai_flavor(text):
    text = _clean(text or "")[:MAX_WINDOW_CHARS]
    sents = _sentences(text)
    if not sents:
        return None
    conn = sum(text.count(c) for c in CONNECTORS)
    fill = sum(text.count(f) for f in FILLERS)
    par = len(re.findall(r"(?:不仅仅|不只是|不是)[^。！？]{0,24}而是", text))
    pat = sum(1 for p, _ in PATTERN_SCORES if p.findall(text))
    long_n = sum(1 for s in sents if len(s) > LONG_SENT_CHARS)
    long_ratio = long_n / len(sents)
    starts = [s[:4] for s in sents if s]
    dup = sum(1 for i in range(1, len(starts)) if starts[i] == starts[i - 1])
    start_dup_ratio = dup / max(1, len(starts) - 1)
    avg_len = statistics.mean(len(s) for s in sents)

    score = 0
    score += min(24, conn * 6)
    score += min(30, fill * 3)   # 修饰语是最强判别信号（AI 每 6k 字 7-18 个，真人 0-2）
    score += min(24, par * 12)
    score += min(20, pat * 10)   # 关联句式/揭露比喻/极值判断
    if long_ratio > 0.25:
        score += 8
    if start_dup_ratio > 0.18:
        score += 10
    if avg_len > 38:
        score += 6
    total = min(100, score)

    metrics = {
        "连接词": conn, "修饰语": fill, "排比": par, "句式": pat,
        "长句比例": f"{long_ratio:.2f}",
        "句首重复": f"{start_dup_ratio:.2f}",
        "平均句长": round(avg_len, 1),
    }
    return metrics, total


def flavor_verdict(score):
    if score < 25:
        return "低（像人手写）"
    if score < 45:
        return "中（有一定AI味）"
    return "高（AI味明显）"


# ============================================================
# ★ 检测器三：scene_dump（惰性环境空镜 / 死描写）
#
# 只抓"规则可判定的硬形态"，审美留给 prompt 守则：
#   1) 空镜开场：正文前 ~200 字内 ≥2 个环境词且 0 个人物动作/对话
#   2) 纯景清场段：某段 ≥3 个环境词且 0 动作/对话/人称代词
# 环境词表与动作/人物排除表为规则近似，阈值取高防误伤。
# ============================================================
_ENV_WORDS = [
    "阳光", "月光", "星光", "灯光", "烛光", "路灯", "晨光", "夕照", "余晖",
    "窗外", "窗台", "窗帘", "窗纱", "落地窗", "玻璃", "门前", "门口", "走廊",
    "墙角", "墙脚", "天花板", "墙壁", "白墙", "地板", "地毯", "客厅", "房间",
    "屋子", "院子", "庭院", "街道", "马路", "路边", "街头", "巷子", "远处",
    "天边", "天空", "夜色", "夜幕", "细雨", "雨丝", "雨声", "雨滴", "雷声",
    "风声", "微风", "寒风", "树叶", "树影", "花坛", "尘埃", "光线", "光影",
    "雾气", "薄雾", "云层", "云影", "暮色", "昏暗", "幽暗", "寂静", "安静地",
    "空旷", "空荡荡", "冷清", "昏暗的", "明亮的", "金黄", "昏黄",
]
_ENV_ACT_SIGNS = [
    "他说", "她说", "我说", "他道", "她道", "我道", "心里", "突然",
    "猛地", "转身", "回头", "抬头", "低头", "推门", "进门", "下楼", "上楼",
    "喊了一声", "叫住", "哭了出来", "笑了笑", "站起来", "坐下", "躺着",
    "走进", "走出", "跑进", "跑出", "看了一眼", "望着", "盯着", "抓住",
    "握住", "抱着", "拿起", "放下", "问道", "答道", "说着",
    # 人物在场排除：空镜的本质是"没有人在场"，任何叙事主语都应阻断
    "我" , "你", "他", "她" , "我们", "你们", "他们", "自己",
]


def check_scene_dump(text):
    """检查"惰性环境空镜"（死描写）：空镜开场 / 纯景清场段。

    返回 dict：
        open_flagged:  空镜开场（前200字 ≥2 环境词 & 无人物动作/对话）
        para_flagged:  存在纯景清场段（≥3 环境词 & 无动作/人称）
        pure_para_n:   纯景段数量
        flagged:       是否命中任一阈值
        reason:        人类可读提示（details/重试反馈用）
    """
    import re
    if not text or not text.strip():
        return {"flagged": False, "open_flagged": False,
                "para_flagged": False, "pure_para_n": 0, "reason": ""}
    body_lines = [ln for ln in text.split(chr(10))
                  if ln.strip() and not re.match(r"^#{1,3}\s", ln.strip())]
    body = chr(10).join(body_lines)

    def env_count(s):
        return sum(1 for w in _ENV_WORDS if w in s)

    def act_count(s):
        return sum(1 for a in _ENV_ACT_SIGNS if a in s)

    # 空镜开场：前 200 字内 ≥2 环境词 & 无动作/对话
    head = body[:200]

    punct = '\'.…。！？!?\'"“”「」'
    head_text = re.sub(r"[\s%s]+" % punct, "", head)
    open_flagged = (env_count(head_text) >= 2
                    and act_count(head_text) == 0
                    and '"' not in head and "「" not in head)
    head_example = head[:40] if open_flagged else ""

    # 纯景清场段：按行（段）统计，≥3 环境词 & 0 动作 & 无对话引号
    pure_para_n = 0
    examples = []
    for p in body_lines:
        p = p.strip()
        if len(p) < 15:
            continue
        p_no_ws = re.sub(r"\s+", "", p)
        if (env_count(p_no_ws) >= 3 and act_count(p_no_ws) == 0
                and '"' not in p and "「" not in p):
            pure_para_n += 1
            if len(examples) < 3:
                examples.append(p[:50])
    para_flagged = pure_para_n >= 1

    reasons = []
    if open_flagged:
        reasons.append("空镜开场：前200字纯景物铺垫（无人物/动作/对话）")
    if para_flagged:
        reasons.append(f"纯景清场段 {pure_para_n} 个（{pure_para_n}处连续景物无人物动作）")
    return {
        "open_flagged": open_flagged,
        "para_flagged": para_flagged,
        "pure_para_n": pure_para_n,
        "flagged": bool(reasons),
        "reason": "；".join(reasons),
        "head_example": head_example,
        "examples": examples,
    }


# ============================================================
# 注册表：id -> 检测函数。返回约定：quant_* / scene_dump 为
# dict(flagged/reason…)；ai_flavor 为 (metrics, score)/None。
# ============================================================
DETECTORS = {
    "quant_density": check_quant_density,
    "ai_flavor": check_ai_flavor,
    "scene_dump": check_scene_dump,
}

# 提示词小节 <-> 检测器 对应表（生成侧约束与本地质检同源）
PROMPT_RULE_MAP = {
    "行文去AI味守则": ["ai_flavor"],
    "量化克制守则": ["quant_density"],
    "人物命名与出场守则": [],  # naming_burst 待中文分词方案落地
    "环境与场景描写守则": ["scene_dump"],
}
