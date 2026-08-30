# AutoQuill 更新日志

版本号以 core/version.py 为唯一事实来源（发布 tag 为 V<VERSION>）。

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
