# AutoQuill 项目工作记忆（新会话速览）

> 给新开窗口的 AI/开发者：本文件总结当前项目状态、架构、约定、已完成工作、待办与坑。

## 新会话必经三件套（进入角色 30 秒）

1. 本文案（PROJECT_MEMORY.md）—— 项目是什么、架构地图、版本发布、已完成功能、待办
2. docs/AGENT-OPERATING-NOTES.md —— Agent 操作守则与经验教训（工具调用方式、模板转义雷区、改代码/验证流程），**先读它可避免本轮大部分重复报错**
3. docs/QA-PLAYBOOK.md —— 测试与质量流程（单测/回归/打包的命令与场景）

再往下按需：README.md（用户视角）、docs/DEVELOPER.md（架构细节）。

## 1. 项目是什么

AutoQuill = 知乎故事自动创作助手：自动选题 → 提取高赞回答 → 用大模型按作者文风生成故事 → 写入知乎草稿（发草稿，不自动发布）。
本地 Web 控制台（FastAPI + 独立 pywebview 窗口），Web 网页版 / API 双 LLM 通道。

- 入口：python main.py --web（或双击 start_autoquill.bat，pythonw 无黑框启动）
- 端口：8787（唯一来源 core/ports.py）；服务端有 Host/Origin 守卫，改端口启动需动态放行（server.run 已自动加白名单）
- 数据：知乎登录态在 data/browser_profile + config/browser_state.json；快照在 data/（published_answers_*.json / drafts_*.json，不入库）
- 日志：logs/autoquill_<时间戳>.log（主）、logs/webui.log（服务）、logs/launcher.log（启动器）

## 2. 版本与发布

- 版本唯一入口：core/version.py（当前 v4.9.1）
- 已发布版本（2026-09-19 同一天连发三版）：
  · v4.9.0 —— DeepSeek 官网改版适配 + 豆包通道 + 历史会话清理 + 网页端丢章节标题修复
  · v4.9.1 —— 起手方式以参考文章为准（+ 抄袭红线）+ 首启引导文案与文档修订
  · **v4.9.2（最新）** —— 看板「统计 / 明细」双视图 + 统计页重排与裁切修复；
    知乎登录失效识别与常驻登录入口（设置 → 知乎账号）；删除即同步本地快照
  安装包与 SHA256 见 release/ 与 GitHub Release（Latest）
- 打包：python tools/build_release.py —— 门禁（git 干净/main）→ 全量测试 → PyInstaller → Inno 安装包 → SHA256，版本号自动注入 installer/AutoQuill.iss（勿手工改 iss）
- 发布：git tag vX.Y.Z && git push origin main --tags && gh release create（gh 已登录 large-su）；产物在 release/，dist/release/build 不入库

## 3. 架构地图（核心模块）

