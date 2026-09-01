# ============================================================
# story_prompt.py — 故事 prompt 构建（由 llm_api.py 拆分，2026-08）
#
# 职责：把 素材（参考回答/配方/元知识/作者签名）渲染成
#       故事生成的完整 user prompt。不发起任何 API 调用。
#
# 架构位置：Layer 0 (Tools) — 被 story_generation / workflows 共享。
#
# 提示词本体在 applications/zhihu_story/prompts.py（知乎域内容，
# 平台抽象轮再收敛）。
# ============================================================

import logging

log = logging.getLogger(__name__)

# 最高优先级约束：问题的原始要求凌驾于一切写作模板之上。作为公共约束
# 追加在所有模式 prompt 之前（位置靠前、醒目），防止"模板硬规则把题目
# 需求顶掉"——当题目要求与本 prompt 冲突时，一律以题目为准。
QUESTION_FIRST_RULE = """

## 最高优先级：以「知乎问题」的原始要求为准

先读透上文给出的「知乎问题」原始要求，并严格遵守：

- 问题里写明的任何约束（题材、人称视角、篇幅、结局走向、人物设定、语气基调等）都是最高优先级，不可被本 prompt 的任何写作模板覆盖。
- 当本 prompt 的写作要求与问题要求相冲突时，一律以问题要求为准，本 prompt 中相冲突的条目自动让步、不再生效。
- 问题要求了什么就必须写什么；问题没限制的地方，再套用本 prompt 的写作规范。"""


# 命名约束节：模型训练先验里网文高频男主名（沈砚/林屿/顾言…）权重极高，
# 生成故事时经常整套复用，读者一眼判定 AI 生成。作为公共约束追加到
# 所有模式的 prompt 末尾（位置靠后、醒目，模型更容易遵守）。
NAMING_CONSTRAINT = """

## 主人公命名要求

避免使用网文高频男主名（如沈砚、林屿、顾言、沈辞等）。
名字要生活化、符合人物时代与身份背景或故事隐喻：
如现代都市可用朴素常见的名字，古风可参考历史真实人名风格。"""


