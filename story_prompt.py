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
import re
import threading

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

- 【建议】开头前 5 段内尽量少用全名：优先用代词/身份/关系/特征称呼（"他""我""男友""房东""值班医生"），需要点名时用"姓氏+称谓"（王先生、李主任）。**开场要让读者一眼知道谁是谁**——该点名就点名，别为了规避名字把开场写成谜语；下面「开头 1500 字内真名 ≤3 个」这条才是要守的线
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

# 输出方式硬约束（2026-09-08 豆包实测根因修复）：
# 网页版豆包对长写作请求会走「写作/文档/工作任务交付」——对话里只留一句
# 引言（60-100 字），正文被放进独立交付界面，驱动读不到正文，整篇被判
# 「故事过短」连废 3 次。真实会话对照（同一账号）：
#   - 未加本约束：对话正文 86-91 字，页面出现 flow-product-card 交付卡片
#   - 加了本约束：豆包把 8022 字全文直接输出在对话里，无交付卡片
# 故作为公共约束追加到所有模式 prompt 末尾（API 模式同样无害：本就没有
# 卡片交付界面，模型只会照常直接输出正文）。
INLINE_OUTPUT_RULE = """

## 输出方式（硬性）

直接在对话里输出完整正文，一次性输出全文：

- 不要使用卡片 / 文档 / 工作任务 / 附件等交付界面，不要把正文放进任何
  需要另外打开才能看到正文的容器
- 不要任何前后缀说明：不要"好的/收到/以下是"开头，不要交代写作思路，
  不要结尾总结、点评或反问
- 不要复述本 prompt 的要求当正文（"我严格遵循…格式、字数、文风要求"
  "采用反差断语开篇""搭建 6+ 章节、先压后弹"这类自我汇报一律不许出现），
  第一行必须是故事正文本身
- 不要分多次输出、不要中途停下问"需要我继续吗"，正文必须一次写完"""


# ============================================================

# 开篇起手式多样化守则（2026-09-19）：真实产物统计——经典模式最近 22 篇里 20 篇，
# 最近 20 篇 100%，都以「我」字开头，且高度同构（"我撬开丈夫的抽屉" / "我把离婚协议
# 放在茶几上" / "我数了数…"）。根因不是模型能力，而是守则叠加把开头挤成了唯一解：
#   第一人称声口（本守则第 6 条 + 作者签名里的"第一人称内心OS"）
# + 引言一票否决（正文第一行必须直接是故事正文）
# + 禁止空镜开场 / 第一行直接进入动作或对话
# → 模型最省力的合规解就是「我 + 强动作/物件/数字」。参考素材本身只有 23% 是「我」
#   开头（1968 篇实测 42%），不是参考带偏的；纯净模式（不注入本守则）只有 33%。
# 本守则只约束"起手式"，不放宽任何格式门槛（引言仍必须第一行是正文）。
OPENING_VARIETY_RULE = """

## 开篇起手式多样化（硬性，优先级高于作者签名里的"第一人称"表述）

作者签名与上面的守则说的是"声口与人称偏好"，不是"每篇都必须用「我」字起手"。
同一账号连续产出的文章，开篇首句必须换着来：**不许把「我 + 强动作/物件/数字」
当成默认模板**（"我撬开丈夫的抽屉""我把离婚协议放在茶几上""我数了数……"都是同构）。

**首选：模仿本题最受认可那篇参考文章的起手方式**（见 prompt 末尾的「本篇开篇
起手方式」，那里指定了就照它写，只学手法、不抄句子）。没有参考文章时，从下面
几种起手式里挑一种，同一批产出不要连续重复同一种：

1. **对话起手**：第一句就是一句带引号的对话，人物身份下一句再交代。
   例：「你要是敢走，我就把这房子点了。」
2. **他人起手**：用他人（他/她/身份称呼）的动作或状态开场，主角随事件入画。
   例：她把离婚协议推过来的时候，手指在抖。
3. **物件起手**：从一个具体东西切入，用它带出冲突，同一句里必须有人或动作。
   例：床头柜上那只蓝边搪瓷碗缺了一角，他用了十年没换。
4. **时间/数字断语起手**：用时间点、次数或事实陈述开场，不要写成"我数了数"。
   例：第十七年，他还没喝过我送的那杯咖啡。
5. **反差断语起手**：先给一句反常识的判断，再用事件撑住它。
   例：这世上最狠的报复，是替一个人把烂摊子全背下来。

- 第一人称叙述本身允许保留（这是本赛道的常态），但**引言第一句不要以「我」字
  开头**；确实需要第一人称时，把人称放到第二句，第一句先给对话/他人/物件/时间。
- 引言其余要求不变：3-8 句、60-300 字，先抛冲突再展开；第一行仍然必须是故事正文
  （不得是章节标题、标签、分割线）。
- 每种起手式的第一段内都要出现人（我/他/她/身份称呼）或对话，避免被判"空镜开场"。"""

