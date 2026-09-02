# ============================================================
# core/originality.py — 纯净模式「洗稿/抄袭」对比审核
#
# 职责：对新生成的回答与参考高赞回答做原创性对比评估，
#       判定模型是否涉嫌抄袭（原文照搬）或洗稿（换皮重写）。
#
# 审核信号 = 本地文本相似度（LLM 不可用时兜底）+ LLM 综合判断：
#   - 本地：最长公共子串占比 / 字符 bigram Dice / 句子重复率
#   - LLM：沿用既有「严禁搬运/换皮重写」判定思路（prompts.py 的
#     ORIGINALITY_AUDIT_PROMPT），对比两文给出 verdict
#     （original / laundered / copied）与可疑点
#
# 架构位置：Layer 2 (Core Capabilities) — 与 detectors 同级，
# 零网络依赖的本地信号可独立测试。
# ============================================================

import logging
import re

log = logging.getLogger(__name__)

# ---- 本地信号阈值（风格学习允许句式/语气相似；达到任一即判违规）----
# 最长公共子串长度 ÷ 较短文本长度：原文大段照搬的最直接证据
# （短回答整段摘抄长参考时，占比反而接近 100%，可被准确拦截）
LCS_RATIO_FAIL = 0.55
# 字符双字母组 Dice 系数：洗稿换皮后字面整体相似度仍偏高
BIGRAM_DICE_FAIL = 0.65
# 参考回答的句子在新回答中原样出现的比例（换皮重写常整句复用）
SENT_DUP_RATIO_FAIL = 0.45

# 本地比较的字符上限（控制 LCS 二分 + bigram 集合的内存/耗时）
_CAP_CHARS = 3000
_MIN_SENT_CHARS = 10


def _squeeze(text):
    """去标题语法与空白，得到连续字符流（字符级比较用）。"""
    text = re.sub(r"^#{1,6}\s*.*$", "", text or "", flags=re.M)
    return re.sub(r"\s+", "", text or "")[:_CAP_CHARS]


def _sentences(text):
    return [s.strip() for s in re.split(r"[。！？!?；;\n]", text or "")
            if len(s.strip()) >= _MIN_SENT_CHARS]


def _longest_common_substring_len(a, b):
    """二分 + 集合：最长公共子串长度（字符级，已去空白）。"""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    lo, hi = 1, len(a)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        seen = {a[i:i + mid] for i in range(len(a) - mid + 1)}
        if any(b[j:j + mid] in seen for j in range(len(b) - mid + 1)):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _bigram_dice(a, b):
    A = {a[i:i + 2] for i in range(len(a) - 1)}
    B = {b[i:i + 2] for i in range(len(b) - 1)}
    if not A or not B:
        return 0.0
    inter = len(A & B)
    return 2.0 * inter / (len(A) + len(B))


def local_signals(new_text, ref_text):
    """本地相似度信号（不依赖 LLM，可离线判定）。"""
    new_flat = _squeeze(new_text)
    ref_flat = _squeeze(ref_text)
    lcs_len = _longest_common_substring_len(new_flat, ref_flat)
    # 占比按「较短者」计：短回答整段照搬长参考时也应收紧拦截
    lcs_ratio = lcs_len / max(min(len(new_flat), len(ref_flat)), 1)
    dice = _bigram_dice(new_flat, ref_flat)

    ref_sents = _sentences(ref_text)
    sent_dup = 0
    if ref_sents:
        for s in ref_sents:
            if s in new_text or s in new_flat:
                sent_dup += 1
        sent_dup_ratio = sent_dup / len(ref_sents)
    else:
        sent_dup_ratio = 0.0

    return {
        "lcs_len": lcs_len,
        "lcs_ratio": round(lcs_ratio, 4),
        "bigram_dice": round(dice, 4),
        "sent_dup_ratio": round(sent_dup_ratio, 4),
        "new_chars": len(new_flat),
        "ref_chars": len(ref_flat),
    }


def local_verdict(signals):
    """本地信号 → 违规理由列表（空 = 本地信号正常）。"""
    reasons = []
    if signals.get("lcs_ratio", 0) >= LCS_RATIO_FAIL:
        reasons.append(
            f"最大公共片段占比 {signals['lcs_ratio']:.0%}"
            f"（{signals.get('lcs_len', 0)} 字，疑似原文照搬）")
    if signals.get("bigram_dice", 0) >= BIGRAM_DICE_FAIL:
        reasons.append(
            f"字面相似度 {signals['bigram_dice']:.0%}（疑似洗稿换皮重写）")
    if signals.get("sent_dup_ratio", 0) >= SENT_DUP_RATIO_FAIL:
        reasons.append(
            f"参考回答句子重复占比 {signals['sent_dup_ratio']:.0%}"
            f"（大面积整句复用）")
    return reasons


