# Agent 操作守则与经验教训（AutoQuill 开发实录复盘）

> 面向未来会话：本文总结本 Agent 在 AutoQuill 开发中反复踩的坑与对策。
> 新会话请先读 docs/PROJECT_MEMORY.md 再读本文；两条配合即可快速进入角色并少犯同类错误。

## 一、报错全景（按频率与危险度归类）

### 1. 工具调用方式（最基础，却最常犯）

报错形态：`Error: unknown tool "xxx": only run_code is callable directly`。
原因：本环境所有工具（bash/read/grep/read_image/web_search/modlens 等）必须包在 run_code 的异步程序内调用，我早期多次直接调用。
对策：
- 入口只有一个：await tools.xxx(...) 写在 run_code 的 code 里；
- 遇到“unknown tool”立即意识到是调用方式问题；
- run_code 的参数要 JSON 安全（get_goal({}) 这类也要显式传对象，别传 undefined）。

### 2. 模板字面量的转义吞噬（最大报错源，占一半以上）

报错形态：SyntaxError: Expected ',' / unterminated string literal / 文件内容被改（反斜杠、反引号、${} 消失或变形）。
原因：在 run_code 程序里用反引号模板写文件内容或 old/new_string 时，JS 模板会吞掉：反引号本身、${插值、\n、\s、\d、\u200b、\. 等转义，外层模板一遇反引号即提前闭合。
实录案例：
- 写 test_ai_flavor.py 时 `晒太阳。\n” \` 续行导致字符串未闭合；
- CHANGELOG.md / PROJECT_MEMORY.md 因文档含反引号多次报 Expected ',';
- bat 文件 .venv\Scripts 的反斜杠被吞成 .venvScripts（双击启动失败）；
- JS 正则 /\s+/g 变 /s+/g，排比正则 /。！？┤ 跨逗号问题靠测试抓出。
对策（铁律）：
- 文件内容一律用“无反引号、无 ${、无反斜杠”的写法：Markdown 代码标识改用中文说明或缩进，路径/命令去掉反引号；
- 必须写反斜杠时用 String.fromCharCode(92) 或先写占位再替换；
- 宁可拆分写入，不用一段大模板；
- 写完立即验证：node --check / py_compile / grep 检查关键字段。

### 3. 路径与环境差异（沙箱 vs 真实运行）

报错形态：FileNotFoundError('/tmp/...')、module not found、heredoc 里 \s 被逐层剥。
原因：Git Bash 的 /tmp 与 Windows python 路径不一致；heredoc 每经过 bash→python 会把 \\n 剥成 \n；.venv 与 miniconda 互混（pytest 不存在）。
对策：
- 临时文件放项目内目录（.skills_tmp/ 等），用 Windows 相对路径；
- 一律 .venv/Scripts/python，不用裸 python/miniconda；
- heredoc 里避免反斜杠正则（改用参数传值或脚本文件）。

### 4. 修改代码时破坏既有结构

报错形态：AttributeError: module has no attribute filter_rows；IndentationError；函数头被吞。
原因：大段整块替换时 old_string 锚点不精确（如用“读 44 行替换 load()”把相邻 filter_rows 头吞掉；zhihu 插桩只用 docstring 首行当锚点，把 _extract_auto_parallel 拆烂）。
对策（铁律）：
- 先读足上下文（函数签名+前后行），用带函数头和独特行的精确小锚点；
- 替换后立即 py_compile + 跑相关单测 + git diff 核对只改了预期行；
- 复杂重构可先 git stash / 对照 git show HEAD 还原再重做。

### 5. 逻辑正确但测试没覆盖到真实语义

案例：
- launcher 重定向 needs 分支（frozen 下 if sys.stdout is None 不执行 → 没替换）既有 test 抓出；
- parallel _release 清掉 last_session 导致同会话连续性丢失，单测抓出；
- auto_test 的 route 拦截顺序 delete 先于 status → 删除轮询恒 started（工具自身缺陷）；
- “heredoc 验证通过”≠“真实双击成功”：launcher 以 tools/ 为 cwd 时 import core 崩（黑屏）。
对策：
- 写逻辑前先读既有测试/对既有语义做假设清单；
- 新增能力必须配单测（本仓库单测成本极低）；
- 验证要走“真实启动路径”（cmd 双击 / python tools/x.py vs import），而不只是 import 导入。

### 6. 对官方/既有流程的上游假设不足

案例：第一次跑 build_release.py 时不知 installer/AutoQuill.iss 硬编码 4.3.0 → 门禁失败；该问题后已改为构建时自动注入版本。
对策：跑陌生脚本前先读脚本头/入口与关键条件；未知假设用 grep 在代码里确认。

## 二、操作守则速查（未来会话直接照做）

1. 一切工具调用包进 run_code；参数 JSON 安全。
2. 写文件/编辑内容避免反引号、${}、反斜杠；需要时用占位+替换或拆分；写完立即语法检查与 grep 验证。
3. 替换代码用精确小锚点（含函数签名与上下文），小步提交，改后 py_compile + 单测 + git diff。
4. 验证按真实运行路径（双击/cmd/脚本入口），不只 import 级验证。
5. 新增行为必须配单测；回归默认跑 tests/run_all.py + tools/auto_test.py。
6. 用 .venv/Scripts/python；沙箱无外网时显式走代理（-x http://127.0.0.1:7890 --ssl-no-revoke）。
7. 先读再改：新模块先读头注释/入口/相关测试；跑陌生脚本先看入口逻辑。

## 三、收益反思

- 把“验证前置”变成习惯后（每改必测），后半程犯错率显著下降（P0-P3 阶段几乎每步一次编译+回归）。
- 自动回归工具 tools/auto_test.py 的价值：它代替了手工“跑到哪测到哪”，把上述 3/4/5 类问题快速暴露成可复现失败。
- 模板转义是环境特性而非代码问题：把“无反引号写文件”固化成铁律后，同类报错从高频降到几乎为 0。
