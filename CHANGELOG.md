# AutoQuill 更新日志

版本号以 core/version.py 为唯一事实来源（发布 tag 为 V<VERSION>）。

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