# 开篇事件化守则（2026-09-19 用户口径）：用户反馈"打开第一页，每个字我都认识，
# 但完全抓不住重点，也完全入不了戏；前五六句既没有情节推进，也没有冲突，
# 单纯在描述环境或情绪"。回看产物证实：引言被写成了"全书简介"——
# "所有人都说…没人知道…只有我清楚…这场…可惜晚了"——人设、身世、牺牲、结局
# 用评价句一次讲完，既没有场景也没有对话。
# 实测（2026-09-19 批次 19 篇 vs 采集参考 35 篇，都只看引言前 10 句）：
#   引言含对话比例 16% vs 100%；抽象评价句占比 0.30 vs 0.00；
#   具体名词密度 0.72 vs 2.40（每百字）。
# 根因：所有守则只管引言的"形式"（句数/字数/第一行不能是标题/不能空镜），
# 没有一条管引言的"内容"必须是一件正在发生的事——模型最省力的合规解
# 就是写一段评价句。本守则补上内容侧门槛。
# 质检对应 core/detectors.py::check_summary_opening（validate_story_format
# 第 8 项，与"引言缺失"同为否决项）。
OPENING_EVENT_RULE = """

## 开篇必须是"正在发生的事"，不是"故事简介"（硬性，与格式同等重要）

读者划到你的第一屏，只看得到前 5-6 句。这 5-6 句里必须**有人在做事**，
而不是你在向读者介绍"这是个什么故事"。

引言（前 3-8 句）要写成一个**微型场景**，三件事同时到位：

1. **有人**：句子里出现具体的人（我 / 他 / 她，或身份称呼：老公、班主任、
   房东、我妈、那个穿黑卫衣的少年）；
2. **有事**：这个人做了一件**看得见的动作**（推开、摔了、递过来、签了、
   跪下去、把手机屏幕转过来），或者**说了一句带引号的话**；
3. **有后果**：动作落在具体的人或东西上并当场起变化（茶水泼了一桌、手指
   划出血、满桌人安静下来、他把协议撕了）。

- **引言里至少要有 1 句带引号的对话**（对白、消息【】、喊话、电话都算）。
- **前 3 句之内**必须出现第 2 条：一个看得见的动作，或一句对话。
- 引言之后的第一节也从场景写起：可以回溯，但回溯的那一句本身也要是一个
  场景（"三天前我在他公司楼下等他下班"是场景；"事情要从三天前说起"不是）。

### 严禁的"总结体开头"（命中即重写引言，检测不过不发）

- **评价句开场**：所有人都…／没人知道…／只有我清楚…／从来…／终究…／
  到头来…／这场…／我以为…可…／更讽刺的是…／可惜…晚了。
  这些句子是在给读者**下结论**，不是在让事情**发生**。
- **性格自述开场**：我不会安慰人／我从不心软／我这人最讨厌…。读者还不认识
  这个人，先别给她贴标签，让她去做一件事。
- **一句话讲完一生**：引言里把身世、误会、牺牲、结局全交代掉，读者看完引言
  就没有往下读的理由了。
- **引言结尾的金句/升华**（"这世上的爱，不过是一场…"）。引言的任务是让事情
  **开始**，不是总结；金句、点题、升华一律不写。

### 写完引言后自检（把引言单独拎出来读一遍，答不上来就重写）

- **谁？** —— 说不出一个具体的人；
- **干了什么？** —— 说不出一个具体动作，或一句原话；
- **然后呢？** —— 读者不会想问"接下来怎么了"。

❌ 反例（全是介绍，没有一件事在发生）：
「我不会安慰人，不会心软。所有人都说我冷血寡情。情绪是最没用的累赘。」

✅ 正例（同样的第一人称、同样的篇幅，但每句都在发生事）：
「我妈让我捐肾。饭桌上，她把配型报告推到我面前。「小满，救救你弟。」
我拿起报告，上面写着配型成功。我放下筷子，说：「不捐。」全家安静了。」"""