- automation/：**自动化模块**（2026-09-19 新增，M1 骨架 + M2 发布草稿）——24 小时时间轴调度，无人化运营：
  model（任务类型契约/计划默认值）· store（计划/当日排班/台账，原子写）· planner（排班：
  配额/时段/铺开式随机排班（等分时段+修复间隔，非随机游走）/上限公式 floor(时段÷间隔)+1/去碰撞/错过补做/失败补位）· scheduler（tick、串行、幂等、熔断、
  暂停/停止）· executor（复用 TaskRunner，不直接碰 DOM）。运行数据在
  `data/state/automation/`（已 gitignore）；API 见 webui/automation_api.py；前端独立文件
  `webui/static/automation.js` + `/automation.js` 路由。规划见 docs/AUTOMATION-PLAN.md
  · **任务 A 发布草稿（M2，2026-09-19）**：真机探针确认草稿箱 DOM（卡片
  `.CreationManage-CreationCard`，DOM 顺序 = 编辑于倒序、**最旧的在最后**；编辑入口
  `a[href*="#write"]`；发布按钮文本「发布回答」）→ `browser_write.list_draft_cards()` /
  `publish_draft()`（qid 空 = 发最旧一篇；校验 URL `/answer/<aid>` 或服务端草稿清空）。
  护栏：空草稿箱记「跳过」而非失败（防空跑触发熔断）、登录失效转 NeedHuman 暂停全自动化、
  失败不自动重试、`run-now {dry_run:true}` 演练只探按钮不点发布（前端「演练发布」按钮）
  · **任务类型只有两个（2026-09-24 用户定案）**：全链路撰写（写故事）+ 发布草稿（发布故事）。
  打卡挑战（网页端不好操作）与互动类（感谢/赞同/回复评论，看着太乱）**整体取消**，
  `automation/model.py` 不再登记任何「预留类型」——目录里有的就是能跑的；
  老 plan.json 里残留的 checkin/thank/reply_comment 会被 normalize_plan 安静丢弃（用户无需手工改文件）
  · 运行时段语义：只约束自动动作的时间（时段外待机、不自动退出、跨零点自动续排），
  手动「立即执行」不受时段限制；时段结束时剩余作业统一收尾（跳过 + 通知一次）
  · **状态语义（2026-09-23 修，排查「发布一直跑不通」时补的洞）**：`plan.enabled` 只代表
  「用户点过开始」——tick 线程是进程内的，所以 `webui/server.py` 启动时按落盘计划
  `ensure_running()` 把线程补回来（重启 ≠ 停工；stop 会把 enabled 置回 false，故不会复活）；
  未开启时 `_tick` 走「只认手动作业」分支、`run_now` 会补线程并在通知里写明「只做这一次」
  （此前未开启时点「立即执行/演练发布」只回一句通知，作业永远挂在待执行）；
  发布链路的登录识别已下沉 `publish_draft`（草稿箱页 302 到 /signin → reason=need_login →
  NeedHuman），执行器那处 `page_needs_login(b.page)` 预检是死代码（page 还是 about:blank），
  发布作业另补 `_browser_lock`（独占 profile 必须串行）
  · **M4 托盘常驻（2026-09-19）**：`tools/launcher.py` 的 TrayController（.NET NotifyIcon，
  必须 UI 线程创建）——关窗默认 Hide() 到托盘（closing 返回 False）、托盘菜单（打开/状态/
  暂停恢复/立即执行/退出，立即执行遇到发布作业先确认）、首次隐藏提示一次、托盘建不起来
  自动退化为真关闭；设置（关窗行为 / 开机自启）在 `core/launcher_config.py`
  （DATA_ROOT/config/launcher.json，启动器与服务共读写，每次关窗重读即生效），
  自启写 HKCU Run（命令带 `--tray`，安装态 exe / 源码态 pythonw+launcher.py），
  API `GET/POST /api/launcher/settings`；探针 `tools/archive/probes/spike_tray.py`
  直接跑生产代码（真机 15 项检查）
- webui/server.py：Web 控制台入口（路由注册 + TaskRunner + watchdog + 日志/SSE + 设置/状态）
- webui/browser_tasks.py：看板/草稿箱四个后台任务状态字典 + browser_busy() 互斥（共用同一浏览器 profile，必须串行）
- webui/dashboard_api.py / drafts_api.py：看板 / 草稿箱路由（register 模式挂到 server app）
- webui/_snapshot.py：统一快照层（published/drafts 共用发现/读取/坏数据回退/质量判定）
- webui/published.py / drafts.py：各自业务（DOM 抽取、筛选、评分、删除；字段/质量回调不同）
- workflows/base.py：单轮/批量/纯净模式编排（run_single / run_batch / run_clean；批量阶段：收集 → 大模型问题筛选 → 生成 → 评分 → 发布）
- workflows/zhihu.py：知乎 DOM 实现（选题规则+评分、并行提取候选取最优、纯净模式 select_topic_clean / extract_content_clean、发布写草稿）
- core/originality.py：纯净模式「洗稿/抄袭 + 段落长度分布」对比审核（本地相似度 + LLM 判定，Paragraph 属纯数学）
- applications/zhihu_story/：browser_adapter（登录/爬取/删除；browser_write 拆分出写操作通道——含 publish_story 写草稿与 publish_draft 发布草稿）、author_profiler（文风蒸馏）、prompts.py（系统/评分/筛选提示词）
- web_drivers/：Web 通道（browser_pool 共享浏览器、deepseek.py DOM 驱动、parallel.py 并行调度、base.py 驱动基类）。
  2026-09 已适配 DeepSeek 官网改版：无模式/开关切换（用默认态）、多轮读取用
  「发送前打锚点 → 只读锚点之后的新消息」防读旧回复、会话删除走
  `POST /api/v0/chat_session/delete`（Bearer=localStorage.userToken）+ 侧栏 DOM 兜底
  2026-09-19 再修「格式校验误杀」：网页端把 markdown 渲染成 DOM（`## **N**` → h2），
  只读 innerText 的通道（DeepSeek）把章节标题丢成裸章节号 → 校验「章节 0 个」必扣 4 分，
  通道满分只剩 6/10，字数略欠的完整稿直接判废（当天 13 轮丢 5 篇，其中 4 篇是误杀）。
  逐块重建抽到 base.MARKDOWN_REBUILD_JS（DeepSeek/豆包共用），文本侧
  story_text.restore_bare_chapter_headings + 校验侧 count_chapter_headings 双兜底；
  豆包 wait_complete 另加「正文末尾停在章节标题 = 残稿，不判完成」