def _llm_audit(question_title, new_text, ref_text):
    """LLM 对比审核（沿用既有「抄袭/洗稿」判定思路）。

    通道与主链路一致：API 模式走服务商 API，Web 模式走网页版大模型。
    失败/不可用返回 None，调用方回退本地信号（不阻断流程）。
    """
    from applications.zhihu_story.prompts import ORIGINALITY_AUDIT_PROMPT
    from config import LLM_MODE

    prompt = (ORIGINALITY_AUDIT_PROMPT
              + f"\n\n### 知乎问题\n{(question_title or '')[:200]}\n"
              + f"\n### 新回答（生成内容）\n{(new_text or '')[:3000]}\n"
              + f"\n### 参考高赞回答\n{(ref_text or '')[:3000]}\n")

    reply = None
    if LLM_MODE == "api":
        from llm_client import call_llm_non_streaming, resolve_kb_llm_config
        api_key, base_url, model, extra_body = resolve_kb_llm_config()
        if not api_key:
            log.warning("原创审核跳过（无 API Key），仅用本地相似度判断")
            return None
        try:
            reply, _elapsed, error = call_llm_non_streaming(
                prompt, max_tokens=800, temperature=0.1, timeout=120,
                api_key=api_key, base_url=base_url, model=model,
                extra_body=extra_body)
            if error:
                log.warning("原创审核请求失败：%s（仅用本地信号）",
                            error[:120])
                return None
        except Exception as exc:
            log.warning("原创审核异常：%s（仅用本地信号）", exc)
            return None
    else:
        from story_scoring import _web_llm_generate
        log.info("原创审核（Web 模式：网页版大模型）...")
        reply = _web_llm_generate(prompt, "原创审核")
        if not reply:
            log.warning("原创审核 Web 无结果（仅用本地信号）")
            return None

    from core.story_text import extract_json_block
    try:
        data = extract_json_block(reply)
        if not isinstance(data, dict):
            return None
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("original", "laundered", "copied"):
            return None
        return data
    except Exception as exc:
        log.warning("原创审核解析失败：%s（仅用本地信号）", exc)
        return None


def audit_originality(question_title, new_text, ref_text, enable_llm=True):
    """纯净模式审核入口：新回答 vs 参考高赞回答。

    返回 dict：
        passed        是否可以发布（未涉嫌抄袭/洗稿）
        verdict       原创 / 抄袭 / 洗稿 / 疑似洗稿
        reasons       违规理由列表（空 = 未发现）
        signals       本地相似度信号
        llm_detail    LLM 判定明细（None = LLM 未参与/不可用）
        originality   LLM 原创度评分（0-100，可能为 None）
    """
    new_text = new_text or ""
    ref_text = ref_text or ""
    if not new_text.strip() or not ref_text.strip():
        return {"passed": True, "verdict": "跳过（素材为空）",
                "reasons": [], "signals": {}, "llm_detail": None,
                "originality": None}

    signals = local_signals(new_text, ref_text)
    local_reasons = local_verdict(signals)

    # 段落长度分布对比（纯数学）：参考短段成文而你写出大长段 → 判不合格
    para = paragraph_similarity(ref_text, new_text)
    para_bad = not para.get("ok")

    llm_detail = None
    llm_verdict = None
    if enable_llm:
        llm_detail = _llm_audit(question_title, new_text, ref_text)
        if llm_detail:
            llm_verdict = str(llm_detail.get("verdict") or "").strip().lower()

    combined = list(local_reasons)
    if para_bad and para.get("reason"):
        combined.append(para["reason"]
                        + "（段落长度应贴近参考回答，请按参考分段重写）")
    if llm_detail:
        for r in (llm_detail.get("reasons") or [])[:3]:
            if r and r not in combined:
                combined.append(r)

    suspicious_local = bool(local_reasons)
    suspicious_llm = llm_verdict in ("copied", "laundered")

    if suspicious_llm:
        passed = False
        verdict = "抄袭" if llm_verdict == "copied" else "洗稿"
    elif suspicious_local:
        passed = False
        verdict = "疑似洗稿"   # 本地信号足够强、LLM 未抓到也保守拦截
    elif para_bad:
        passed = False
        verdict = "段落长度不符"
    else:
        passed, verdict = True, "原创"

    return {
        "passed": passed,
        "verdict": verdict,
        "reasons": combined,
        "signals": signals,
        "paragraph": para,
        "llm_detail": llm_detail,
        "originality": (llm_detail or {}).get("originality"),
    }




