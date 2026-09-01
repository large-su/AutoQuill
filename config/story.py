# ============================================================
# config/story.py — 故事创作业务参数（单一事实来源）
#
# 架构位置：Config 层 — 被 workflows / core / llm_api / kb_manager 等
# 跨层共享。原位于 applications/zhihu_story/config.py，因被底层/中间层
# 反向引用造成分层倒置，2026-08 迁出收敛于此。
#
# 原则：只放「故事创作域」参数；框架级通用参数在顶层 config.py；
# 应用私有参数（browser_adapter 等内部用、无跨层引用的）也可在此，
# 应用层通过 applications/zhihu_story/config.py 的 re-export 读取。
# ============================================================

__all__ = [
    # 模式设置
    "QUESTION_SELECT_MODE", "ENABLE_STORY_FILTER", "QUESTION_SOURCE",
    "CUSTOM_QUESTION_URL",
    # 选题规则筛选
    "STORY_INCLUDE_KEYWORDS", "STORY_EXCLUDE_KEYWORDS", "MAX_SELECT_SCREENS",
    # 格式与素材
    "ENABLE_FORMAT_RETRY", "STORY_MATERIAL_MODE",
    # 知识库
    "KB_MAX_PER_GENRE", "KB_MERGE_TRIGGER", "KB_ENABLE", "RECIPE_VERBOSE_MODE",
    # reader_score
    "READER_SCORE_W_LIKES", "READER_SCORE_W_COMMENTS", "READER_SCORE_W_COLLECTS",
    "READER_SCORE_W_HEARTS", "READER_SCORE_REF_AGE_DAYS", "READER_SCORE_DECAY_EXPONENT",
    # 反馈闭环（core/feedback_loop）
    "FEEDBACK_LOOP_ENABLE", "TOPIC_GENRE_PRIOR_ENABLE",
    "TOPIC_GENRE_PRIOR_WEIGHT", "TOPIC_GENRE_BOOST_MIN",
    "TOPIC_GENRE_BOOST_MAX",
    # P1 质量与可靠性：守则前置已内置于 prompt（story_prompt 常量）；
    # Web 降级 / 提取自适应为以下配置
    "WEB_FAILOVER_TO_API", "WEB_FAILOVER_MAX_CONSECUTIVE",
    "EXTRACT_ADAPTIVE_RELAX", "EXTRACT_LENGTH_FACTORS",
    "EXTRACT_LIKES_FACTORS", "EXTRACT_MIN_LENGTH_FLOOR",
    "EXTRACT_MIN_LIKES_FLOOR",
    "BATCH_QUALITY_FIRST", "BATCH_COLLECT_MAX_EMPTY_ROUNDS",
    "TOPIC_PRIOR_IN_SCORE",
    # URL
    "ZHIHU_RECOMMEND_URL", "ZHIHU_INVITED_URL",
    # 自动选题与提取
    "MIN_ANSWER_LENGTH", "MAX_ANSWER_RETRIES", "MAX_TOPIC_RETRY",
    "ENABLE_DOM_ANSWER_EXTRACTION", "PARALLEL_EXTRACT_LIMIT",
    "ENABLE_MATERIAL_LIKES_GATE", "MATERIAL_MIN_LIKES", "MATERIAL_UNKNOWN_LIKES_POLICY",
    "AUTHOR_PROFILE",
    # 批量模式
    "DEFAULT_BATCH_GENERATE_COUNT", "DEFAULT_BATCH_PUBLISH_COUNT",
    "BATCH_AUTO_GENERATE_COUNT", "BATCH_GENERATE_REDUNDANCY_RATIO",
    "BATCH_GENERATE_MIN_EXTRA", "BATCH_ROUND_SPLIT_ENABLE", "BATCH_MAX_PUBLISH_PER_ROUND",
    "BATCH_QUESTIONS_PER_PAGE", "SCROLLS_PER_REFRESH", "MAX_TOTAL_ATTEMPTS",
    "ENABLE_PARAGRAPH_ANALYSIS",
    "SCORE_STORY_HEAD_CHARS", "SCORE_STORY_TAIL_CHARS",
    "STORY_GENERATE_CONCURRENCY", "STORY_GENERATE_CONCURRENCY_AUTO",
    "STORY_GENERATE_CONCURRENCY_MIN", "STORY_GENERATE_CONCURRENCY_MAX",
    "STORY_GENERATE_MAX_ATTEMPTS",
]

