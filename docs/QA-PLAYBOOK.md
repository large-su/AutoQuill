# AutoQuill 测试与质量手册（QA Playbook）

本项目把常用的「测量 / 编译 / 校验 / 测试 / 打包」流程固化成可复用的脚本，本文说明**每个脚本是干什么的、什么时候用、怎么执行**，避免重复摸索。

## 一、脚本清单

| 脚本 | 用途 | 什么时候用 | 示例 |
|---|---|---|---|
| `tests/run_all.py` | 全量单元测试（618 例；自动跳过需要真实浏览器/登录态的用例） | 每次改动后端后、提交前、CI 必跑 | `python tests/run_all.py` |
| `tools/auto_test.py` | 自动回归测试：后端单测 + Python/app.js 语法 + 前端 Playwright 全流程 + 服务端日志检查 | 改完前端/后端后，模拟“人工测试员”跑一遍；`--quick` 只跑后端+语法（CI 友好） | `python tools/auto_test.py`<br>`python tools/auto_test.py --quick` |
| `tools/ai_flavor_check.py` | AI 味检测（0-100）：检查生成稿的机器味，与真人基准对比 | 生成效果前后对比、发布前自查 | `python tools/ai_flavor_check.py output`<br>`python tools/ai_flavor_check.py --zhihu data/published_answers_.json` |
| `tools/build_release.py` | 正式发版：门禁（git 干净/分支 main）→ 全量测试 → PyInstaller → Inno Setup 安装包 → SHA256；**版本号自动从 core/version.py 注入** | 发新版本时 | `python tools/build_release.py` |
| `tests/test_*.py` | 专项单测（草稿箱/快照/评分回退/并行窗口/大模型筛选/launcher/自动化/启动器设置等） | 定位具体模块问题时单独跑 | `python -m unittest tests.test_drafts` |
| `tools/archive/probes/` | 历史一次性探查脚本（已归档，只读参考）。其中 `probe_markdown_rebuild.py` 是逐块重建 markdown 的**合成 DOM 真机验证**（不登录不联网，改 walker 后跑一次）；`spike_tray.py` 是 M4 托盘常驻的真机验证（**直接跑生产代码** `launcher.TrayController` + 本地假 API，15 项检查） | 浏览器 DOM / 托盘排查时的历史参考 | `python tools/archive/probes/probe_markdown_rebuild.py`<br>`python tools/archive/probes/spike_tray.py` |

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
python tests/run_all.py                 # 514 例
python tools/auto_test.py               # 含前端 Playwright 全流程 + 日志检查
```

### 前端单独回归（改样式/JS 后）
```bash
python tools/auto_test.py               # 会起临时服务并逐项点击验证（五大模式/看板/草稿箱/自动化/设置）
```

### 托盘常驻 / 开机自启（M4，改 launcher 或 launcher_config 后）
```bash
# 自动部分：真机跑生产代码（托盘图标、关窗隐藏、菜单动作、干净退出，15 项检查）
python tools/archive/probes/spike_tray.py

# 手动部分（托盘是「人点出来」的功能，机器代替不了）——双击「AutoQuill 启动器」：
#   1) 窗口右上角 X → 窗口消失、托盘出现 AutoQuill 图标（Win11 在 ^ 折叠区里）
#   2) 双击托盘图标 → 控制台回来；右键 → 菜单五项齐全、状态行显示今日进度
#   3) 右键「暂停自动化」→ 控制台里状态变「已暂停」；再点「恢复自动化」→ 恢复运行
#   4) 设置 →「常驻与启动」：关窗行为切「直接退出」→ 再点 X → 程序真的退出
#   5) 勾选「开机后自动运行」→ 注册表里出现 HKCU\...\Run\AutoQuill（命令带 --tray）；
#      取消勾选 → 该项消失。测试完记得取消，别把自启留在机器上

