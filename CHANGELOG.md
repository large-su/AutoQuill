# AutoQuill 更新日志

版本号以 core/version.py 为唯一事实来源（发布 tag 为 V<VERSION>）。
## v4.8.1（2026-09-02）

### 经典模式完整链路（原「单轮」改名）+ 说明文档补全
- 「单轮（完整链路）」正式改名「经典模式 · 完整链路」，并同样支持「发布几篇（轮数）」
  （默认 1，设为 N 即循环完整链路 N 轮，每轮独立选题/生成/审核/发布，轮间小憩 2s，
  看门狗按批量口径放宽总时长）
- 后端轮数语义统一：新增 _RunSpec.rounds（默认 1）专用于经典/纯净完整链路，
  批量模式 publish_count 不受影响
- 说明文档新增「经典模式 vs 纯净模式」五环节对比（选题/提取/生成/审核/发布），
  讲清两条完整链路的约束深度差异与选型建议

### 测试
- 全量 tests/run_all.py：430 例，0 失败 0 错误
## v4.8.0（2026-09-02）

### 新增「纯净模式」（工作台 · 完整链路）

工作台新增纯净模式运行项（运行控制 → 「纯净模式 · 完整链路」）：刻意去掉
选题规则筛选、大模型题材筛选、首答长度门槛、生成格式/字数/章节/去AI味守则
等层层限制，只保留：

- **选题**：有飙升选飙升，无飙升按问题关注量选（不做故事关键词过滤）
- **提取**：首答 ≥「最短回答」（MIN_ANSWER_LENGTH，默认 500 字）+ 点赞 ≥
  「最低点赞」（MATERIAL_MIN_LIKES，门槛逐轮放宽、重试耗尽回退池内最高赞），
  其余长度/体裁/领域不限
- **生成**：给定题目 + 学习参考高赞回答风格，只禁抄袭与洗稿（build_clean_prompt/
  generate_story_clean，无格式硬约束）
- **审核**：core/originality.py 对比新回答与参考高赞回答——本地相似度信号
  （最长公共子串/字符 bigram Dice/句子重复率）+ LLM 综合判定（原创/洗稿/抄袭），
  不通过自动带原因重写（CLEAN_MAX_GEN_ATTEMPTS 次）
- **发布**：与单轮一致，写入知乎草稿箱

配置（config/story.py）：素材点赞门槛即 Web 控制台「设置→选题参数→最低点赞」
（MATERIAL_MIN_LIKES，纯净模式不再另设独立门槛，避免与 UI 设置错位）；
另可调 CLEAN_MAX_GEN_ATTEMPTS / CLEAN_AUDIT_ENABLE（关闭审核则生成后直接发布）。新增 tests/test_originality.py
（19 例：本地信号、审核判定、纯净 prompt、纯净选题/提取、run_clean 契约、
已答过台账、反馈升级）。

首轮实测迭代（2026-09-02 真机一轮：升频选题→已答过→换题→两次洗稿重审→
第三版通过并发布）：
- 「此问题已发布过回答」拒绝时写入 published_topics.jsonl（source=manual），
  跨轮/跨天选题直接跳过，不再每次白耗一轮打开+提取
- 洗稿重试反馈升级为「结构性大改」清单（换背景/人物/事件顺序/台词句式/视角），
  连续两版洗稿再叠加「换完全不同背景」的强指令，收敛速度显著加快
- 审核日志去重：判定依据只由生成循环输出一次
- 生成阶段新增「学习参考回答段落长度」：统计参考回答的段落特征
  （平均/中位/主区间，跳过章节标题与分隔线），注入纯净模式 prompt，
  新回答的段落长短向参考习惯看齐（实测段落偏长的问题）
- 浏览器启动容错（连续运行后偶发「Target page, context or browser has
  been closed」/误报未登录）：启动前与每次重试前自动清理仍占用
  data/browser_profile profile 锁的残留 msedge.exe（只按命令行匹配本
  profile 路径，不碰日常 Edge）；预检失败提示区分「浏览器启动失败」
  与「未登录」，不再只甩一句去登录