# ============================================================
# 模式设置
# ============================================================

# 选题模式："manual" = 手动选题 / "auto" = 全自动评分选题
QUESTION_SELECT_MODE = "auto"

# 选题来源："recommend" = 创作中心推荐话题（默认）/ "invited" = 邀请回答
#           / "custom" = 自选问题（CUSTOM_QUESTION_URL，跳过选题直接提取）
QUESTION_SOURCE = "recommend"

# 自选问题模式的问题链接（Web 控制台设置里填写，运行时校验）
CUSTOM_QUESTION_URL = ""

# 故事领域筛选开关（True = 用规则筛选非故事类问题）
ENABLE_STORY_FILTER = True

# ============================================================
# 选题规则筛选 — 白名单模式，替代 LLM 筛选
# ============================================================
# 逻辑：标题命中任一关键词 → 保留
#       标题不命中           → 排除
# 只需要维护这个列表，想多选就加词，想排除就删词

STORY_INCLUDE_KEYWORDS = [
    # --- 故事/小说直接标识 ---
    "小说", "故事", "爽文", "甜文", "虐文", "言情",
    "古言", "现言", "重生", "穿书", "耽美",
    "宫斗", "宅斗", "仙侠", "奇幻", "末世", "病娇",
    "大女主", "追妻", "火葬场", "小甜饼", "暗恋文",
    "救赎文", "复仇文", "悬疑文", "脑洞文", "系统文",
    "攻略文", "女频", "男频", "短篇", "古代",
    # --- 创作相关（「写一个故事/小说」类请求）---
    "写小说", "写故事", "写文", "网文", "网络小说",
    # --- 角色/情节指向 ---
    "女主", "男主", "女主角", "男主角",
]

# 反例关键词：命中即排除（即使标题也命中了上面的白名单）。
# 这些是「求推荐/书单/写作教学/变现」类——答案是书单或方法论，不是
# 故事体，曾导致「通过了硬性筛选却写不出好故事」的痛点。
STORY_EXCLUDE_KEYWORDS = [
    # 求推荐/找书（答案是书单而非故事）
    "书荒", "求文", "推文", "文推荐", "书单", "求书", "求推荐",
    "好看的小说", "推荐小说", "小说推荐",
    # 写作教学/变现（答案是方法论而非故事）
    "如何写", "怎么写", "写作技巧", "写作变现", "写作入门",
    "签约", "投稿", "稿费", "大纲", "码字", "新人写",
]

# 自动选题时首屏无故事类问题的滚动扩池上限（屏数）。
# 推荐页首屏常只有 5-10 张卡片且多数是非故事话题，滚动可加载更多；
# 仍无命中则选题报错（不静默选非故事话题）。
MAX_SELECT_SCREENS = 3

# 全自动选题的并行提取候选数：一批同时打开 N 个问题页并行提取，
# 取点赞最高的合格者进入生成（失败原因不阻塞其他候选，整批全败才重选）。
# 页面加载在浏览器进程并行，单个问题页的等待不再串行累加。
PARALLEL_EXTRACT_LIMIT = 5

# 格式不合规时是否自动重试。单轮/批量生成现已统一走
# generate_story_with_retry（按 STORY_GENERATE_MAX_ATTEMPTS 带失败原因
# 反馈重试），此开关主要控制批量模式阶段2.5 的格式补重试。
# False = 直接跳过，不浪费 token；True = 对不合规文章再重试一次。
ENABLE_FORMAT_RETRY = True

# 故事创作素材模式：
#   "sample"               参考文章采样（默认）：本地片段采样注入，零 LLM 提炼
#   "recipe"               纯配方驱动（从当前文章提炼配方后生成，不附参考原文）
#   "reference"            纯参考文章（旧模式，用 STORY_SYSTEM_PROMPT + 参考文章）
#   "recipe_and_reference" 配方 + 参考文章结合（配方指引 + 参考文章风格借鉴）
STORY_MATERIAL_MODE = "sample"

# ============================================================
# 知识库配置
# ============================================================

KB_MAX_PER_GENRE = 30
KB_MERGE_TRIGGER = 120
# [2026-08 已退役] kb_manager 配方闭环停止维护（2404 配方零消费）；
# 反馈闭环的后续形态见 core/feedback_loop.py。保留 False 以兼容旧读取。
KB_ENABLE = False