- llm_client.py / story_generation.py / story_prompt.py / story_scoring.py：API 生成、提示词、评分、问题池筛选
- 前端：webui/static/index.html（结构）+ style.css + app.js（已抽离）；四大模式：工作台 / 作者蒸馏 / 已发布内容看板 / 草稿箱素材
  · 看板双视图（2026-09-19）：**统计**（6 KPI + 摘要条 + 12 栅格图表卡）/ **明细**（筛选 chips +
    表格），标题栏分段控件切换，状态存 localStorage `aqDashView`；样式见 style.css 的
    `.view-switch / .stats-grid / .stat-card / .stats-kpis`；改样式后可跑
    `tools/archive/probes/shot_dashboard.py` 出图肉眼验收（含样例数据，不碰真实账号）

## 4. 已完成的重大功能（截至 v4.9.0）

v4.6.0（草稿箱修复轮）：草稿箱 qid 正则语法修复 + 适配知乎草稿卡改版 DOM（标题/时间/正文 div，时间「编辑于 …」相对文本）→ 字数改用服务端草稿全文统计（不再 200 字摘要）、相对时间换算日期、列表点击条目开浏览器、删除按真实 qid 匹配；Web 模式评分/问题池筛选改走网页版大模型（双头：API 只用 API、Web 只用 Web），失败自动回退不阻断。（详见 CHANGELOG.md）

1. 草稿箱素材管理：预览/筛选/批量删除（从知乎删除，二次确认），不含发布；与看板共用快照层+互斥
2. 去 AI 味体系：采样惩罚参数、行文守则+中文 AI 句式禁词、评分「自然度」维度与专项扣分、tools/ai_flavor_check.py 检测器（真人≈1/100 vs AI≈28/100）
3. Web 窗口复用：continue_chat 同会话连续提问；并行 slot 损坏（超时/错误/重置 3 次失败→DEAD）自动开新窗口补位（上限 8）；meta.session_id 同会话连续（生成重试接入属下一步）
4. 大模型问题池筛选：批量（run_batch 收集后）+ 单轮（_ai_pick_best 在并行提取合格候选后），先排除不适合写知乎故事/小说的，再挑最适合的 1 个；开关 config/story.py 的 QUESTION_AI_SCREEN；失败/Web 模式/关闭回退原规则
5. 自动回归测试：tools/auto_test.py（后端 514 例+语法+Playwright 前端全流程+服务端日志；--quick 供 CI）
6. P0-P3 工程化：统一快照层、server 路由拆分、前端抽离 style.css+app.js、统一测试入口 tests/run_all.py、GitHub Actions CI、日志轮转（30 天+留 20）、端口单一来源 core/ports.py、19 个探查脚本归档 tools/archive/probes、类型注解
7. 可靠性修复：批量 watchdog 按模式放宽（batch 60min，非用户操作会标注）、评分 Key 401 自动回退主 Key、快照质量防护/坏数据回退、删除单条容错、四任务浏览器互斥、双击启动黑屏修复（launcher sys.path+兜底）、草稿删除完成 toast 保留
8. 技能安装：.claude/skills/code-review-skill（审查指南）+ superpowers（writing-plans/systematic-debugging/TDD 等 14 个），已随仓库提交
9. 纯净模式（v4.8.0，工作台新增运行项）：去限制创作——流量选题（有飙升选飙升/无则按关注量）→ 提取只卡最短回答+点赞（门槛放宽+最高赞兜底）→ 极简生成（学风格+段落长短，禁抄袭洗稿）→ 审核（原创+段落分布）→ 发布草稿；支持多轮（一次设 N）。已答过题自动记台账跳过；后端纯净参数集中在 config/story.py 的 CLEAN_* 系列