- 消除「黑框一闪」：所有后台 PowerShell/taskkill 调用（截图取屏、窗口
  聚焦、残留进程清理）统一走 run_process_silent（CREATE_NO_WINDOW +
  SW_HIDE），运行链路不再弹黑色终端框
- 纯净模式审核新增「段落长度分布对比」（纯数学）：生成后统计生成文的
  段落分布（短<50/中50-150/长>150 占比 + 平均段长），与参考回答对比，
  差异过大（CLEAN_PARAGRAPH_BUCKET_DIFF_MAX=0.55 / AVG_MIN_RATIO=0.30）
  判「段落长度不符」并带原因重写——实测生成故事平均段长 20~80 字波动，
  与参考短段风格对不齐时会被拦截重写
- 纯净模式新增「最短回答」底线：提取的首答需 ≥ 设置里「最短回答」
  （MIN_ANSWER_LENGTH，默认 500 字），一句话/太离谱的题目被过滤掉；
  兜底素材也只从「长度达标」候选中取
- 纯净/经典完整链路支持多轮：运行控制可设「发布几篇（轮数）」，默认 1，
  设置 >1 时把完整链路循环执行多轮（每轮独立选题/生成/审核/发布，轮间
  小憩 2s，看门狗按批量口径放宽总时长）；「单轮（完整链路）」改名
  「经典模式 · 完整链路」，同样可配轮数
- 提取防空转（8/31 实测复现：池内唯一飙升题已答过、其余首答点赞 3~37，
  固定 200 赞门槛 9 连拒整轮报错）：点赞门槛改为逐轮放宽
  （200→120→60，下限 CLEAN_MIN_LIKES_FLOOR=20），重试耗尽仍无合格素材时
  回退取整轮所见最高赞首答（警告后继续，不再报错中断）；选题无飙升/候选
  不足时自动滚动扩池（CLEAN_SELECT_SCREENS=3）

## v4.7.3（2026-09-01）

### 批量质量优先
批量运行改回「单轮链路循环 + 末尾筛选」的原设计（用户实测反馈：旧批量
为效率做的并行化拉低了故事质量；口径改为质量第一、效率第二）。

- BATCH_QUALITY_FIRST=True（默认）：
  - 素材收集 = 逐轮走单轮链路的 extract_content（热度选题 → 并行 5 候选
    取点赞最优 → LLM 问题池筛选），不再整页滚动取前 N 凑数；连续空轮上限
    BATCH_COLLECT_MAX_EMPTY_ROUNDS 防死循环（旧 collect_materials_batch 保留可切回）
  - 生成 = 每篇走 generate_story_with_retry（与单轮一字不差：带失败原因
    反馈的重试循环，最多 STORY_GENERATE_MAX_ATTEMPTS 次，最高分版兜底；
    Web 模式含前端改版自动降级 API）。API 模式保留多线程并行外壳（不伤质量）；
    Web 模式质量优先改为串行（反馈重试需同会话往返，效率让位于质量）
  - 阶段2.5 不再盲重试（生成已重试过），仍不合格者标记废稿不发布
  - 评分择优排序乘账号题材先验 TOPIC_PRIOR_IN_SCORE（发布 top N 向口碑题材倾斜）
- 新增 12 例 tests/test_batch_quality.py（分支锚点守护、单轮式精选去重/异常/空轮、
  单篇质量生成、API 并行/Web 串行、题材先验排序）

### 故事生成逻辑强化（故事质量）
- 引言缺失一票否决：validate_story_format 的「引言存在性」从软扣分改为硬校验——
  正文第一行直接是章节标题 `## **1**` 时，即使其余项满分也判不合格并带原因重试
  （此前只扣 2 分、满分 8/10 照样放行，缺引言废稿因此漏网）；并把「# 第一章/第一章」
  等非标准章节标题也识别为缺引言
