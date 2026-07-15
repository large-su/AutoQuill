# ============================================================
# applications/zhihu_story/config.py
# 知乎故事创作 — 专用业务参数
#
# 架构位置：Layer 5 (Applications) — 变化边界
# 原则：这里的任何参数只对知乎故事创作有意义，
#       框架级通用参数留在顶层 config.py
# ============================================================

# ============================================================
# 模式设置
# ============================================================

# 选题模式："manual" = 手动选题 / "auto" = 全自动评分选题
QUESTION_SELECT_MODE = "auto"

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

# 格式不合规时是否自动重试（False = 直接跳过，不浪费 token）
ENABLE_FORMAT_RETRY = False

# 故事创作素材模式：
#   "recipe"               纯配方驱动（从当前文章提炼配方后生成，不附参考原文）
#   "reference"            纯参考文章（旧模式，用 STORY_SYSTEM_PROMPT + 参考文章）
#   "recipe_and_reference" 配方 + 参考文章结合（推荐，配方指引 + 参考文章风格借鉴）
STORY_MATERIAL_MODE = "recipe"

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
# 元知识自学习
# ============================================================

META_LEARN_ENABLE = False
META_DISTILL_THRESHOLD = 20
META_DISTILL_TOP_RATIO = 0.6
META_INJECT_DEFAULT = False
META_HIGH_SCORE_THRESHOLD = 50

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

# 首答采集优先通过 Windows UI Automation 读取完整已渲染内容；失败时自动回退 OCR 滚屏。
ENABLE_UIA_ANSWER_EXTRACTION = True
UIA_ANSWER_WAIT_TIMEOUT = 4.0       # 等待首答 UIA 正文完整出现的最长时间
UIA_ANSWER_POLL_INTERVAL = 0.25     # UIA 首答未就绪时的轮询间隔

# 素材赞同数门槛：通过门槛的回答才进入生成池，并触发配方提炼
ENABLE_MATERIAL_LIKES_GATE = True       # True=启用赞同数过滤；False=所有合格回答都进入生成池
MATERIAL_MIN_LIKES = 200                 # 最低赞同数；已识别赞同数低于此值时跳过该素材
MATERIAL_UNKNOWN_LIKES_POLICY = "drop"  # 未识别到赞同数时：keep=保留，drop=跳过

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

# ============================================================
# 元知识分层检索
# ============================================================

META_RETRIEVAL_ENABLE = True     # True=按 recipe 检索相关小节, False=注入全文
META_RETRIEVAL_TOP_K = 3         # 每次检索返回的小节数量