10. 历史会话清理（v4.9.0 运维工具，2026-09-15 真机校准）：`tools/ds_history_cleanup.py`
    scan/delete/smoke 三命令，清理上线前堆积在 DeepSeek 的写故事会话。判定 = 30 天前
    （updated_at）+ 会话内容命中 AutoQuill 提示词指纹；删除前逐条备份正文、分批 + 熔断 +
    删后复核。接口：`GET /api/v0/chat_session/fetch_page`（游标 lte_cursor.updated_at，
    每页 100）、`GET /api/v0/chat/history_messages?chat_session_id=`、
    `POST /api/v0/chat_session/delete`。**坑**：不带站点 `x-client-*` 头时列表游标被
    忽略（只回第一页）——客户端头在进站时从页面自身请求捕获。真机全量扫描（2026-09-17，
    1243 条会话 / 1053 条旧会话）：165 条确认 AutoQuill 写故事链路 + 74 条模板式疑似 +
    76 条用户手写 + 2 条弱指纹 + 736 条无关；报告 data/cleanup/report_20260917.md。
    **2026-09-17 已执行清理**：删 314 条（0 失败），备份
    data/cleanup/backup_20260917_085832/；删后全量复核确认命中 0（仅剩 1 条置顶手写 +
    2 条「洗稿含义」弱指纹）。日常新会话已由 v4.9.0 的「完成后自动删除」兜住

11. v4.9.0 网页版通道大修（2026-09）：DeepSeek 官网改版适配（取消模式/开关切换、
    多轮读取改「发送前打锚点、只读新回复」、会话删除走 `POST /api/v0/chat_session/delete`
    + 侧栏 DOM 兜底）、新增**豆包网页版驱动** `web_drivers/doubao.py`（md-box 逐块重建、
    卡片式交付兜底补问、发送双判据、完成判定含「末尾停在章节标题=残稿」、侧栏删除）、
    网页端正文提取统一 `base.MARKDOWN_REBUILD_JS`、格式校验章节三形态容错
    （规范 `## **N**` / `第N章` / 裸章节号）；经典与纯净两条完整链路均支持多轮发布

## 5. 约定与常见坑（改代码前必读）

- 启动器单实例（2026-09-20 用户口径）：多次启动不再叠窗口/托盘图标——启动器进程持有
  Windows 命名互斥体（按 data_root 取名，源码态与安装版互不干扰）+ 实例文件里的控制端口
  （DATA_ROOT/config/launcher_instance.json），第二次启动只唤起已有窗口后 exit=0；
  `--service` 子进程不受守卫影响。真退出 = 托盘菜单或控制台设置页的「退出 AutoQuill」
  （关窗 = 最小化到托盘，launcher.json 的 close_to_tray）；退出监听已与托盘解耦
  （托盘挂了按钮照样管用）。残留实例文件无害：互斥体一释放就会被新实例覆盖
- 首行自我汇报（2026-09-20 真机）：模型会把"我严格遵循…格式/字数要求，采用反差断语
  开篇，搭建 6+ 章节"当正文第一行（复述我们的 prompt）。清洗（_is_meta_plan_line）要求
  写作元词 >=2 且计划动词起手；格式校验把它当一票否决项（details["废话"]）