def paragraph_similarity(ref_text, new_text):
    """段落长度分布相似度（纯数学，不依赖 LLM）。

    与生成守则同源：参考回答的段落长短 = 风格的一部分；生成后实测
    生成文的段落分布，差异过大判不合格（触发重新生成）。

    指标：
      bucket_diff  短/中/长三段占比向量的 L1 距离/2（0=完全一致，1=完全相反）
      avg_ratio    min(平均段长)/max(平均段长)（1=平均长度相同）
    受 config.story.CLEAN_PARAGRAPH_* 开关与阈值控制（函数内动态读取）。
    """
    from core.story_text import paragraph_length_stats
    ref_stats = paragraph_length_stats(ref_text)
    new_stats = paragraph_length_stats(new_text)
    result = {"ref_stats": ref_stats, "new_stats": new_stats,
              "bucket_diff": None, "avg_ratio": None,
              "ok": True, "reason": ""}
    if not ref_stats or not new_stats:
        result["reason"] = "段落统计不足（参考或生成缺少正文段落），跳过判定"
        return result

    r = [ref_stats["short_ratio"], ref_stats["mid_ratio"],
         ref_stats["long_ratio"]]
    n = [new_stats["short_ratio"], new_stats["mid_ratio"],
         new_stats["long_ratio"]]
    bucket_diff = sum(abs(a - b) for a, b in zip(r, n)) / 2
    avg_ratio = (min(ref_stats["avg"], new_stats["avg"])
                 / max(ref_stats["avg"], new_stats["avg"]))
    result["bucket_diff"] = round(bucket_diff, 3)
    result["avg_ratio"] = round(avg_ratio, 3)

    from config.story import (CLEAN_PARAGRAPH_AUDIT_ENABLE,
                              CLEAN_PARAGRAPH_BUCKET_DIFF_MAX,
                              CLEAN_PARAGRAPH_AVG_MIN_RATIO)
    if not CLEAN_PARAGRAPH_AUDIT_ENABLE:
        return result

    reasons = []
    if bucket_diff > CLEAN_PARAGRAPH_BUCKET_DIFF_MAX:
        reasons.append(f'段落长短分布差异过大（差异度 {bucket_diff:.0%} '
                       f'> 上限 {CLEAN_PARAGRAPH_BUCKET_DIFF_MAX:.0%}）')
    if avg_ratio < CLEAN_PARAGRAPH_AVG_MIN_RATIO:
        reasons.append(f'平均段落长度出入过大（参考 {ref_stats["avg"]:.0f} 字/段 '
                       f'vs 生成 {new_stats["avg"]:.0f} 字/段）')
    if reasons:
        result["ok"] = False
        result["reason"] = "；".join(reasons)
    return result

def audit_feedback_text(audit):
    """把审核结论渲染成重试 prompt 的修正要求（供生成循环注入）。

    除了列出审核原因，还给出可执行的「结构性大改」清单——从日志复盘
    看，光说"别洗稿"模型容易沿用同一套路换皮，必须明确要求更换设定/
    人物/事件/台词句式才收敛得快。
    """
    audit = audit or {}
    reasons = audit.get("reasons") or []
    head = f"审核判定：{audit.get('verdict', '未通过')}。"
    lines = [head]
    if reasons:
        lines.append("审核依据：" + "；".join(str(r) for r in reasons[:4]))
    lines += [
        "请做以下【结构性大改】再输出（至少满足三项，否则仍会被判洗稿）：",
        "1. 更换故事设定背景：时代/世界观/职业/场景不要沿用参考回答的环境；",
        "2. 调整人物关系与身份：职业、身世、性格配对、相处模式至少换两项；",
        "3. 重排关键事件顺序，增删核心冲突与反转，不要沿用参考的推进节奏；",
        "4. 所有台词必须全新创作：禁止沿用参考回答任何一句的句式模板" 
        "（如“离了我，谁还……”这类），换完全不同的表达；",
        "5. 更换叙述视角或开头引入方式，避免开头手法一致。",
        "请重写成一篇完全原创的新回答（可保留其风格气质）。",
    ]
    return "\n".join(lines) + "\n"