- 前 5 段禁虚构人名：行文守则新增硬性条款，开头前 5 段（约引言及 300-500 字）不得
  出现虚构人物全名，用代词/身份/关系/称呼顶着，真名推迟到第 6 段之后
- 学习参考回答的开头引入：生成 prompt 明确要求拆解参考回答「第一句如何抛钩子、
  用什么视角/语气引入人物与事件」，学其开头引入的手法（情节仍严禁搬运）
- 问题题目优先：新增 QUESTION_FIRST_RULE 注入所有生成模式，题目原始要求（题材/人称/
  篇幅/结局/语气）为最高优先级，与本 prompt 写作要求冲突时以题目为准

### 选题质量修复（痛点：通过了硬性筛选却非故事）
- 关键词白名单+反例黑名单：STORY_INCLUDE_KEYWORDS 移除「文推荐/书荒/求文/推文/
  好看的小说/推荐小说/小说推荐/码字/新人写」等推荐/写作教学类词，新增
  STORY_EXCLUDE_KEYWORDS（求推荐/书单/写作教学/变现），_apply_story_filter 改为
  「命中白名单且未命中黑名单」才保留
- _ai_pick_best 不再「退而求其次」：单个候选也过 LLM 筛选；LLM 判定候选全部不适合
  写故事时返回哨兵 _AI_SCREEN_REJECT_ALL 触发整批重新选题，而非回退到点赞最高的非故事题
- _ai_screen_questions 区分「全部被否」与「LLM 失败」：前者中止本批（返回空），后者
  才回退原候选，不再把「全否」误当「不可用」而放行
- 顺带修复 workflows/base.py 3 处 log f-string 格式 bug（logging 抛 TypeError）

### 意见反馈模块（新功能）
- 控制台右上角「反馈」按钮：分类 + 问题描述 + 可选上下文，随时记录使用中遇到的问题
- 存储为 feedback.md（源码态项目根 / 安装态 %APPDATA%\AutoQuill），供后续迭代直接翻阅
- 终端也可用 `python feedback.py "描述" -c 选题`（--list 查看历史）
- 后端 webui/api_feedback.py（POST/GET /api/feedback）+ core/user_feedback.py（线程安全追加）

### 测试
- 新增 tests/test_user_feedback.py 4 例 + test_zhihu_ai_pick.py 重写单候选/全否用例
- 全量 tests/run_all.py：392 例，0 失败 0 错误

## v4.7.2（2026-08-30）

发布数据反馈闭环 + 稳定性保障 + 复盘自动化（P0/P1/P2 一次交付）。

### P0：发布数据反馈闭环（core/feedback_loop.py 从预留桩变为完整实现）
- core/feedback_loop.py 从预留桩变为完整实现：
  - record_story_published(url, title, meta) 与 core/topic_ledger 合流，台账落账追加 version / aid / genre / story_file / session_id——复盘可按版本直接出账，不再靠日志时间戳反推
  - attach_performance(...)：看板抓取时自动把每篇的阅读/赞/评/藏/喜欢写入 data/state/story_performance.jsonl（每条=一篇的一次观测，天然时间序列；幂等不重复）
  - attach_snapshot_rows(...)：兼容新（扁平字段）/老（metrics 字典）两种快照格式
  - seed_from_snapshots()：历史快照一键回填（tools/seed_feedback.py），本次已回填 813 篇、1576 条观测
  - summarize(genre=None)：按题材聚合「发布后日均互动分」（沿用 READER_SCORE_* 权重：赞1/评3/藏2.5/喜欢2，90 天指数衰减），观测不足 2 篇的题材回落全局中位
- 题材反馈加权选题（P0-B）：workflows/zhihu.py _dom_score 在热度分后叠加 topic_genre_multiplier（boost 钳制 0.5~2.0，默认权重 0.5；无数据/未知题材/失败时恒为 1.0，不改变原打分行为）
- 题材分类下沉：GENRE_RULES / genre_of 移入 core/detectors.py（classify_genre），看板 /api/dashboard 语义不变（webui/published.py re-export）
- 配置：config/story.py 新增 FEEDBACK_LOOP_ENABLE / TOPIC_GENRE_PRIOR_ENABLE / TOPIC_GENRE_PRIOR_WEIGHT / TOPIC_GENRE_BOOST_MIN/MAX（默认开启）
- 工具：tools/seed_feedback.py 回填历史快照 + 打印题材先验摘要（幂等，可反复执行）