- 测试：改完先 python tests/run_all.py（691 例，浏览器依赖类自动跳过）；前端改动跑 python tools/auto_test.py；发版用 build_release.py；完整手册见 docs/QA-PLAYBOOK.md
- Python 环境：一律用 .venv/Scripts/python，不用 miniconda 裸 python
- 行尾：仓库文件多 CRLF（编辑工具默认 LF），改完大文件用脚本归一化行尾；bat 必须纯 ASCII（中文注释会因 GBK 崩）
- 写入文件的坑：DSH 模板字面量会把反引号、${}、\n 吞噬——写含这些的文件时避免或转义；前/后端 JS 用 node --check 验证
- 开篇同质化：写作守则叠加（第一人称声口 + 引言一票否决 + 禁空镜开场 + 首句进动作）
  会把开头挤成唯一解「我+动作」（2026-09-19 实测经典模式最近 22 篇 20 篇如此、最近
  20 篇 100%）。对策 = story_prompt 的 OPENING_VARIETY_RULE + **起手方式以本题参考文章
  为准**（analyze_reference_opening 本地识别后注入，学手法不抄句子），无参考时才轮换
  兜底；抄袭红线由 core.originality.opening_copy_signals（默认 12 字连续重合）在重试
  循环里拦截。开关：config/story.py 的 OPENING_MIRROR_REFERENCE / OPENING_VARIETY /
  OPENING_COPY_MIN_RUN
- 开篇"读不进去"（2026-09-19 用户口径）：约束叠加的另一面——守则只管引言的"形式"
  （句数/字数/第一行不是标题/禁空镜），没人管"内容必须是一件正在发生的事"，模型就写
  评价句简介（"所有人都说…／没人知道…／只有我…"）。对策 = OPENING_EVENT_RULE（开篇事件化
  守则）+ core.detectors.check_summary_opening（validate_story_format 第 8 项，一票否决）。
  判定口径：引言无对话 且（总结体模板 ≥2 处 / 性格自述 / 评价句 ≥50%）；采集参考 35 篇零误报
- 知乎登录态会「cookie 在、服务端已登出」：`is_logged_in()` 只看 z_c0 会假阳性，
  任何页面被 302 到 /signin（2026-09-19 看板/草稿箱「刷新失败」的真因）。
  抓取/删除链路统一用 `page_needs_login()` 识别（抛 ZhihuLoginRequired），前端提示
  重新登录；排查用 tools/archive/probes/probe_state_login.py（一条命令定性）
  **登录引导本身也要守这条（2026-09-23 用户报「弹窗闪一下就被关」的真因）**：
  `login_zhihu_flow` 原先只看 cookie 判「已登录」就 return，于是窗口一拉起就被 close
  （日志里 0.9 秒走完全程）。判定改 `zhihu_login_confirmed()`＝页面离开登录页 **且**
  z_c0 在（两个条件缺一不可：只看 cookie 是本次事故，只看页面会把「未登录也能打开的
  首页」误判成功）。凡「靠 cookie 判断登录态」的新代码，先想一遍这条
  · 第二轮实测又补两条（用户报「登完了不关窗 / 登了等于没登」）：
  (a) 判定要**三条**：凭证换新 / 当前页或**上下文任一页**离开登录页 / 会话探测
  （`context.request` 问首页，登出态 302→/signin、登录态 200；不开标签页、不打扰
  用户）；登录可能在别的标签页完成，只看 `browser.page` 会一直等到超时。判失败前
  再权威探测一次（用户可能刚好在最后几秒登完）
  (b) `load_storage_state()` **只补缺、绝不覆盖**：陈旧 state 文件会把 profile 里刚
  登录出的新 cookie 盖回去（时间线：登录窗口关 → 下一秒网页版登录检查启动浏览器又把
  旧 cookie 写回）——这是「登了等于没登」的直接机制
  · 排查入口：`tools/archive/probes/probe_signin_page.py`（登录页渲染了什么：二维码/表单/
  风控词）；profile 的 History/Cookies 库能还原「窗口里到底访问过什么」（注意库被浏览器
  占用时要重试复制）
- 登录入口：设置弹窗「知乎账号」区块 = 状态显示 + 「检查登录状态」（POST /api/setup/zhihu-check，
  真实打开知乎首页判断）+「重新登录知乎」；browser_adapter.verify_zhihu_login() 可复用。
  首启引导的知乎步骤在失效态下也会重新显示登录按钮（此前被判「已完成」而隐藏）
- 删除即同步本地：知乎删除成功后由 API 调 `_snapshot.prune_rows()` 剔除本地快照
  （published.prune_aids / drafts.prune_qids），前端重载列表即可见，不必整页重抓