# 起手式轮换表：按顺序分配给同一进程里连续生成的每一篇，保证一次运行内不重样。
# 每项 = (短标签, 给模型的硬要求)。
OPENING_STYLES = (
    ("对话起手",
     "引言第一句必须是一句带引号的对话（以「开头），且不得以「我」字开头；"
     "说话人身份放到第二句再交代。"),
    ("他人起手",
     "引言第一句必须以他人（他/她/身份称呼）的动作或状态开场，主角在第二、三句入画；"
     "第一句不得以「我」字开头。"),
    ("物件起手",
     "引言第一句必须从一个具体物件切入（一件东西、一张纸、一只碗、一份病历…），"
     "同一句里就要有人或动作，不得以「我」字开头。"),
    ("时间/数字断语起手",
     "引言第一句用时间点、次数或事实陈述开场（如「第十七年，…」），"
     "不要写成「我数了数」这类以「我」起手的句式。"),
    ("反差断语起手",
     "引言第一句先给一句反常识的判断或结论（不许用「人这一生」「这世上」开头的空话）。"
     "★ 判断句最多 1 句：第二句必须立刻落到一个看得见的动作或一句带引号的对话上，"
     "第三句给出后果——只有判断没有事件就是总结体开头，会被判不合格；"
     "不得以「我」字开头。"),
)

_OPENING_LOCK = threading.Lock()
_OPENING_CURSOR = 0


def next_opening_style():
    """取下一个起手式（进程内轮换，多线程安全）。

    一次运行里的每篇故事都调用一次 → 第 1 篇对话起手、第 2 篇他人起手……
    到末尾回环，保证同一批产出不重样（各篇之间互不可见，只能靠外部轮换）。
    """
    global _OPENING_CURSOR
    with _OPENING_LOCK:
        style = OPENING_STYLES[_OPENING_CURSOR % len(OPENING_STYLES)]
        _OPENING_CURSOR += 1
    return style


def peek_opening_cursor():
    """当前轮换位置（只读，测试/日志用）。"""
    with _OPENING_LOCK:
        return _OPENING_CURSOR


def reset_opening_cursor():
    """把轮换位置复位（测试用）。"""
    global _OPENING_CURSOR
    with _OPENING_LOCK:
        _OPENING_CURSOR = 0


def render_opening_instruction(style):
    """把指定的起手式渲染成本篇的硬要求块（prompt 末尾、醒目位置）。"""
    if not style:
        return ""
    label, requirement = style
    return ("\n\n## 本篇指定的开篇起手式（硬性，与作者签名的第一人称声口不冲突）\n\n"
            "- 起手式：**%s**\n"
            "- %s\n"
            "- 第一句必须是故事正文（不得是章节标题/标签/分割线），引言 3-8 句、60-300 字。\n"
            "- ★ 引言必须是一个正在发生的场景：至少 1 句带引号的对话，前 3 句内出现\n"
            "  一个看得见的动作；起手式只管第一句怎么写，不许把引言写成故事简介。\n"
            "- 先满足本条，再谈其余风格；作者签名约束的是声口与节奏，不是「每篇我起手」。") % (
                label, requirement)


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
   ★ 开篇内容：引言必须是一个"正在发生的微型场景"（有人 + 有看得见的动作或
   对话 + 有后果），至少 1 句带引号的对话、前 3 句内出现动作或对话；
   禁止"所有人都…／没人知道…／只有我清楚…"式评价句开场、性格自述开场、
   把整条故事线讲完的简介式引言、以及引言结尾的金句升华（见「开篇事件化守则」）。
   ★ 起手式：按 prompt 末尾「本篇指定的开篇起手式」写，引言第一句不得默认
   落回「我 + 强动作/物件/数字」的老模板（同一账号篇篇这样开头，读者一眼看出是机器批量产的）。
2. 章节：全篇用 "## **N**" 分节（N 为 1、2、3...），至少 6 节，
   每节不少于 500 字（**不设上限**，情节需要就写长）；节内用短段落（单段不超过 150 字）。
3. 量化克制：不要堆数字（一年、三百六十五天式换算罗列禁止）；
   全文量化表达密度接近人类作者（中文约 8-11 处/千字）。
