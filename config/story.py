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
    "STORY_INCLUDE_KEYWORDS", "MAX_SELECT_SCREENS",
    # 格式与素材
    "ENABLE_FORMAT_RETRY", "STORY_MATERIAL_MODE",
    # 长文模式
    "LONG_FORM_MODE", "LONG_FORM_CHAPTER_COUNT", "LONG_FORM_OUTLINE_MAX_TOKENS",
    "LONG_FORM_CHAPTER_MAX_TOKENS", "BATCH_CHAPTER_COUNT", "STORY_OUTPUT_DIR",
    # 知识库
    "KB_MAX_PER_GENRE", "KB_MERGE_TRIGGER", "KB_ENABLE", "RECIPE_VERBOSE_MODE",
    # reader_score
    "READER_SCORE_W_LIKES", "READER_SCORE_W_COMMENTS", "READER_SCORE_W_COLLECTS",
    "READER_SCORE_W_HEARTS", "READER_SCORE_REF_AGE_DAYS", "READER_SCORE_DECAY_EXPONENT",
    # URL 与等待时间
    "ZHIHU_RECOMMEND_URL", "ZHIHU_INVITED_URL",
    "WAIT_ZHIHU_PAGE_LOAD", "WAIT_WRITE_ANSWER_CLICK", "WAIT_EDITOR_CLICK",
    "WAIT_AFTER_PASTE", "WAIT_CONFIRM_CLICK", "WAIT_DRAFT_SAVE",
    "WAIT_FOCUS_SETTLE", "WAIT_AFTER_HOME", "WAIT_ANSWER_LOAD_TRIGGER",
    "WAIT_NEXT_SCREEN", "WAIT_CLOSE_TAB", "WAIT_IMPORT_MENU_SETTLE",
    "WAIT_IMPORT_DOC_PANEL", "WAIT_UPLOAD_DIALOG_OPEN", "WAIT_FILE_PATH_PASTE",
    "WAIT_FILE_CONFIRM", "WAIT_DOC_IMPORT_DONE", "WAIT_FALLBACK_CLOSE_DIALOG",
    # 发布阶段 OCR 点击重试
    "OCR_CLICK_WRITE_ANSWER_RETRIES", "OCR_CLICK_WRITE_ANSWER_WAIT",
    "WAIT_WRITE_ANSWER_RETRY_HOME", "OCR_CLICK_IMPORT_RETRIES",
    "OCR_CLICK_IMPORT_WAIT", "OCR_CLICK_MORE_RETRIES", "OCR_CLICK_MORE_WAIT",
    "OCR_CLICK_IMPORT_DOC_RETRIES", "OCR_CLICK_IMPORT_DOC_WAIT",
    "OCR_CLICK_UPLOAD_RETRIES", "OCR_CLICK_UPLOAD_WAIT",
    # 自动选题与提取
    "MIN_ANSWER_LENGTH", "MAX_ANSWER_RETRIES", "MAX_TOPIC_RETRY",
    "ENABLE_DOM_ANSWER_EXTRACTION",
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
    # --- 求文/推荐类 ---
    "文推荐", "书荒", "求文", "推文", "好看的小说",
    "推荐小说", "小说推荐",
    # --- 创作相关 ---
    "写小说", "写故事", "写文", "码字", "新人写",
    "网文", "网络小说",
    # --- 角色/情节指向 ---
    "女主", "男主", "女主角", "男主角",
]

# 自动选题时首屏无故事类问题的滚动扩池上限（屏数）。
# 推荐页首屏常只有 5-10 张卡片且多数是非故事话题，滚动可加载更多；
# 仍无命中则选题报错（不静默选非故事话题）。
MAX_SELECT_SCREENS = 3

# 格式不合规时是否自动重试（False = 直接跳过，不浪费 token）
ENABLE_FORMAT_RETRY = False

# 故事创作素材模式：
#   "sample"               参考文章采样（默认）：本地片段采样注入，零 LLM 提炼
#   "recipe"               纯配方驱动（从当前文章提炼配方后生成，不附参考原文）
#   "reference"            纯参考文章（旧模式，用 STORY_SYSTEM_PROMPT + 参考文章）
#   "recipe_and_reference" 配方 + 参考文章结合（配方指引 + 参考文章风格借鉴）
STORY_MATERIAL_MODE = "sample"

# ============================================================
# 长文模式（大纲→批量写作交替流水线）
# ============================================================

LONG_FORM_MODE = False
LONG_FORM_CHAPTER_COUNT = 20             # 总章节数
LONG_FORM_OUTLINE_MAX_TOKENS = 2048      # 大纲 max_tokens
LONG_FORM_CHAPTER_MAX_TOKENS = 8192      # 批量写作 max_tokens（5 章 × ~1500 字/章）

# 批量写作：每批规划 N 章大纲，然后一次性生成 N 章正文
# 大纲→写作→大纲→写作交替，大纲生成即审视（基于上一批真实输出调整下一批）
BATCH_CHAPTER_COUNT = 5                  # 每批章节数