### P1：生成-守则对齐 + 通道容错 + 提取自适应
- **守则前置**：story_prompt.py 新增 FORMAT_SELF_CHECK_RULE「发布前自检」（引言/章节/量化克制/环境空镜/对话句式/篇幅六条，与 validate_story_format 扣分点一一对应），所有模式生成 prompt 末尾注入——让稿子一次过，减少 8/29 式废稿与重试
- **Web 通道自动降级**：workflows 新增 _generate_web_with_failover——DeepSeek 前端改版/输入框丢失类故障自动降级到 API 通道补跑本轮；同任务连续失败 >= WEB_FAILOVER_MAX_CONSECUTIVE（默认 2）后跳过 Web 直走 API（断路器）；成功重置计数；非界面类异常（超时/风控）照常抛出
- **提取门槛自适应**：workflows/zhihu.py 新增 _adaptive_min_length/_adaptive_min_likes——首轮按 MIN_ANSWER_LENGTH/MATERIAL_MIN_LIKES 原值，之后每轮按 EXTRACT_LENGTH_FACTORS/LIKES_FACTORS 逐级放宽（长度 500→400→300 地板 250；点赞 200→120→60 地板 20），放宽时日志明示，消灭「重试 9 次仍无合格首答」的整轮空转；EXTRACT_ADAPTIVE_RELAX 可整体关闭
- 配置：config/story.py 新增 WEB_FAILOVER_TO_API / WEB_FAILOVER_MAX_CONSECUTIVE / EXTRACT_ADAPTIVE_RELAX / EXTRACT_LENGTH_FACTORS / EXTRACT_LIKES_FACTORS / EXTRACT_MIN_LENGTH_FLOOR / EXTRACT_MIN_LIKES_FLOOR

### P2：复盘自动化
- **tools/version_feedback_report.py**：每周一键复盘工具——日志事件解析（文件/标题/格式分/重试/废稿）→ git 提交时间线归因版本（新→旧自动处理，无版本号的开发提交按提交主题分组）→ 与最新知乎快照按标题归一化匹配（支持截断标题唯一前缀兜底）→ 按版本聚合发布率/格式合规/重试/废稿/互动中位 → 控制台摘要 + --write 落 docs/REVIEW-<日期>.md + 题材先验输出
- **看板「日均赞」列**：webui/published.py 为每行计算 likes_per_day / reads_per_day / engagement_per_day（互动分=赞+3×评+2.5×藏+2×喜欢，与反馈闭环同口径），前端列表新增「日均赞（/天）」列，悬停显示日均互动分——反馈梯度在控制台直接可见
- 工具已用真实数据运行：docs/REVIEW-2026-08-30.md（53 事件 / 356 快照 / 11 版本分组，与手工复盘完全一致）

### 测试
- 新增 tests/test_feedback_loop.py 10 例（落账版本/元数据、幂等观测、双格式回填、题材先验与乘数钳制、稀有题材回落、无数据时选题打分不变）+ tests/test_p1_quality_gates.py 11 例（自检清单注入、Web 降级/断路器/成功重置/非界面异常不吞、提取门槛步进与地板、看板日均指标）+ tests/test_version_report.py 12 例（日志解析、版本归因新→旧、标题归一匹配、截断前缀兜底、歧义不匹配、双格式快照）
- 全量 tests/run_all.py：375 例，0 失败 0 错误（跳过浏览器依赖 2 例）

## v4.7.0（2026-08-26）

本轮聚焦「让故事更有趣」的作者能力升级 + 单次链路可回答性死循环修复。