4. 环境空镜：场景描写必须带人物动作/情绪，禁止连续景物清场段；
   开头 5 段内不要出现"纯环境开场"。
5. 人名缓出（建议级）：开头 1500 字内真名 ≤3 个，次要人物用身份/关系称呼；
   不必为了规避全名把开场写成谜语——第一句该点名就点名，让读者立刻知道谁是谁。
6. 对话句式：对话一律用中文引号 "" 括起，语气口语化；
   不要以"好的/收到/以下是"等 AI 废话开头。
7. 篇幅：全篇**不少于 4000 字**，鼓励写到 **5000-7000 字**。
   字数只有下限、没有上限：写长不扣分，只扣注水/重复/复述；
   低于 4000 字才扣分。★ 本账号效果最好的两篇是 5709 / 4560 字。
"""


# ============================================================
# 纯净模式 prompt（工作台 · 完整链路）：刻意去限制
# ============================================================
# 设计：不注入格式硬校验/章节/字数/命名/去AI味等守则——这些压住了
# 大模型自身能力。只保留三件事：给定题目、学习高赞回答风格、
# 严禁抄袭与洗稿（原创由 core/originality.py 的审核环节兜底）。
CLEAN_SYSTEM_PROMPT = """你是一位知乎答主。请针对给定的「知乎问题」撰写一篇全新的回答。

## 要求

1. 学习参考高赞回答的风格：它的语气口吻、开头写法、叙事节奏、句子长短、
   段落组织方式——把这些风格特点自然地用到你的新回答里（风格可以像，内容必须新）。
2. 内容必须完全原创：情节、人物、经历、设定、台词都必须是你全新构思的。
3. 严禁抄袭：不得复制参考回答的任何句子、段落，不得照搬其情节、人物、台词、设定。
4. 严禁洗稿：不得把参考回答"换皮重写"（情节主线、人物关系、关键事件、结构顺序
   照搬只是换了表述），洗稿同样违规。
5. 段落长度向参考回答看齐：按下方「参考回答的段落特征」分段，段落不要太长，
   分段习惯贴近参考回答（它是短句成段你就写短段，它长段铺陈才允许长段）。
