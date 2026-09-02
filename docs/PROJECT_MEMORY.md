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

- 版本唯一入口：core/version.py（当前 v4.8.0，tag v4.8.0 随本版发布）
- 打包：python tools/build_release.py —— 门禁（git 干净/main）→ 全量测试 → PyInstaller → Inno 安装包 → SHA256，版本号自动注入 installer/AutoQuill.iss（勿手工改 iss）
- 发布：git tag vX.Y.Z && git push origin main --tags && gh release create（gh 已登录 large-su）；产物在 release/，dist/release/build 不入库

## 3. 架构地图（核心模块）

- webui/server.py：Web 控制台入口（路由注册 + TaskRunner + watchdog + 日志/SSE + 设置/状态）
- webui/browser_tasks.py：看板/草稿箱四个后台任务状态字典 + browser_busy() 互斥（共用同一浏览器 profile，必须串行）
- webui/dashboard_api.py / drafts_api.py：看板 / 草稿箱路由（register 模式挂到 server app）
- webui/_snapshot.py：统一快照层（published/drafts 共用发现/读取/坏数据回退/质量判定）
- webui/published.py / drafts.py：各自业务（DOM 抽取、筛选、评分、删除；字段/质量回调不同）
- workflows/base.py：单轮/批量/纯净模式编排（run_single / run_batch / run_clean；批量阶段：收集 → 大模型问题筛选 → 生成 → 评分 → 发布）
- workflows/zhihu.py：知乎 DOM 实现（选题规则+评分、并行提取候选取最优、纯净模式 select_topic_clean / extract_content_clean、发布写草稿）
- core/originality.py：纯净模式「洗稿/抄袭 + 段落长度分布」对比审核（本地相似度 + LLM 判定，Paragraph 属纯数学）
- applications/zhihu_story/：browser_adapter（登录/爬取/删除）、author_profiler（文风蒸馏）、prompts.py（系统/评分/筛选提示词）
- web_drivers/：Web 通道（browser_pool 共享浏览器、deepseek.py DOM 驱动、parallel.py 并行调度、base.py 驱动基类）
- llm_client.py / story_generation.py / story_prompt.py / story_scoring.py：API 生成、提示词、评分、问题池筛选
- 前端：webui/static/index.html（结构）+ style.css + app.js（已抽离）；四大模式：工作台 / 作者蒸馏 / 已发布内容看板 / 草稿箱素材

## 4. 已完成的重大功能（截至 v4.6.0）

v4.6.0（草稿箱修复轮）：草稿箱 qid 正则语法修复 + 适配知乎草稿卡改版 DOM（标题/时间/正文 div，时间「编辑于 …」相对文本）→ 字数改用服务端草稿全文统计（不再 200 字摘要）、相对时间换算日期、列表点击条目开浏览器、删除按真实 qid 匹配；Web 模式评分/问题池筛选改走网页版大模型（双头：API 只用 API、Web 只用 Web），失败自动回退不阻断。（详见 CHANGELOG.md）

1. 草稿箱素材管理：预览/筛选/批量删除（从知乎删除，二次确认），不含发布；与看板共用快照层+互斥
2. 去 AI 味体系：采样惩罚参数、行文守则+中文 AI 句式禁词、评分「自然度」维度与专项扣分、tools/ai_flavor_check.py 检测器（真人≈1/100 vs AI≈28/100）
3. Web 窗口复用：continue_chat 同会话连续提问；并行 slot 损坏（超时/错误/重置 3 次失败→DEAD）自动开新窗口补位（上限 8）；meta.session_id 同会话连续（生成重试接入属下一步）
4. 大模型问题池筛选：批量（run_batch 收集后）+ 单轮（_ai_pick_best 在并行提取合格候选后），先排除不适合写知乎故事/小说的，再挑最适合的 1 个；开关 config/story.py 的 QUESTION_AI_SCREEN；失败/Web 模式/关闭回退原规则
5. 自动回归测试：tools/auto_test.py（后端 336 用例+语法+Playwright 前端全流程+服务端日志；--quick 供 CI）
6. P0-P3 工程化：统一快照层、server 路由拆分、前端抽离 style.css+app.js、统一测试入口 tests/run_all.py、GitHub Actions CI、日志轮转（30 天+留 20）、端口单一来源 core/ports.py、19 个探查脚本归档 tools/archive/probes、类型注解
7. 可靠性修复：批量 watchdog 按模式放宽（batch 60min，非用户操作会标注）、评分 Key 401 自动回退主 Key、快照质量防护/坏数据回退、删除单条容错、四任务浏览器互斥、双击启动黑屏修复（launcher sys.path+兜底）、草稿删除完成 toast 保留
8. 技能安装：.claude/skills/code-review-skill（审查指南）+ superpowers（writing-plans/systematic-debugging/TDD 等 14 个），已随仓库提交
9. 纯净模式（v4.8.0，工作台新增运行项）：去限制创作——流量选题（有飙升选飙升/无则按关注量）→ 提取只卡最短回答+点赞（门槛放宽+最高赞兜底）→ 极简生成（学风格+段落长短，禁抄袭洗稿）→ 审核（原创+段落分布）→ 发布草稿；支持多轮（一次设 N）。已答过题自动记台账跳过；后端纯净参数集中在 config/story.py 的 CLEAN_* 系列

## 5. 约定与常见坑（改代码前必读）

- 测试：改完先 python tests/run_all.py（336 用例，浏览器依赖类自动跳过）；前端改动跑 python tools/auto_test.py；发版用 build_release.py；完整手册见 docs/QA-PLAYBOOK.md
- Python 环境：一律用 .venv/Scripts/python，不用 miniconda 裸 python
- 行尾：仓库文件多 CRLF（编辑工具默认 LF），改完大文件用脚本归一化行尾；bat 必须纯 ASCII（中文注释会因 GBK 崩）
- 写入文件的坑：DSH 模板字面量会把反引号、${}、\n 吞噬——写含这些的文件时避免或转义；前/后端 JS 用 node --check 验证
- 网络：沙箱 bash 无外网，需显式走 Clash 代理 -x http://127.0.0.1:7890 --ssl-no-revoke
- 端口守卫：测试用 8799 时需在 server 白名单放行（tools/auto_test.py 内建 bootstrap 已处理）
- Hindsight 工具：仓库记忆服务可能不可达（网络策略）；优先读 docs/*.md + 代码定位

## 6. 待办 / 建议下一步

- 把「同一生成的格式修正重试」接到 meta.session_id（同窗口连续修正，能力已就绪未接）
- 批量素材“DOM 提取失败或过短”告警偏多 → 提取阈值/重试调优
- 前端 dashboard/drafts 渲染函数进一步合并（已去重状态/进度条助手，列表/筛选仍双份）
- 后续功能建议优先参考 QA-PLAYBOOK 与用户真实测试反馈（webui.log 有详细链路日志）

## 7. 日常高频命令速查

- 全量单测：python tests/run_all.py
- 自动回归：python tools/auto_test.py
- 快速自检：python tools/auto_test.py --quick
- AI 味对比：python tools/ai_flavor_check.py output
- 发版打包：python tools/build_release.py
- 启动控制台：python main.py --web