STORY_OUTPUT_DIR = "data/stories"        # 故事工作区根目录

# ============================================================
# 知识库配置
# ============================================================

KB_MAX_PER_GENRE = 30
KB_MERGE_TRIGGER = 120
KB_ENABLE = True

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
# URL
# ============================================================

ZHIHU_RECOMMEND_URL = "https://www.zhihu.com/creator/featured-question/recommend"
ZHIHU_INVITED_URL = "https://www.zhihu.com/creator/featured-question/invited"

# ============================================================
# 知乎专用等待时间（秒）
# ============================================================

WAIT_ZHIHU_PAGE_LOAD = 2.0        # 打开知乎问题页后的等待时间；页面慢、写回答按钮常找不到时调大
WAIT_WRITE_ANSWER_CLICK = 0.5     # 点击「写回答」后等待编辑器/工具栏出现的时间
WAIT_EDITOR_CLICK = 0.3           # 降级为直接粘贴时，点击编辑区后的稳定等待
WAIT_AFTER_PASTE = 1.0            # 降级为直接粘贴后，等待内容进入编辑器的时间
WAIT_CONFIRM_CLICK = 0.3          # 预留确认点击后的短等待；当前主发布链路较少使用
WAIT_DRAFT_SAVE = 1.5             # 内容导入/粘贴完成后，等待知乎自动保存草稿的时间

# 前台自动化细分等待：优先调这些，避免改代码里的 sleep
WAIT_FOCUS_SETTLE = 0.2           # 切回/聚焦 Edge 窗口后的稳定等待
WAIT_AFTER_HOME = 0.4             # 按 Ctrl+Home 回到页面顶部后的等待
WAIT_ANSWER_LOAD_TRIGGER = 0.8    # 进入问题页后触发回答加载（如 PageDown）后的等待
WAIT_NEXT_SCREEN = 0.5            # 采集阶段翻到下一屏推荐问题后的等待
WAIT_CLOSE_TAB = 0.3              # 采集完成后关闭当前问题页标签的等待
WAIT_IMPORT_MENU_SETTLE = 0.5     # 点击「导入」或「更多」后，等待菜单展开稳定
WAIT_IMPORT_DOC_PANEL = 0.7       # 点击「导入文档」后，等待上传面板出现
WAIT_UPLOAD_DIALOG_OPEN = 0.5     # 点击上传区域后，等待系统文件选择框打开
WAIT_FILE_PATH_PASTE = 0.3        # 文件选择框里粘贴 md 文件路径后的等待
WAIT_FILE_CONFIRM = 0.25          # 文件选择框中确认/回车前后的短等待
WAIT_DOC_IMPORT_DONE = 1.0        # 选择 md 文件后，等待知乎把文档内容导入编辑器
WAIT_FALLBACK_CLOSE_DIALOG = 0.3  # 找不到上传区域时，按 Esc 关闭弹窗后的等待

# 发布阶段 OCR 点击重试参数。调这里可以控制「写回答/导入/上传」等按钮定位耗时。
OCR_CLICK_WRITE_ANSWER_RETRIES = 3  # OCR 查找「写回答」按钮的最大尝试次数
OCR_CLICK_WRITE_ANSWER_WAIT = 0.2   # 每次没找到「写回答」后，下一次 OCR 前的等待
WAIT_WRITE_ANSWER_RETRY_HOME = 0.2  # 首轮找不到「写回答」时，回到顶部后再次重试前的等待
OCR_CLICK_IMPORT_RETRIES = 3        # OCR 查找工具栏「导入」按钮的最大尝试次数
OCR_CLICK_IMPORT_WAIT = 0.2         # 每次没找到「导入」后，下一次 OCR 前的等待
OCR_CLICK_MORE_RETRIES = 2          # 找不到「导入」时，OCR 查找「更多」按钮的最大尝试次数
OCR_CLICK_MORE_WAIT = 0.2           # 每次没找到「更多」后，下一次 OCR 前的等待
OCR_CLICK_IMPORT_DOC_RETRIES = 3    # OCR 查找「导入文档」入口的最大尝试次数
OCR_CLICK_IMPORT_DOC_WAIT = 0.2     # 每次没找到「导入文档」后，下一次 OCR 前的等待
OCR_CLICK_UPLOAD_RETRIES = 2        # OCR 查找上传区域文案的最大尝试次数
OCR_CLICK_UPLOAD_WAIT = 0.2         # 每次没找到上传区域后，下一次 OCR 前的等待

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

# --- 已移除的 UIA/OCR 屏幕通道参数（保留定义兼容旧 import，勿再使用）---
ENABLE_UIA_ANSWER_EXTRACTION = False
UIA_ANSWER_WAIT_TIMEOUT = 4.0
UIA_ANSWER_POLL_INTERVAL = 0.25

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
