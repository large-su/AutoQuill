# -*- coding: utf-8 -*-
# ============================================================
# applications/zhihu_story/reply_prompts.py
# 评论回复：两套提示词（先挑人、再写话）+ 本地硬校验
#
# 用户口径（2026-09-27 语音原话整理）：
#   1) 先筛出最友善的一条（戾气大的直接忽略）；
#   2) 长度跟着对方走——对方 2 个字，回 100 个字肯定不行；
#   3) 不出现对方用户名，称呼只用「您 / 你」；
#   4) 友善、不硬刚；对方提建议就认真接住；
#   5) 要有人味，不能太有 AI 味；
#   6) 平台要求有效评论 ≥10 字，所以下限锁 10 字（对方极短时回 10–16 字口语）。
#
# 提示词 1 只喂「编号 + 正文」，**刻意不喂用户名**：一是减少身份偏见，
# 二是从源头避免模型把用户名写进回复里。
# ============================================================

import re

# 对方评论字数 → (回复下限, 回复上限)
# 上限刻意「向下压」：宁可短一点，也不要长篇大论（用户口径：尽可能少一点）
LENGTH_BANDS = (
    (4, 10, 16),
    (15, 10, 22),
    (40, 15, 46),
    (10 ** 9, 30, 64),
)

MIN_REPLY_CHARS = 10          # 平台有效评论下限（打卡任务也要求 ≥10 字）

# 套话/客服腔/AI 味重灾区：命中任何一条就重写
FORBIDDEN_PHRASES = (
    '感谢您的认可', '感谢你的认可', '感谢支持', '感谢您的支持', '感谢你的支持',
    '您的支持是我', '你的支持是我', '希望对你有帮助', '希望对你有所帮助',
    '希望能帮到你', '作为AI', '作为 AI', '首先', '其次', '最后一点', '总之',
    '综上所述', '不得不说', '值得一提的是', '您的关注', '感谢阅读',
    '不胜感激', '非常感谢您的', '谢谢您的宝贵', '欢迎交流', '共勉',
)

# 硬刚/阴阳怪气口吻：命中即重写（用户要求绝不硬刚）
CONFRONTATIONAL = (
    '你懂吗', '你自己看', '呵呵', '笑死', '无语', '你行你上', '杠',
    '看清楚', '别乱说', '你这', '不懂别', '莫名其妙', '搞笑',
)

# 回复里不该出现的东西（模型常见的「包装」）
_WRAP_CHARS = '「」“”\'\'《》【】*#`'