# 配方提炼详细模式开关（影响 RECIPE_EXTRACT_PROMPT 组装）
RECIPE_VERBOSE_MODE = True

# ============================================================
# reader_score：基于真实读者互动的评分
# ============================================================

READER_SCORE_W_LIKES    = 1.0
READER_SCORE_W_COMMENTS = 3.0
READER_SCORE_W_COLLECTS = 2.5
READER_SCORE_W_HEARTS   = 2.0
READER_SCORE_REF_AGE_DAYS = 90
READER_SCORE_DECAY_EXPONENT = 0.5

# ============================================================
# 反馈闭环（core/feedback_loop.py）
# ============================================================

# 是否启用发布数据反馈闭环（落账/表现观测/题材先验）。
# False = 关闭后：发布只走 topic_ledger 原有台账，选题不做题材加权，
# 看板抓取不再自动入账（历史快照仍可用 seed_from_snapshots 手动回填）。
FEEDBACK_LOOP_ENABLE = True

# 选题打分是否叠加「题材读者先验」乘数（P0-B）。
TOPIC_GENRE_PRIOR_ENABLE = True

# 先验乘数的干预强度：boost = 1 + W × (题材分/全局分 - 1)。
# 0.5 = 题材分是全局 2 倍时乘 1.5 倍分；0 关闭。
TOPIC_GENRE_PRIOR_WEIGHT = 0.5

# 乘数上下限（防止口碑题材垄断选题 / 冷门题材被完全排除）。
TOPIC_GENRE_BOOST_MIN = 0.5
TOPIC_GENRE_BOOST_MAX = 2.0

# ============================================================
# Web 通道自动降级（P1：8/29 曾因 DeepSeek 前端改版 3 连败）
# ============================================================

# Web 生成通道遇「前端改版/输入框丢失」类错误时，自动降级到 API 通道
# 完成本轮剩余尝试（需已配置 API Key；未配置则照旧报错并提示）。
WEB_FAILOVER_TO_API = True

# 同一次生成任务内连续失败多少次后，跳过 Web 直接走 API（断路器）。
WEB_FAILOVER_MAX_CONSECUTIVE = 2

# ============================================================
# 提取门槛自适应（P1：消灭「重试 9 次仍无合格首答」的整轮空转）
# ============================================================

# 首轮按 MIN_ANSWER_LENGTH / MATERIAL_MIN_LIKES 原值筛选；之后每轮
# 按下方因子逐级放宽（并受地板约束），并在日志中明示放宽幅度。
EXTRACT_ADAPTIVE_RELAX = True

# 长度门槛逐级系数（第 0 轮 1.0 → 第 1 轮 ×0.8 → 第 2 轮及以后 ×0.6）
EXTRACT_LENGTH_FACTORS = (1.0, 0.8, 0.6)
# 点赞门槛逐级系数（第 0 轮 1.0 → 第 1 轮 ×0.6 → 第 2 轮及以后 ×0.3）
EXTRACT_LIKES_FACTORS = (1.0, 0.6, 0.3)
# 放宽地板（避免把低质素材放进来）
EXTRACT_MIN_LENGTH_FLOOR = 250
EXTRACT_MIN_LIKES_FLOOR = 20


# ============================================================
# 批量模式：质量优先（默认开启；想把效率摆在质量前面的旧模式可关闭）
# ============================================================

# True = 批量完全复用单轮链路语义：
#   素材 = 逐轮「选题 → 并行 5 候选取最优 → LLM 筛选 → 提取」（extract_content），
#         不再用整页滚动取前 N 凑数；
#   生成 = 每篇走带失败原因反馈的重试循环（generate_story_with_retry，
#         与单轮一致），格式不合格不再盲重试；
#   发布 = 评分择优时乘账号题材先验（TOPIC_PRIOR_IN_SCORE）。
# False = 旧批量（collect_materials_batch 整页滚动 + 无反馈生成 +
#         单次盲重试）——仅当数量优先于质量时使用。
BATCH_QUALITY_FIRST = True

# 单轮式精选：连续多少轮拿不到新素材即停止（防推荐池枯竭死循环）
BATCH_COLLECT_MAX_EMPTY_ROUNDS = 8

# 批量评分排序是否乘以账号题材先验（feedback_loop 题材口碑，
# 让发布 top N 向发过的同类题跑得赢的题材倾斜）
TOPIC_PRIOR_IN_SCORE = True