### 新功能
- 作者蒸馏升级到「法」层：作者技能签名新增 5 个结构性字段——叙述声口 voice_signature、信息控制与反转构造 info_control、爽点/满足机制 satisfaction_mechanics、章末钩子方式 chapter_hooks、读者承诺/题材契约 reader_promises。把"学句长/比喻的味儿"升级为"学节奏、信息差、爽点、声口的法"，生成时注入让模型锁结构而非只仿句式
- 通用写作风格新增 engagement_mechanics（通用「有趣/爽感」机制），默认「通用」模式同样受益
- 生成侧新增「爽感与趣味硬约束」：先压后弹、每节章末钩子、≥2 层套娃真相、一次打脸四部曲、≥2 种爽点类型交替、确立唯一叙述声口、龙头凤尾水蛇腰自检——采样/配方/参考模式全生效
- 旧版作者签名缺失新字段时渲染优雅降级为「（未提炼）」，不阻断生成

### 修复
- 单次完整链路反复回答同一问题的死循环：check_answerable 原先把「编辑回答」（本账号已答过此题）误判为可回答，导致已答问题被反复提取/发布；现按「写回答=未答可答、编辑回答=已答跳过、撤销删除=删过跳过、查看我的回答=发布过跳过」精确区分，已答/已删问题直接跳过并换题（发布通道仍接受写回答/编辑回答，仅选题判定收紧）

### 测试
- 渲染层：旧签名降级与「法」层新字段渲染均验证通过；构建门禁全量测试（unittest）通过

---

## v4.6.1（2026-08-25）

v4.6.0 上线实测后的修复版。

### 修复
- 草稿箱刷新完成后前端进度条不消失：完成/失败时明确清除「正在抓取」状态，成功时弹出「刷新完成」提示（此前成功路径从未被走到，进度条永远停在转圈）
- 安装版快照路径错误：webui/drafts.py 与 published.py 的 `_DATA_DIR` 由 `__file__` 相对路径改为 `core.paths.data("data")`（源码态不变，安装版正确落到 `%APPDATA%\AutoQuill\data`，不再写进安装目录 `_internal`）

### 测试
- 草稿箱/看板/服务端相关 98 例 + 全量 341 例通过；node 语法检查通过

---

## v4.6.0（2026-08-24）

本版为「草稿箱素材模块修复 + Web 模式双头化（评分/筛选走网页版大模型）」修复发布。

### 修复（草稿箱素材模块）
- qid 提取正则语法错误（`/question/(d+)/`，两个斜杠未转义且 `\d` 丢失反斜杠）→ 修正为 `/question\/(\d+)/`，浏览器 evaluate 不再抛 SyntaxError（此前网页端永远显示无草稿）
- 适配知乎草稿卡改版 DOM：标题/时间/正文均为 `div`（时间文本形如「编辑于 5 小时前」，不再有 span/data-tooltip），卡片不再只能抓到 qid
- 字数统计改为抓取服务端草稿全文（`/api/v4/questions/{qid}/draft`），不再显示卡片固定约 200 字摘要；「详情」可查看全文
- 相对时间（分钟/小时/昨天/前天/N 天/周/月前）换算为日期，供排序与筛选
- 草稿列表点击条目可直接在浏览器打开知乎对应编辑页（新标签）；原误删风险消除——删除按真实 qid 精确匹配卡片的「删除」按钮

### 新功能（Web 模式双头化）
- 评分（score_stories）与问题池筛选（screen_question_pool）在 Web 链路下改走 DeepSeek 网页版大模型（与故事生成同一浏览器通道），不再依赖 API Key；Web 不可用时自动回退原顺序/原候选，不阻断链路
- API 链路保持不变：KB Key + 401/403 自动回退生成 Key
- 批量「并行/串行」日志标签按实际通道显示（Web 并行模式不再误标串行）；评分失败提示按模式区分（Web 提示确认网页版登录）

### 测试
- 新增草稿箱（qid 正则、卡片选择器、相对时间、全文转换）与评分 Web 通道（成功/回退）回归测试；全量tests/run_all.py 341 用例通过

