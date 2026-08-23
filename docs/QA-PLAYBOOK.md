# AutoQuill 测试与质量手册（QA Playbook）

本项目把常用的「测量 / 编译 / 校验 / 测试 / 打包」流程固化成可复用的脚本，本文说明**每个脚本是干什么的、什么时候用、怎么执行**，避免重复摸索。

## 一、脚本清单

| 脚本 | 用途 | 什么时候用 | 示例 |
|---|---|---|---|
| `tests/run_all.py` | 全量单元测试（336+ 用例；自动跳过需要真实浏览器/登录态的用例） | 每次改动后端后、提交前、CI 必跑 | `python tests/run_all.py` |
| `tools/auto_test.py` | 自动回归测试：后端单测 + Python/app.js 语法 + 前端 Playwright 全流程 + 服务端日志检查 | 改完前端/后端后，模拟“人工测试员”跑一遍；`--quick` 只跑后端+语法（CI 友好） | `python tools/auto_test.py`<br>`python tools/auto_test.py --quick` |
| `tools/ai_flavor_check.py` | AI 味检测（0-100）：检查生成稿的机器味，与真人基准对比 | 生成效果前后对比、发布前自查 | `python tools/ai_flavor_check.py output`<br>`python tools/ai_flavor_check.py --zhihu data/published_answers_.json` |
| `tools/build_release.py` | 正式发版：门禁（git 干净/分支 main）→ 全量测试 → PyInstaller → Inno Setup 安装包 → SHA256；**版本号自动从 core/version.py 注入** | 发新版本时 | `python tools/build_release.py` |
| `tests/test_*.py` | 专项单测（草稿箱/快照/评分回退/并行窗口/大模型筛选/launcher 等） | 定位具体模块问题时单独跑 | `python -m unittest tests.test_drafts` |
| `tools/archive/probes/` | 历史一次性探查脚本（已归档，只读参考） | 浏览器 DOM 排查时的历史参考 | — |

## 二、推荐工作流（按场景）

### 日常改代码
```bash
# 改完一个模块，先快速编译自检（等价 auto_test --quick 的语法部分）
python -m py_compile webui/server.py
node --check webui/static/app.js        # 前端 JS

# 快速自检 = 全量单测 + 语法
python tools/auto_test.py --quick
```

### 提交前（完整回归）
```bash
python tests/run_all.py                 # 336 用例
python tools/auto_test.py               # 含前端 Playwright 全流程 + 日志检查
```

### 前端单独回归（改样式/JS 后）
```bash
python tools/auto_test.py               # 会起临时服务并逐项点击验证（四大模式/看板/草稿箱/设置）
```

### 生成效果与 AI 味对比
```bash
python tools/ai_flavor_check.py output            # 检测生成稿（期望均值随去 AI 味改进逐步下降）
python tools/ai_flavor_check.py --zhihu data/published_answers_2026-08-23.json  # 真人基准（约 1/100）
python tools/ai_flavor_check.py output/story_x.md # 单篇
```

### 发新版本（一键）
```bash
# 1. 改版本号（唯一入口）
#    core/version.py  →  VERSION = "x.y.z"
# 2. 打包发布（自动：门禁+测试+PyInstaller+安装包+SHA256，并自动把版本号写入 iss）
python tools/build_release.py
# 3. 发布到 GitHub
#    git add -A && git commit -m "..."
#    git tag vx.y.z && git push origin main --tags
#    gh release create vx.y.z release/AutoQuill-Setup-x.y.z.exe release/AutoQuill-Setup-x.y.z.exe.sha256 --title "AutoQuill vx.y.z" --notes-file notes.md
```

## 三、关键说明

- **版本号唯一入口**：`core/version.py`。`build_release.py` 构建时自动把版本号注入 `installer/AutoQuill.iss`（手工改 iss 会被覆盖）。
- **CI**：`.github/workflows/test.yml` 每次 push/PR 自动跑 `tests/run_all.py`（浏览器依赖用例自动排除）。
- **产物不入库**：dist/、release/、build/ 永远不提交，发版产物在 `release/` 下。
- **官网安装包未签名**：用户下载时可能见 SmartScreen 提示，点「更多信息 → 仍要运行」即可（README FAQ 有说明）。

## 四、常见失败与排查

| 现象 | 排查路径 |
|---|---|
| `python tools/auto_test.py` 前端项失败 | 看测试输出定位到具体检查点；测试服务日志在临时目录 server.log，页面 console 错误会汇总在「页面无 console 错误」项 |
| 单元测试报导入错误 | 确认在项目根执行；`tests/run_all.py` 已自动把项目根加入 sys.path |
| 双击启动无窗口 | 查看 `logs/launcher.log` 最近一次双击的时间戳与内容；多为启动早期崩溃 |
| build_release 门禁失败「工作区有未提交改动」 | 先 `git add -A && git commit` |
| AI 味检测分数对比不明显 | 确认采样的是同一数据源；检测器为规则启发式，配合评分日志的「自然度」维度交叉看 |