# 关窗后「找不到托盘图标」怎么排查（2026-09-19 真机踩过）
#   a) 先看 logs/launcher.log：应有「托盘：已就绪（关窗 = 最小化到托盘）」；
#      若写着「托盘：拿不到窗口原生对象」或「等待窗口原生对象超时」→ 托盘没建起来
#      （根因：pywebview 的 start(func) 回调早于窗口创建 → 已用 wait_native 修掉）；
#   b) 图标可能在 Win11 的 ^ 折叠区里（新图标默认不进常驻区），点 ^ 找 AutoQuill；
#   c) 设置 →「常驻与启动」→「托盘状态」会显示最近一次自检结果与时间；
#   d) 托盘确实不可用时，关窗会退化为「最小化到任务栏」（程序不退出），任务栏按钮仍在。
```

### 运行时段语义（改 scheduler 的时段/收尾逻辑后）
```bash
python -m unittest tests.test_automation_core     # 含：时段外手动可执行 / 时段结束收尾 / 跨零点续排

# 手工验证（不改代码）：
#   1) 把「时段开始/结束」设成「当前时间之后 2 分钟 ~ 之后再 4 分钟」+ 每日 1 篇 → 点开始：
#      状态应显示「运行中 · 时段外待机」，到点自动跑一篇（时间轴出现执行中的块）；
#   2) 时段结束后：未执行的作业应变「已跳过」，通知里出现「运行时段已结束：今天完成 X 项…」；
#   3) 时段外点「立即执行下一个」→ 照做（通知里带「当前不在运行时段，手动执行照做」）；
#   4) 不点停止，把软件放到第二天（或临时把时段改到明天）→ 自动排出新一天的时间轴。
```

### 自动化排班（改 planner / 时段与配额设置后）
```bash
python -m unittest tests.test_automation_core     # 铺开/间隔下限/上限公式/续做/密集排班
python tools/archive/probes/shot_automation.py    # 造当天排班 + 截图（看动作是不是铺满时段）

# 手工核对数学关系（不改代码）：时段 930 分钟 ÷ 间隔 60 分钟 + 1 → 上限 16；
#   把「每日」设成 20 时，任务卡应变黄提示「多出的 4 篇排不下」，时间轴上多一条跳过说明。
```

### 生成效果与 AI 味对比
```bash
python tools/ai_flavor_check.py output            # 检测生成稿（期望均值随去 AI 味改进逐步下降）
python tools/ai_flavor_check.py --zhihu data/published_answers_2026-08-23.json  # 真人基准（约 1/100）
python tools/ai_flavor_check.py output/story_x.md # 单篇
```

### 发新版本（一键，2026-09-19 实跑校准）
```bash
# 1. 改版本号（唯一入口）
#    core/version.py  →  VERSION = "x.y.z"
# 2. 先提交：门禁要求工作区干净（未提交改动会直接拒绝构建）
#    git add -A && git commit -m "vx.y.z: ..."
# 3. 打包（自动：门禁+全量测试+PyInstaller+安装包+SHA256，并自动把版本号写入 iss）
#    --skip-browser = 测试走 tests/run_all.py（跳过需要真实浏览器/登录态的用例）
python tools/build_release.py --skip-browser
# 4. 提交构建脚本回写的 iss 版本号（此时工作区会多出这一处改动）
#    git add installer/AutoQuill.iss && git commit -m "chore: 安装器版本号同步 vx.y.z"
# 5. 打 tag + 推送 + 建 Release（两个资产：安装包 + sha256）
#    git tag vx.y.z && git push origin main --tags
#    gh release create vx.y.z --title "AutoQuill vx.y.z" \
#        --notes-file release/release_notes_x.y.z.md \
#        release/AutoQuill-Setup-x.y.z.exe release/AutoQuill-Setup-x.y.z.exe.sha256
```

发布说明的惯例：正文用 `CHANGELOG.md` 对应版本段 + 「测试」+「安装提示（SmartScreen / 数据保留 / sha256 校验）」；
发完把同一份说明存一份到 `release/release_notes_x.y.z.md`（`release/` 不入库，仅本机留档）。
发布后回下载一次安装包比对 sha256（`gh release download` + `certutil -hashfile ... SHA256`），并确认 CI 变绿。

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