# 行文去AI味守则：AI 生成中文故事的高频"机器味"集中在万能连接词、整齐排比、
# 抽象形容词与机械句式。作为公共约束追加到所有模式 prompt 末尾（紧跟命名约束），
# 让"读起来像人写的"成为与格式同等重要的硬要求。
DEAI_STYLE_RULE = """

## 行文去AI味守则（读起来要像人写的，而不是AI写的）

语言层面：
- 禁用万能连接词与套话：然而、因此、与此同时、总而言之、不可否认、
  在这个X的时代、让我们、不禁让人感叹、意味深长地、仿佛在诉说着什么
- 禁止整齐排比三连："不是……而是……"、连续三个同构分句的炫技排比；
  需要对称时拆成不同长度的分句，打散节奏
- 少用抽象形容词与情绪标签：深深地、巨大的、默默地、瞬间、终于；
  换成具体动作、物件、声音、气味（"他很愤怒"→写他摔了茶杯，茶水泼了一地）
- 句式长短错落：一句话超过40字必须拆；连续三句以上长句后插一句短句；
  对话允许打断、抢白、只说一半，别让每个人都把话说完
- 每段必须有信息增量；删除纯过渡句与"结论句"；不总结、不点题、不升华

## 量化克制守则（防"数字堆砌"——AI 的高辨识度毛病）

人类作者用数字是有功能的（日期、年龄、金额、型号、编号），AI 则常把数字当"具体的伪装"到处堆。请按下面的标准写：

- 数字必须服务于叙事：日期、年龄、金额、时间、数量只有在承载信息/情绪/情节时才写；禁止为"显得具体"而罗列数量
- 禁止无信息增量的数量清单：一段里连续报数（"一个、两个、三个""两次、三次、四次"）必须合并或删掉
- 单句内量化表达不超过 1 处；一句里出现 2 个及以上数字/量词要拆开重写
- 能用感官/动作/模糊表达代替精确计数就代替：
  "桌上摆着四只盘子" → "桌上摆着几只盘子，边沿还沾着油星"
  "她发了三十七条消息" → "她发了一串消息，最后一条没头没尾"
  "他今年四十二岁" → "他眼角有了褶子，头发灰白一片"
- 保留叙事必需数字：日期（"4 月 4 号"）、年龄、金额、手机型号、编号小节——这些是"世界的细节"，不是堆砌
- 一句话里不要同时出现多个数量单位（"三千二百块，买了四十七斤，吃了一个月"要拆散）

## 人物命名与出场守则（防"人名轰炸"——开头一堆名字让读者读不下去）

人类作者让人物"缓出"：主角必要时早点有名，其他人先用身份/关系/称呼顶着，剧情需要时才点名。请按下面的标准写：

- 【硬性·最高优先级】开头前 5 段内（约引言及开头 300-500 字）不得出现任何虚构人物的全名（真名）：人物一律先用代词/身份/关系/特征称呼顶着（"他""我""男友""房东""值班医生""那个穿黑卫衣的少年"）；实在需要称呼就用"姓氏+称谓"（王先生、李主任）而非全名；真名一律推迟到第 6 段之后、剧情真正需要时再点出
- 全篇开头 ~1500 字内真名总数 ≤3 个（含第 6 段之后出现的）；其他人用身份/关系/特征称呼（他男友、闺蜜、房东、保安、那个穿黑卫衣的少年、值班医生、她妈）
- 边缘角色不点名：只出现一两场的角色用属性称呼（"王先生"这类姓氏+称谓即可），不要给每个路过的人起全名
- 人名缓出且带介绍：首次出现要配套"他是谁"（"他叫陈家祠，是陆洲的室友"），禁止裸奔式点名
- 禁止首段人名大礼包：不要让三五个角色在前几段全部全名出场；把次要角色的名字推迟到他们真正介入剧情的段落
- 叙述起步优先用代词/身份（"他""她""我""男友""医生"），读者代入感反而更强；人名是记忆锚点，不是开场清单
- 可用称呼制造人物层次：长辈叫"我妈/我爸/外婆"，权威用职称（"主任""校长"），陌生人用特征（"金发男""戴眼镜的女生"）

## 环境与场景描写守则（环境是道具不是装饰画——防"死描写"）




人类作者写景很少单纯写景；AI 则爱在中段过渡、场景切换处放纵惰性空镜（"窗外的雨淅淅沥沥""房间里光线昏暗""空气中弥漫着花香"）。请按下面的检查清单写：

- 每处环境描写先回答"这段风景在为谁服务？"——人物情绪 / 身份处境 / 剧情伏笔 / 氛围反衬。答不出来的描写删掉
- 禁止空镜开场：开头第一段必须有人或有事（动作/对话/事件），禁止先写一个房间、一条街、一场雨再进入人物
- 禁止三无铺陈：无人物、无动作、无对话的纯景物句，一段最多 1 句；连续两句以上纯景物清场必须合并或砍掉
- 用"人的动作带景"替代纯景物描写："她站在窗前，雨把玻璃打花"（有动作有情绪）✓；"窗外的雨淅淅沥沥地下着"（空镜）✗
- 环境细节只留会被记住的那 1-2 个：光线、气味、温度、一个反常物件；不要全景扫描（阳光+窗帘+桌角+墙纸+地板五件套）
- 情绪化天气守则：人物悲伤≠必须下雨，重逢≠必须黄昏。用反讽天气（好事发生在雨天、分手发生在晴天）制造落差，避免对号入座式借景

开头与结尾：
- 第一行直接进入动作或对话，不要环境铺垫式开头（不许"窗外雨声淅沥"起笔）
- 结尾停在画面或悬念上，禁止"这件事让我明白……"式升华收尾

中文 AI 高频句式（全篇从严控制）：
- 关联句式"一旦……就 / 只有……才 / 无论……都 / 随着……的 / 正是因为……所以 / 通过……来"
  全篇合计不超过 2 处，能直说就直说
- 揭露式比喻（遮羞布/面具/画皮/伪装/外衣/幌子/烟幕弹；撕下/戳穿/揭开 + 面具/真面目/本质）禁止使用
- 极值判断（"最……的地方在于 / 真正……的是 / 更……的是 / ……之处在于"）全篇不超过 1 处
- 比喻义抽象词（噪音/底色/滤镜/解药/拼图/镜像/缩影/棱镜/窗口/投影）能少用就少用
- 否定式排比"不仅仅是……而是……"、三段并列堆叠要拆开
- 删掉"金句"：读起来像名言警句、能单独摘出来的句子，一律重写成随口说出的样子"""