---

## v4.5.0（2026-08-23）

本版为「草稿箱 + 去 AI 味 + 自动化测试 + Web 窗口复用 + 大模型问题筛选」综合发布。

### 新增功能
- 草稿箱素材管理模块：预览（题目/摘要/字数/更新时间）、关键词/时间/字数筛选、勾选后批量「从知乎删除」（二次确认，不可逆），不含发布；与看板共用快照层、浏览器互斥与任务状态。
- 去 AI 味体系：
  - 采样参数：frequency/presence penalty 打开（专治复读句式）
  - 行文去 AI 味守则：万能连接词禁词、三连排比/否定排比禁令、中文 AI 高频句式（关联句式/揭露比喻/极值判断/金句）清单
  - 评分新增「语言自然度（去 AI 味）」维度与专项扣分、natural 参考分
  - 本地检测器 tools/ai_flavor_check.py（真人 1/100 vs AI 稿 28/100 区分度）
- Web 网页通道窗口复用：同一会话连续提问（continue_chat，问题连贯、不新开窗口）；并行窗口损坏（长时间无输出/输出错误/重置超限）自动开新窗口补位，一次抖动不重建。
- 大模型问题池筛选：批量与单轮链路在硬性规则（关键词/关注度/点赞门槛）之后，先由 LLM 排除不适合写知乎故事/小说的候选，再挑最适合的 1 个；失败/Web 模式/开关关闭自动回退原规则。开关 config/story.py 的 QUESTION_AI_SCREEN。
- 自动回归测试工具 tools/auto_test.py：一键跑后端 324+ 用例 + Python/app.js 语法 + Playwright 前端全流程回归 + 服务端日志检查，替代人工测试；--quick 供 CI。
- 统一测试入口 tests/run_all.py（本地/CI 共用，自动跳过浏览器依赖用例）+ GitHub Actions CI。

### 可靠性修复
- 批量 watchdog 总时长按模式放宽（batch 60min），避免正常推进被 15 分钟一刀切误杀，日志标注是否用户操作
- 评分 Key 401/403 自动回退「故事生成」Key 重试一次，失败时给出明确提示
- 双击启动黑屏：launcher 顶部 from core.ports 找不到 core 包（pythonw 以 tools/ 为 cwd）→ sys.path 注入 + 打包态兜底
- 草稿/看板快照质量防护：零数据/坏指标不落盘，自动回退最近好快照；双格式归一兼容
- 草稿删除/看板删除单条容错：页面导航异常/找不到按钮自动跳过继续，结束汇总成功/跳过/异常
- 浏览器任务四路互斥：刷新/删除之间禁止并发抢占同一 profile
- 草稿删除完成提示被 loadDrafts 清空 → 保留 8s 完成 toast
- 自动回归工具首发即抓出 4 个问题（含上述黑屏与 toast），全部修复

### 工程化（P0-P3）
- 统一快照层 webui/_snapshot.py（published/drafts 共用）
- server 拆分：browser_tasks / dashboard_api / drafts_api
- 前端抽离 webui/static/style.css + app.js，共享渲染助手去重
- 端口单一来源 core/ports.py；19 个一次性探查脚本归档 tools/archive/probes
- 日志轮转（启动清理 30 天前日志、保留最近 20 份）；关键模块类型注解
- 新增单测：drafts / published 快照 / ai_flavor / 评分回退 / question_screen / 并行 continue / zhihu ai_pick / launcher 等

### 测试
- 全量 tests/run_all.py：336 用例，0 失败 0 错误（跳过的浏览器依赖用例在 CI 自动排除）

---

## v4.4.0（2026-08-23）

与 v4.5.0 同轮迭代（草稿箱主体、去 AI 味主体、批量可靠性、无黑框启动、评分失败提示），详细条目见 v4.5.0；本行保留以对版本提交对齐。

## v4.3.0

修复作者蒸馏/生成链路多问题 + 一键启动（详见 README「版本历史」）。
