# ============================================================
# applications/zhihu_story/comment_fallback.py
# 打卡评论兜底：最后一班如果「发布评论」还没达成，就在**参考故事**下补一条。
#
# 为什么要有它（用户 2026-09-29 口径）：
#   回复读者评论那条链路会「挑不出合适的评论」而整体跳过——
#   界面显示任务完成，实际一条评论都没发出去，打卡的「发布评论」项永远
#   达不成。而参考故事（问题下的首答）此刻就在眼前、且**评论别人的回答
#   没有任何限制**，所以在最后一班顺手补一条，是最省事也最可靠的保底。
#
# 分层：本模块只做**纯逻辑**（提示词 + 校验 + 去重启发式），
# 发送在 browser_interact.send_answer_comment，编排在 checkin_task。
# ============================================================

import re

from applications.zhihu_story import reply_prompts as rp

# 评论比「回复」宽松：打卡只要求「10 字以上有效评论」。
# 上限给得比回复宽一些（对一篇故事说几句，30-60 字自然），但绝不能长成小作文。
COMMENT_MIN_CHARS = 10
COMMENT_MAX_CHARS = 80

# 评论里不该出现的「AI 味/客服腔」——直接复用回复模块的黑名单，避免两套标准
_FORBIDDEN = rp.FORBIDDEN_PHRASES
_CONFRONTATIONAL = rp.CONFRONTATIONAL


def clean(text):
    """归一化：剥零宽字符、压空白。"""
    return rp.clean_comment_text(text)


def build_prompt(story_title, story_text, avoid_texts=()):
    """写评论的提示词：喂**参考故事正文**，让它贴着内容说。

    刻意要求「说一个具体细节」：通用夸奖（写得好/支持）既容易被判无效评论，
    也不好看。avoid_texts 是今天已经发过的评论文本，要求换一种说法。
    """
    excerpt = clean(story_text or "")[:1200]
    lines = [
        "你在知乎读到一篇故事，想留一条评论。",
        "",
        "【故事标题】%s" % clean(story_title or "")[:60],
        "【故事内容（节选）】",
        excerpt or "（未拿到正文）",
        "",
        "【要求】",
        "1. 只回一条评论，中文，%d-%d 字。" % (COMMENT_MIN_CHARS, COMMENT_MAX_CHARS),
        "2. **贴着这段内容说**：点出一个具体细节、情节或感受，"
        "让人看出你真的读了（例如提到某个情节的转折、某句话的写法）。",
        "3. 不要说「写得好」「支持」「加油」「期待后续」这类空话，"
        "也不要客套或复述标题。",
        "4. 不提作者名字，不称呼「作者」，不用「您」，像普通读者随口说话。",
        "5. 不要用引号或书名号把整句包起来，不要 emoji，不要表情包文字。",
        "6. 只输出评论正文这一行，不要任何解释或前后缀。",
    ]
    if avoid_texts:
        lines += [
            "",
            "【避免重复】下面这些是今天已经发过的评论，别再写成一样的意思：",
        ]
        for t in list(avoid_texts)[:3]:
            t = clean(t)[:40]
            if t:
                lines.append("- %s" % t)
    return "\n".join(lines)


def check_comment(text, avoid_texts=()):
    """返回问题列表（空列表 = 通过）。纯函数，可单测。

    检查项：长度区间 / 空话套话 / 硬刚语气 / 表情包 / 与已发评论重复 / AI 味。
    """
    t = clean(text)
    issues = []
    if not t:
        return ["评论为空"]
    n = len(t)
    if n < COMMENT_MIN_CHARS:
        issues.append("太短（%d 字 < 平台下限 %d 字）" % (n, COMMENT_MIN_CHARS))
    if n > COMMENT_MAX_CHARS:
        issues.append("太长（%d 字 > 上限 %d 字）" % (n, COMMENT_MAX_CHARS))
    for phrase in _FORBIDDEN:
        if phrase in t:
            issues.append("套话：「%s」" % phrase)
    for phrase in _CONFRONTATIONAL:
        if phrase in t:
            issues.append("语气硬：「%s」" % phrase)
    if re.search(r"\[[^\]]{1,6}\]", t):
        issues.append("出现了表情包文字")
    for old in avoid_texts or ():
        if _too_similar(t, old):
            issues.append("与今天已发的评论太像")
            break
    try:
        from core.detectors import check_ai_flavor, flavor_verdict
        got = check_ai_flavor(t)
        if got:
            _metrics, score = got
            if score >= 45:
                issues.append("AI 味%s（%d 分）" % (flavor_verdict(score), score))
    except Exception:                     # noqa: BLE001 检测器不可用不该阻断
        pass
    return issues


def _too_similar(a, b):
    """两条评论是否太像：归一化后一方包含另一方，或前 8 字相同。"""
    x, y = clean(a), clean(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if len(x) >= 8 and len(y) >= 8 and (x in y or y in x):
        return True
    return x[:8] == y[:8]


def extract_comment(text):
    """模型输出 → 干净的评论文本（借用回复模块的剥壳逻辑，避免两套实现）。"""
    return rp.strip_wrapping(text)


def compose(ask, story_title, story_text, avoid_texts=(), max_retry=2,
            progress=None):
    """生成一条合格评论：写 → 校验 → 带原因重写。

    ask: 可注入的提问函数 ask(prompt) -> 模型输出（便于脱网单测）。
    返回 {ok, comment, issues}。空输出会换一轮新提问（网页版驱动偶发读空）。
    """
    prompt = build_prompt(story_title, story_text, avoid_texts=avoid_texts)
    comment, issues = "", []
    first = True
    budget = max(1, int(max_retry) + 1) + 1        # 多留一次给「读回空内容」
    for attempt in range(budget):
        try:
            raw = ask(prompt, reuse_session=not first)
        except TypeError:               # 测试里的简单替身只接受一个参数
            raw = ask(prompt)
        except Exception as exc:        # noqa: BLE001 提问失败不该抛给调用方
            return {"ok": False, "comment": "", "issues": ["提问失败：%s" % exc]}
        first = False
        comment = extract_comment(raw)
        if not comment and attempt + 1 < budget:
            if progress:
                progress("评论兜底：第 %d 次没读回内容，重新提问" % (attempt + 1))
            first = True
            continue
        issues = check_comment(comment, avoid_texts=avoid_texts)
        if not issues:
            return {"ok": True, "comment": comment, "issues": []}
        if progress:
            progress("评论兜底：第 %d 版不达标（%s），重写"
                     % (attempt + 1, "；".join(issues)))
        prompt = (build_prompt(story_title, story_text, avoid_texts=avoid_texts)
                  + "\n\n【上一版的问题】%s\n请按上面的要求重写一条，"
                    "不要重复上一版的写法。" % "；".join(issues))
    return {"ok": False, "comment": comment, "issues": issues}