# ============================================================
# 发布前自检（与 core.story_text.validate_story_format 扣分点一一对应）
# 生成结束前自查一遍：任何一项不满足都会在格式检测被扣分重试（8/29
# 复盘：引言缺失/量化堆砌/环境空镜/章节不足是废稿与重试的主要来源）。
# ============================================================
FORMAT_SELF_CHECK_RULE = """
## 发布前自检（收尾前逐条核对，全部满足再输出）

1. 引言：正文第一行必须直接是故事正文（悬念/反差/钩子开头），
   绝不能是章节标题（如 `## **1**`）、分割线，也不要写"引言/引子"标签；
   第一行若直接是章节标题即判缺少引言、整篇不合格（一票否决）。
   引言 3-8 句、60-300 字，先抛冲突再展开。
2. 章节：全篇用 "## **N**" 分节（N 为 1、2、3...），至少 6 节，
   每节 500-800 字；节内用短段落（单段不超过 150 字）。
3. 量化克制：不要堆数字（一年、三百六十五天式换算罗列禁止）；
   全文量化表达密度接近人类作者（中文约 8-11 处/千字）。
4. 环境空镜：场景描写必须带人物动作/情绪，禁止连续景物清场段；
   开头 5 段内不要出现"纯环境开场"。
5. 人名缓出：开头前 5 段内（约引言及开头 300-500 字）不得出现任何虚构人物的全名（真名），
   一律用代词/身份/关系/特征称呼顶着；真名推迟到第 6 段之后。
6. 对话句式：对话一律用中文引号 "" 括起，语气口语化；
   不要以"好的/收到/以下是"等 AI 废话开头。
7. 篇幅：全篇达到 4000 字上下（不足 4000 字会被扣分）。
"""



def _resolve_meta_content(meta_knowledge, recipe):
    """
    解析要注入的元知识内容（全量注入；分层检索随 meta_learner
    P5 归档移除）。

    返回：
        (meta_text, was_retrieved): 元知识文本 和 是否实际做了检索
    """
    if not meta_knowledge or not str(meta_knowledge).strip():
        return "", False
    return str(meta_knowledge).strip(), False


def _render_retry_feedback(feedback):
    """把「重试反馈」（历次失败原因列表）渲染成修正要求段，追加到 prompt 末尾。

    带反馈的重试让模型知道上一版哪里不合格（太短/章节不足/长段太多/引号
    残留），收敛率远高于同 prompt 盲目重试。渲染模板与 prompts.py 的
    硬性要求保持一致（≥6 节、≥4000 字、句号后换行等）。
    """
    reasons = list(feedback) if isinstance(feedback, (list, tuple)) else [feedback]
    lines = [
        "",
        "## ⚠ 上一版不符合发布要求，请立刻修正（最重要）",
        "你上一版未通过格式校验，这次必须严格满足以下硬性指标，否则仍不合格：",
    ]
    for r in reasons:
        lines.append(f"- {r}")
    lines += [
        "- 正文最开头必须先有一段引言正文（3-8 句、60-300 字，悬念/钩子开头），"
        "第一行绝不能直接是章节标题 `## **N**`。",
        "- 章节标题必须用 `## **N**`，且 **不少于 6 节**；总字数 **不少于 4000 字**。",
        "- 每个句号/问号/感叹号后换行并空一行，长段落占比尽可能低。",
        "- 对话引号统一用「」，省略号用 ……（六个点），不出现直引号或 AI 废话前缀。",
        "请重新完整创作一篇全新的故事，不要解释，直接输出正文。",
    ]
    return "\n".join(lines) + "\n"