# ============================================================
# URL
# ============================================================

ZHIHU_RECOMMEND_URL = "https://www.zhihu.com/creator/featured-question/recommend"
ZHIHU_INVITED_URL = "https://www.zhihu.com/creator/featured-question/invited"


# ============================================================
# 自动选题参数
# ============================================================

MIN_ANSWER_LENGTH = 500
MAX_ANSWER_RETRIES = 3

# 选题重试次数：首答过短 / 问题不可回答 / 未过点赞门槛时重新选题的次数。
# 总尝试 = MAX_TOPIC_RETRY + 1（默认 5 次重试共 6 次尝试）。
# 旧版硬编码 3（共 4 次尝试）——知乎推荐流低质题变多，提高重试后
# 单轮成功概率显著上升；可配项，Web 控制台「设置」可调。
MAX_TOPIC_RETRY = 5

# DOM 通道（browser_adapter）：唯一提取通道。UIA/OCR 屏幕降级已随
# V4.0.2 移除——纯 DOM 可无头运行，不再需要坐标校准。
ENABLE_DOM_ANSWER_EXTRACTION = True

# 素材赞同数门槛：通过门槛的回答才进入生成池，并触发配方提炼
ENABLE_MATERIAL_LIKES_GATE = True       # True=启用赞同数过滤；False=所有合格回答都进入生成池
MATERIAL_MIN_LIKES = 200                 # 最低赞同数；已识别赞同数低于此值时跳过该素材
MATERIAL_UNKNOWN_LIKES_POLICY = "drop"  # 未识别到赞同数时：keep=保留，drop=跳过

# 作者技能注入：生成故事时把该作者的蒸馏技能 profile 注入 prompt。
# 置空字符串关闭注入；profile 文件位于 data/authors/{name}.json。
# 默认「通用」：注入内置通用写作规则（config/builtin_general_profile.json，
# 随安装包分发，新环境开箱可用；也可用「提炼通用文风」生成更定制化的版本）
AUTHOR_PROFILE = "通用"

# ============================================================
# 批量模式默认值
# ============================================================

DEFAULT_BATCH_GENERATE_COUNT = 20
DEFAULT_BATCH_PUBLISH_COUNT = 12

# 大模型问题池筛选：批量收集到的问题+回答候选，先由 LLM 排除
# 不适合写知乎故事/小说的，再从剩余中挑最适合的（API 模式生效；
# 失败/禁用时回退原硬性规则结果，不阻断流程）。
QUESTION_AI_SCREEN = True

# True：批量入口只询问发布数，生成/采集数按冗余比例自动计算
# False：沿用旧模式，分别询问生成数和发布数
BATCH_AUTO_GENERATE_COUNT = True
BATCH_GENERATE_REDUNDANCY_RATIO = 1.20
BATCH_GENERATE_MIN_EXTRA = 2

# 大批量发布时自动拆成多轮，降低单轮采集/评分/发布失败成本
BATCH_ROUND_SPLIT_ENABLE = True
BATCH_MAX_PUBLISH_PER_ROUND = 30

BATCH_QUESTIONS_PER_PAGE = 3
SCROLLS_PER_REFRESH = 5          # 每次刷新推荐页后 PageDown 轮数
MAX_TOTAL_ATTEMPTS = 1000

# 正式跑批默认关闭段落分布图；调试段落长度时再打开
ENABLE_PARAGRAPH_ANALYSIS = False

# 评分时只取开头+结尾，减少评分 prompt 长度
SCORE_STORY_HEAD_CHARS = 1000
SCORE_STORY_TAIL_CHARS = 500

# API 模式下故事并行生成的并发数（增大可缩短阶段2耗时，上限取决于 API 限流策略）
STORY_GENERATE_CONCURRENCY = 10
STORY_GENERATE_CONCURRENCY_AUTO = True
STORY_GENERATE_CONCURRENCY_MIN = 3
STORY_GENERATE_CONCURRENCY_MAX = 10

# 故事生成最大尝试次数（含首次）。生成失败/过短/格式不合规时，会把
# 上一次的具体失败原因（字数/章节/长段/引号等）反馈注入重试 prompt，
# 带反馈重试的收敛率远高于同 prompt 盲目重试。值 >=1；1 = 不重试。
STORY_GENERATE_MAX_ATTEMPTS = 3