def clean_comment_text(text):
    '''评论正文归一化：剥零宽字符、压空白（判重与长度都用它）。'''
    t = str(text or '')
    t = re.sub('[\u200b-\u200d\ufeff]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def length_target(comment_text):
    '''按对方评论长度给出 (下限, 上限)，单位字符。'''
    n = len(clean_comment_text(comment_text))
    for limit, lo, hi in LENGTH_BANDS:
        if n <= limit:
            return (max(lo, MIN_REPLY_CHARS), max(hi, MIN_REPLY_CHARS))
    return (MIN_REPLY_CHARS, 64)


def length_hint(comment_text):
    lo, hi = length_target(comment_text)
    return '目标 %d–%d 字' % (lo, hi)


# ------------------------------------------------------------
# 提示词 1：挑出最友善、最适合友善回应的一条
# ------------------------------------------------------------
PICK_PROMPT = '''下面是读者留在我知乎回答下面的评论（只有编号和正文，没有用户名）。
请挑出**最友善、最适合被友善回应**的一条，只挑一条。

优先挑：真诚的夸奖、感谢、认真的提问、具体的建议（哪怕带着不同意见）。
直接排除：人身攻击、嘲讽挖苦、阴阳怪气、无端指责、情绪宣泄、纯表情符号、
广告或引流（例如「看我主页」「关注我」这类）。
如果一条评论既有情绪又有一点信息量，按「能不能友善地接住」来判断。

**只输出那一条的编号（一个数字）**：不要解释、不要标点、不要输出任何别的字。
如果这些评论里没有一条适合友善回应，只输出 0。

评论列表：
'''


def filter_comments(comments):
    '''过滤掉空评论，返回 [(编号, 归一化正文)]。

    ★ 编号只对「留下的」评论连续编号，且**提示词与调用方共用这一个函数**，
    这样模型回的编号能一对一映射回候选（早期版本用 enumerate 编号，
    跳过一个空评论就会整体错位）。
    '''
    out = []
    for c in comments or []:
        text = clean_comment_text(c.get('text') if isinstance(c, dict) else c)
        if text:
            out.append((len(out) + 1, text))
    return out


def build_pick_prompt(comments):
    '''comments: [{text}] 或 [str]；返回可直接发给模型的完整提示词。

    刻意不带用户名/主页链接：避免身份偏见，也避免模型把用户名写进回复。
    模型回的编号 → 候选：用 filter_comments(comments)[编号 - 1]。
    '''
    return PICK_PROMPT + chr(10).join(
        '%d. %s' % (n, t) for n, t in filter_comments(comments))


# ------------------------------------------------------------
# 提示词 2：写回复
# ------------------------------------------------------------
REPLY_PROMPT_TMPL = '''你是一个在知乎写故事的人，现在要回复读者留在我回答下面的评论。

【对方的评论】{comment}
【我当时回答的问题】{question}
【我当时写的回答（节选）】{answer}

写作要求（按重要性排序）：

1. **说人话**：像真人随手敲出来的，不写客服腔、不写总结腔、不用排比句。
   一律不要出现「感谢您的认可」「希望对你有帮助」「首先/其次/总之」「作为AI」
   这类套话。
2. **长度跟着对方走**：{length_hint}（对方共 {comment_len} 字）。
   可以更短，不要更长；平台要求有效评论至少 {min_chars} 字，所以最少 {min_chars} 字。
3. **不要提对方的名字、ID、主页**，称呼只用「您」或「你」。
4. **按对方的类型回应**：
   - 夸你：谢谢 + 一句自然的话，别谦虚过头，也别自夸；
   - 提问：直接回答；答不上来就说「这个我其实没细想过」；
   - 提建议/批评：先接住（承认问题），说清会怎么改，**绝不辩解、绝不反问**；
   - 说没看懂/看不下去：客气接住，不争论、不复述剧情；
   - 只是「蹲」「好看」这类短评：一句轻松的回应就够。
5. 不引战、不承诺、不谈敏感话题、不提站外信息、不写表情包文字（如 [微笑]）。

**只输出回复正文本身**：不要引号、不要署名、不要解释、不要 markdown、不要换行。
'''

ANSWER_EXCERPT_CHARS = 600      # 回答只喂开头这么多字：够模型知道写了什么，又不撑爆上下文


def build_reply_prompt(comment_text, question_title='', answer_text='',
                       answer_excerpt_chars=ANSWER_EXCERPT_CHARS):
    '''拼出「写回复」提示词。answer_text 会被截断，避免把整篇故事塞进去。'''
    comment = clean_comment_text(comment_text)
    answer = clean_comment_text(answer_text)
    if answer_excerpt_chars and len(answer) > answer_excerpt_chars:
        answer = answer[:answer_excerpt_chars] + '……（后略）'
    lo, hi = length_target(comment)
    return REPLY_PROMPT_TMPL.format(
        comment=comment,
        question=clean_comment_text(question_title) or '（未读到题目）',
        answer=answer or '（未读到正文）',
        length_hint='目标 %d–%d 字' % (lo, hi),
        comment_len=len(comment),
        min_chars=MIN_REPLY_CHARS,
    )


# ------------------------------------------------------------
# 本地硬校验（模型输出不达标 → 带原因重写）
# ------------------------------------------------------------
def strip_wrapping(text):
    '''剥掉模型爱加的外壳：引号、书名号、markdown 记号和首尾空白。'''
    t = str(text or '').strip()
    t = re.sub(r'^\s*[-*#>]+\s*', '', t)      # 行首 markdown 记号
    t = re.sub(r'\s*[*#`]+\s*$', '', t)        # 行尾 markdown 记号（**加粗** 收尾）
    t = t.strip()
    while len(t) >= 2 and t[0] in _WRAP_CHARS and t[-1] in _WRAP_CHARS:
        t = t[1:-1].strip()
    t = t.strip('"\'')
    return clean_comment_text(t)


def check_reply(reply, comment_text, author=''):
    '''返回问题列表（空列表 = 通过）。纯逻辑，可单测。

    检查项：长度区间 / 套话 / 硬刚语气 / 用户名 / 表情包文字 / AI 味分数。
    '''
    text = clean_comment_text(reply)
    issues = []
    if not text:
        return ['回复为空']
    lo, hi = length_target(comment_text)
    n = len(text)
    if n < MIN_REPLY_CHARS:
        issues.append('太短（%d 字 < 平台下限 %d 字）' % (n, MIN_REPLY_CHARS))
    elif n < lo:
        issues.append('偏短（%d 字 < 目标下限 %d 字）' % (n, lo))
    if n > hi:
        issues.append('偏长（%d 字 > 目标上限 %d 字）' % (n, hi))
    for phrase in FORBIDDEN_PHRASES:
        if phrase in text:
            issues.append('套话：「%s」' % phrase)
    for phrase in CONFRONTATIONAL:
        if phrase in text:
            issues.append('语气硬：「%s」' % phrase)
    if author:
        name = clean_comment_text(author)
        if len(name) >= 2 and name in text:
            issues.append('提到了对方用户名：%s' % name)
    if re.search(r'\[[^\]]{1,6}\]', text):
        issues.append('出现了表情包文字')
    try:
        from core.detectors import check_ai_flavor, flavor_verdict
        got = check_ai_flavor(text)
        if got:
            _metrics, score = got
            if score >= 45:
                issues.append('AI 味%s（%d 分）' % (flavor_verdict(score), score))
    except Exception:                     # noqa: BLE001
        pass
    return issues


def rewrite_feedback(issues):
    '''把校验问题转成给模型的「带反馈重写」指令（沿用项目里成熟的重试模式）。'''
    if not issues:
        return ''
    return ('上一条回复有以下问题，请重写一条：' + '；'.join(issues)
            + '。只输出回复正文本身。')