def build_story_prompt(question_title, reference_answer=None, recipe=None,
                       meta_knowledge=None, author_profile=None,
                       feedback=None):
    """
    根据 STORY_MATERIAL_MODE 构建故事生成 prompt。

    四种模式：
      - "sample"               参考文章开头截取（默认）：前 3000 字直接注入，零 LLM 提炼
      - "recipe"               配方驱动（从当前文章提炼配方，不附参考原文）
      - "reference"            参考文章模式（整篇注入，旧逻辑）
      - "recipe_and_reference" 配方 + 参考文章结合

    参数：
        question_title:    问题标题
        reference_answer:  参考回答文本
        recipe:            配方 dict（包含 hook/conflict/... 字段）
        meta_knowledge:    跨任务积累的元知识文本（可选）。
                          若 STORY_RECIPE_PROMPT 内含 {meta_knowledge} 占位符，
                          会直接填入；否则作为一个独立的"心法节"追加到 prompt 末尾。
        author_profile:    作者技能签名 dict（author_profiler.load_author_profile
                          的返回）。非 None 时把风格签名渲染为独立节追加到 prompt
                          末尾（generate_story 的 author= 参数会自动加载）。
        feedback:          重试修正反馈（可选）。str 或 str 列表，是上一版故事的
                          失败原因；非空时在 prompt 末尾渲染成「必须修正」段，
                          供模型针对性重写。

    返回：(user_message, mode_str)
    """
    from applications.zhihu_story.prompts import STORY_SYSTEM_PROMPT

    from config.story import STORY_MATERIAL_MODE

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
            from applications.zhihu_story.prompts import META_STORY_INJECT_SECTION
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
        from applications.zhihu_story.prompts import STORY_RECIPE_PROMPT
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
        from applications.zhihu_story.prompts import STORY_RECIPE_PROMPT
        ref_section = (
            "\n## 参考文章\n\n"
            "以下\"高赞文章\"重点学习其\"开头引入\"的手法与风格（第一句如何抛钩子、"
            "用什么视角/语气引入人物与事件）；其余感受其语感、节奏和氛围即可。\n"
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

    # === 模式3：参考文章采样（默认：开头 3000 字截取注入，零 LLM 提炼） ===
    elif STORY_MATERIAL_MODE == "sample":
        from core.story_text import sample_reference_sections
        sample = sample_reference_sections(reference_answer) \
            if reference_answer else ""
        system_body = STORY_SYSTEM_PROMPT
        injected = False
        if _has_meta:
            system_body = system_body + _meta_section_for_append
            injected = True
        meta_tag = " +心法" if injected else ""
        if sample:
            user_message = f"""{system_body}

## 全新文章主题（知乎问题）

{question_title}

## 参考文章（高赞回答开头——重点学习其"开头引入"的手法：第一句如何抛钩子、用什么视角/语气引入人物与事件；其余仅供感受语感与节奏，严禁借鉴情节）

{sample}

请根据以上要求，创作一个全新的故事。"""
            mode_str = f"采样模式{meta_tag}（参考{len(sample)}字）"
        else:
            # 无参考文章：仅基础要求 + 主题
            user_message = f"""{system_body}

## 全新文章主题（知乎问题）

{question_title}

请根据以上要求，创作一个全新的故事。"""
            mode_str = f"采样模式{meta_tag}（无参考文章）"

    # === 模式4：纯参考文章（旧逻辑 / 兜底） ===
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

以下是"高赞文章"（重点学习其"开头引入"的手法与风格，其余参考风格；严禁搬运情节）：
{reference_answer}

请根据以上内容，按照要求，开始创作全新的故事。"""
        meta_tag = " +心法" if injected else ""
        mode_str = f"参考文章模式{meta_tag}（{len(reference_answer or '')} 字符）"

    # === 风格签名注入（二选一：通用模板 或 具体作者签名，不再叠加） ===
    # 选中具体作者时只注入该作者签名，避免「通用模板 + 作者」两套规则
    # 混合（通用是跨作者模板、偏普通；叠加会让写出来的东西都一个味）。
    author_tag = ""
    if author_profile:
        try:
            from applications.zhihu_story.author_profiler import (
                render_style_section, render_general_section,
                load_general_profile)
            # 判断是否为「通用」模板：通用 profile 的 author 键为 "通用"
            # （内置文件即如此）；否则视为具体作者签名。
            is_general = author_profile.get("author") in (None, "", "通用")
            if is_general:
                # 只注入通用模板（用户选"通用"或未置空时）
                general = author_profile if author_profile.get("signature") \
                    else load_general_profile()
                general_section = render_general_section(general)
                if general_section:
                    user_message += general_section
                    author_tag = " +通用风格"
            else:
                # 只注入选中作者的签名，不再叠加通用模板
                user_message += render_style_section(author_profile)
                author_tag = f" +作者:{author_profile.get('author', '?')}"
        except Exception as e:
            log.warning(f"  [作者风格注入] 渲染失败，跳过：{e}")
    mode_str = mode_str + author_tag

    # === 问题优先 + 命名约束 + 行文去AI味守则 + 发布前自检（公共：所有模式生效） ===
    user_message += QUESTION_FIRST_RULE
    user_message += NAMING_CONSTRAINT
    user_message += DEAI_STYLE_RULE
    user_message += FORMAT_SELF_CHECK_RULE

    # === 重试修正反馈（如有：放在最末尾，最醒目，模型应先读到它） ===
    if feedback:
        user_message += _render_retry_feedback(feedback)

    return user_message, mode_str