- 网页端正文提取：markdown 会被渲染成 DOM（`## **N**` → h2），只读 innerText 必丢
  章节语法 → 一律走 base.MARKDOWN_REBUILD_JS 逐块重建；判「生成完成」前查末尾是否
  停在章节标题（base/豆包已有判据）；格式校验只认文本形态，别让通道差异背锅
- 网络：本机可直连 GitHub——git push 走 SSH、gh 走 HTTPS 都实测可用（2026-09-19 发布验证）。
  git 配置里写着 Clash 代理 http://127.0.0.1:7890，**Clash 没开时不要给命令加代理环境变量**
  （会报 proxyconnect 拒绝）；确实需要代理的命令再显式 -x http://127.0.0.1:7890 --ssl-no-revoke
- 端口守卫：测试用 8799 时需在 server 白名单放行（tools/auto_test.py 内建 bootstrap 已处理）
- Hindsight 工具：仓库记忆服务可能不可达（网络策略）；优先读 docs/*.md + 代码定位

## 6. 待办 / 建议下一步

- 网页端逐块重建的真机复核：下一轮真实运行看日志的 `格式检测：` ——DeepSeek 出现 10/10
  说明 h1-h6 命中；若仍显示「章节:0个(-4)」却判定合规，说明走的是文本侧兜底
  （restore_bare_chapter_headings），届时再校准一次 DOM walker

- 把「同一生成的格式修正重试」接到 meta.session_id（同窗口连续修正，能力已就绪未接）
- 批量素材“DOM 提取失败或过短”告警偏多 → 提取阈值/重试调优
- 前端 dashboard/drafts 渲染函数进一步合并（已去重状态/进度条助手，列表/筛选仍双份）
- **选题/写作的三处开关（2026-09-23 按复盘结论改的，都可一键回退）**：
  · `config.story.TOPIC_TYPE_PRIOR_ENABLE/TOPIC_TYPE_PENALTY`（默认 0.4）——命题作文/微小说类
    打分打折（实测这类题阅读/天中位 2.0，是求推荐类的 1/7）；
  · `config.story.STORY_DOWNWEIGHT_KEYWORDS/STORY_DOWNWEIGHT_FACTOR`（默认 0.6）——「求推荐」类
    从硬排除改降权（旧黑名单把本期第一那道题也封了）；纯书单（书荒/书单/求书）仍在 STORY_EXCLUDE_KEYWORDS；
  · `config.story.TOPIC_GENRE_PRIOR_MIN_AGE_DAYS`（默认 7）——题材先验只采纳发布满 7 天的篇目；
    两篇爆款第 3 天只有 674/296 阅读，那时计入会把好题材判死（且会自我强化）
  · 篇幅与命名：story_prompt 的「不少于 4000 字、鼓励 5000-7000、每节不设上限」+
    命名规则从硬性降为建议（改回旧口径就改这两处文案）
- **效果复盘的两个入口（2026-09-23 新增，建议每周跑一次）**：
  · 生成侧（过程）：`python tools/version_feedback_report.py --write` → 每版生成量/合规/重试/废稿；
  · 结果侧（读者买不买账）：`python tools/published_review.py --days 60 --write` → 版本×题型×榜单，
    口径是「阅读/天」+「固定 20 天窗口增量」（两者都有偏差，要交叉看）。结论与待办见
    docs/REVIEW-perf-2026-09-23.md
  · **已确认的教训**：两篇最高阅读（3639/3058，v4.7.0）在第 3 天只有 674/296 阅读，20 天后才涨到
    3 千+ → 别用发布头几天的数据判断内容好坏（题材先验尤其危险）；**题型差距（13 倍）远大于任何
    正文特征差距** → 选题环节才是杠杆
- 后续功能建议优先参考 QA-PLAYBOOK 与用户真实测试反馈（webui.log 有详细链路日志）

## 7. 日常高频命令速查

- 全量单测：python tests/run_all.py
- 自动回归：python tools/auto_test.py
- 快速自检：python tools/auto_test.py --quick
- AI 味对比：python tools/ai_flavor_check.py output
- 清 DeepSeek 历史残留会话：python tools/ds_history_cleanup.py scan（只读）
  / delete --report data/cleanup/scan_*.json --yes（真删，先出报告确认）
- 发版打包：python tools/build_release.py
- 启动控制台：python main.py --web