6. 直接输出回答正文，不要标题、不要前言、不要解释。"""




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
        "- 引言必须是正在发生的场景，不是故事简介：至少 1 句带引号的对话，"
        "前 3 句内出现一个看得见的动作；禁止「所有人都…／没人知道…／只有我…」"
        "式评价句开场、性格自述开场，也不要在引言里把整条故事线讲完。",
        "- 章节标题必须用 `## **N**`，且 **不少于 6 节**；总字数 **不少于 4000 字**"
        "（鼓励 5000-7000 字，每节不设上限；写长不扣分）。",
        "- 每个句号/问号/感叹号后换行并空一行，长段落占比尽可能低。",
        "- 对话引号统一用「」，省略号用 ……（六个点），不出现直引号或 AI 废话前缀。",
        "- 直接在对话里输出完整正文，不要使用卡片 / 文档 / 任务交付界面"
        "（上一版正文若被放进交付界面，这次必须把全文写在对话里）。",
        "请重新完整创作一篇全新的故事，不要解释，直接输出正文。",
    ]
    return "\n".join(lines) + "\n"



# ---- 参考文章起手方式（2026-09-19 用户口径）----
# 不要固定循环：这个话题下大家最认可的那篇参考文章怎么起手，我们就模仿它的起手方式，
# 只是不能抄袭、要控制度。因此：有参考文章 → 模仿其起手方式；没有参考（或参考不可用）
# → 才退回轮换表兜底。抄袭的度由 core.originality.opening_copy_signals 在生成后兜底拦截
# （故事引言与参考开头连续重合达到阈值 → 判抄、带反馈重写）。
_OPENING_ENV_WORDS = ("窗外", "窗台", "阳光", "月光", "灯光", "夜色", "雨", "风",
                      "街道", "街头", "巷子", "院子", "屋子", "房间", "客厅",
                      "走廊", "远处", "空气", "光线", "天边", "云")
# 参考首句若是"情境/事件陈述"（人物关系 + 当场事件）就单列一类：这类首句既不是
# 「我」起手，也不含物件量词，早先会被误判成 judgment（判断/反差断语起手），
# 于是 prompt 要求模型"先给一句反常识的判断"——正是总结体开头的来源之一。
_REF_EVENT_ANCHORS = (
    "老公", "男友", "前男友", "女友", "前女友", "丈夫", "妻子", "老婆", "未婚夫",
    "未婚妻", "新娘", "新郎", "妈", "爸", "儿子", "女儿", "妹妹", "姐姐", "哥哥",
    "弟弟", "婆婆", "公公", "外婆", "奶奶", "爷爷", "班主任", "老师", "老板",
    "同事", "同学", "同桌", "室友", "房东", "邻居", "医生", "护士", "警察",
    "律师", "初恋", "白月光", "相亲", "婚礼", "订婚", "离婚",
)
_OPENING_OBJECT_RE = re.compile(
    # 量词前必须有指示/数量词：否则"把/件/条"这些兼作介词/量词的字会把
    # 判断句误判成物件起手（"是替一个人把烂摊子全背下来"曾被当成物件起手）
    r"(?:那|这|一|两|三|几)\s*(?:张|只|份|把|条|枚|本|支|瓶|碗|件|双|页|封|串)"
    r"|(?:照片|病历|协议|车票|戒指|钥匙|信|合同|收据|手机|日记|搪瓷碗)")

_REF_OPENING_TECHNIQUES = {
    "dialogue": ("对话起手",
                 "第一句就是一句带引号的对话，说话人身份放到第二句再交代"),
    "first_person": ("第一人称起手（参考本身就是「我」开头）",
                     "可以沿用第一人称，但必须换句式与切入口：不要写成「我+动词+具体物件」的清单式模板，也要换掉具体事件、道具与数字"),
    "other_person": ("他人起手",
                     "第一句用他人（他/她/身份称呼）的动作或状态开场，主角随后入画"),
    "object": ("物件起手",
               "第一句从一个具体物件切入，同一句里就有人或动作"),
    "time_number": ("时间/数字断语起手",
                    "第一句用时间点、次数或事实陈述开场，第二句立刻给事件"),
    "scene": ("场景起手（必须带人）",
              "第一句可以给场景，但同一句里必须有人或动作，不许纯景物空镜"),
    "event_statement": ("情境/事件陈述起手",
                       "第一句直接陈述一个具体情境或事件（谁和谁是什么关系、当场发生了什么），"
                       "第二句立刻给一句带引号的对话或一个看得见的动作，第三句给后果；"
                       "严禁把这个情境写成评价句或主题句"),
    "judgment": ("判断/反差断语起手",
                 "第一句先给一句反常识的判断或结论（最多 1 句），第二句立刻落到一个"
                 "看得见的动作或一句带引号的对话上，第三句给出后果"),
}


def _first_sentence(text):
    """取参考回答的第一个非空句（去掉标题井号/引用符号）。"""
    for line in str(text or "").split(chr(10)):
        s = line.strip().lstrip("#>*- ").strip()
        if not s:
            continue
        parts = re.split(r"[。！？!?...]", s)
        return parts[0].strip() if parts else ""
    return ""


def analyze_reference_opening(reference_answer):
    """识别参考回答（本题最受认可的高赞文章）的起手方式（纯本地启发式，零 LLM 调用）。

    返回 {label, key, technique, sample, cliche_tail}；无参考返回 None。
    cliche_tail=True 表示参考首句本身就是「我+强动作」这类批量感模板——
    此时指令会额外要求换句式，避免把同质化再学一遍。
    """
    first = _first_sentence(reference_answer)
    if not first:
        return None
    head = first[:40]
    if head[:1] in "「『“‘":
        key = "dialogue"
    elif re.match(r"^(?:我|我们|咱)", head):
        key = "first_person"
    elif re.match(r"^(?:他|她|他们|她们|它)", head):
        key = "other_person"
    elif re.match(r"^第?\s*(?:那|这)?[0-9一二三四五六七八九十百千万两]+\s*[年月日天次岁遍周]", head) \
            or re.match(r"^(?:那|这)(?:天|年|月|日|晚|次)", head) \
            or re.match(r"^[0-9]{2,4}\s*[年月日]", head):
        key = "time_number"
    elif _OPENING_OBJECT_RE.search(head):
        key = "object"
    elif any(w in head for w in _OPENING_ENV_WORDS) \
            and not re.search(r"[我你他她]", head):
        key = "scene"
    elif any(w in head for w in _REF_EVENT_ANCHORS):
        # 2026-09-19 补：参考首句常常是"情境陈述"（"儿子被请家长，班主任是前男友。"），
        # 既不是「我」起手也没有物件量词，早先一律落到 judgment 分支 → 模型被要求
        # "先给一句反常识的判断"，直接把引言写成评价句。这里单列一类，要求它落地成事件。
        key = "event_statement"
    else:
        key = "judgment"
    label, technique = _REF_OPENING_TECHNIQUES[key]
    # 参考首句就是「我…」起手：允许沿用第一人称，但必须换句式与切入口，
    # 否则会把「篇篇我起手」的同质化原样学回来
    cliche_tail = bool(key == "first_person")
    return {"label": label, "key": key, "technique": technique,
            "sample": first[:60], "cliche_tail": cliche_tail}


def render_reference_opening_instruction(info):
    """把「模仿参考起手方式」渲染成本篇硬要求（放在 prompt 末尾、醒目位置）。"""
    if not info:
        return ""
    lines = [
        "", "",
        "## 本篇开篇起手方式（模仿参考文章的手法，不抄它的句子）", "",
        "- 参考文章（本题最受认可的高赞回答）的起手方式：**%s**" % info["label"],
        "- 它的原句：%s（只作手法示例，不得复用）" % info["sample"],
        "- 具体写法：%s" % info["technique"],
    ]
    if info.get("cliche_tail"):
        lines.append(
            "- 注意：参考首句是「我…」起手。整句照学容易又写成一篇同构文章——"
            "第一人称可以保留，但请换句式与切入口（对话/他人动作/物件特写/"
            "时间断语任选一种），不要写成「我+动词+具体物件」的清单式开场。")
    lines += [
        "- 抄袭红线：内容、人物、场景、道具、事件、措辞全部换新；与参考首句不得有",
        "  10 字以上连续重合，也不许同义替换（换词不换骨架同样算抄）。",
        "- 格式底线不变：第一行必须直接是故事正文（不得是章节标题/标签/分割线），",
        "  引言 3-8 句、60-300 字，先抛冲突再展开。",
        "- ★ 起手式只决定第一句怎么写：引言整体仍必须是一个正在发生的场景",
        "  （至少 1 句带引号的对话 + 前 3 句内一个看得见的动作），",
        "  不许用判断句/评价句把引言写成故事简介。",
        "- 若这种起手方式与本题不合（如参考是对话起手但你的题开场不适合对话），",
        "  可换成对话/他人/物件/时间·数字/反差断语任一种，但不要默认用",
        "  「我+强动作/物件/数字」。",
    ]
    return chr(10).join(lines) + chr(10)


def build_story_prompt(question_title, reference_answer=None, recipe=None,
                       meta_knowledge=None, author_profile=None,
                       feedback=None, opening_style=None, opening_auto=True):
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
        opening_style:     指定本篇的开篇起手式（OPENING_STYLES 里的一项）；
                          默认 None = 按轮换自动取下一个（防篇篇「我」字起手）。
        opening_auto:      False 时两种自动选择都不注入（单测/特殊场景用）。

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

    # === 开篇起手式多样化（公共：经典/常规链路生效；纯净模式刻意不注入） ===
    # 真实产物统计（2026-09-19）：经典模式最近 22 篇里 20 篇以「我」字开头，
    # 最近 20 篇 100% 同构（我+强动作/物件/数字）。守则叠加把开头挤成了唯一解，
    # 这里按篇轮换指定起手式——各篇生成互相看不见，只能靠外部轮换保证不重样。
    try:
        from config.story import OPENING_MIRROR_REFERENCE, OPENING_VARIETY
    except Exception:      # 配置缺失时按开启处理（防回归成同构）
        OPENING_MIRROR_REFERENCE, OPENING_VARIETY = True, True
    _opening_block = ""
    _opening_tag = ""
    if opening_style:                       # 显式指定（测试/特殊场景）最优先
        _opening_block = render_opening_instruction(opening_style)
        _opening_tag = opening_style[0]
    else:
        _ref_opening = (analyze_reference_opening(reference_answer)
                        if (OPENING_MIRROR_REFERENCE and opening_auto)
                        else None)
        if _ref_opening:
            # ① 用户口径：模仿本题最受认可那篇参考文章的起手方式（学手法不抄句子）
            _opening_block = render_reference_opening_instruction(_ref_opening)
            _opening_tag = "参考起手:" + _ref_opening["label"]
        elif OPENING_VARIETY and opening_auto:
            # ② 没有参考文章时才用轮换表兜底（不再是固定循环指定）
            _style = next_opening_style()
            _opening_block = render_opening_instruction(_style)
            _opening_tag = _style[0]

    # === 问题优先 + 命名约束 + 行文去AI味守则 + 发布前自检（公共：所有模式生效） ===
    user_message += QUESTION_FIRST_RULE
    user_message += NAMING_CONSTRAINT
    user_message += DEAI_STYLE_RULE
    # 开篇事件化（2026-09-19 用户口径）：管的是引言的"内容"，补住"形式达标但
    # 内容空转"的缺口；紧随去AI味守则之后（环境/空镜守则同属一节）。
    user_message += OPENING_EVENT_RULE
    if _opening_block:
        user_message += OPENING_VARIETY_RULE
    user_message += FORMAT_SELF_CHECK_RULE
    if _opening_block:
        # 起手方式放在公共守则之后（越靠后越醒目），并写进 mode_str 便于日志核对
        user_message += _opening_block
        mode_str += f" · 起手式:{_opening_tag}"
    user_message += INLINE_OUTPUT_RULE

    # === 重试修正反馈（如有：放在最末尾，最醒目，模型应先读到它） ===
    if feedback:
        user_message += _render_retry_feedback(feedback)

    return user_message, mode_str

# ============================================================
# 纯净模式 prompt 构建（工作台 · 完整链路）
# ============================================================



def _reference_paragraph_stats(reference_answer):
    """从参考回答统计段落长度特征，返回一行中文描述（无参考时返回 None）。

    统计逻辑与审核侧共用 core.story_text.paragraph_length_stats（单一事实源）。
    """
    from core.story_text import paragraph_length_stats
    s = paragraph_length_stats(reference_answer)
    if not s:
        return None
    total = s["count"]
    short_n = int(round(s["short_ratio"] * total))
    medium_n = int(round(s["mid_ratio"] * total))
    long_n = int(round(s["long_ratio"] * total))
    if long_n / total >= 0.5:
        zone = f"以长段为主（>150 字，占 {long_n} 段）"
    elif short_n / total >= 0.4:
        zone = f"以短段为主（<50 字，占 {short_n} 段）"
    elif medium_n / total >= 0.4:
        zone = f"以中段为主（50-150 字，占 {medium_n} 段）"
    else:
        zone = "长短混排"
    return (f"参考回答共 {total} 段：平均每段 {s['avg']:.0f} 字，"
            f"中位 {s['median']} 字，最短 {s['min']}、最长 {s['max']}；{zone}。")

def build_clean_prompt(question_title, reference_answer=None, feedback=None):
    """纯净模式：极简 prompt（题目 + 高赞风格学习 + 原创禁令）。

    刻意不注入格式/字数/章节/去AI味等守则——这些限制压住了模型的
    自身能力；原创底线由 CLEAN_SYSTEM_PROMPT 的禁令 + 生成后的
    core.originality.audit_originality 审核环节共同守住。

    返回：(user_message, mode_str)
    """
    para_section = ""
    para_stats = _reference_paragraph_stats(reference_answer)
    if para_stats:
        para_section = (
            "\n## 参考回答的段落特征（请同样学习）\n\n"
            + para_stats
            + "\n\n新回答的段落长度要与该特征接近：段落不要太长，"
              "按参考回答的分段习惯来（它是短句成段就写短段）。")

    user_message = f"""{CLEAN_SYSTEM_PROMPT}

## 知乎问题

{question_title}

## 参考高赞回答（仅供学习风格，严禁抄袭/洗稿）

{reference_answer or '（无参考回答，按题目自由创作）'}
{para_section}

请撰写新的回答。"""
    user_message += INLINE_OUTPUT_RULE

    if feedback:
        user_message += (
            "\n\n## ⚠ 上一版原创审核未通过，请针对以下问题重新创作\n\n"
            + str(feedback).strip()
            + "\n\n请直接输出一篇完全原创的新回答正文，不要解释。"
            + INLINE_OUTPUT_RULE)
    return user_message, "纯净模式"
