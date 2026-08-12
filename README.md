# AutoQuill v3.0

> 一套以 LLM 为认知中枢、以 **Playwright DOM 通道**为浏览器主交互、以 OCR/UIA 为降级感知的**类人 Agent 框架**。
>
> 当前成熟实例：知乎故事创作自动化（作者文风蒸馏 → 双层风格注入 → 批量生成发布）。未来可扩展至论文写作审稿迭代、后台系统操作等场景。

---

## 1. 架构哲学

### 1.1 系统定义

AutoQuill 不是一个面向单一平台的自动化脚本集合，而是一个能够在真实数字环境中像人一样 **"看 → 想 → 做 → 校验 → 修正"** 的执行型框架。

### 1.2 四条核心原则

| 原则 | 含义 |
|---|---|
| **DOM 直连优先** | 目标环境有 DOM/接口可编程交互时直接用 Playwright 原生操作（点击/输入/上传/读取），与屏幕坐标/分辨率解绑 |
| **能力优先于流程** | 脑/眼/手是长期稳定能力，不同业务只是对这些能力的不同编排 |
| **框架优先于实例** | 任何单一业务不应反向定义框架，抽象必须能同时容纳多种场景 |
| **行动闭环优先于单次调用** | 最小闭环单元是：观察→理解→规划→执行→校验→必要时恢复 |

### 1.3 分层架构（5 层）

```
┌──────────────────────────────────────────────────┐
│  Layer 5: Applications / Scenarios               │
│  applications/zhihu_story/  (知乎故事创作实例)     │
│   ├ browser_adapter.py  DOM 通道（Playwright）    │
│   ├ extractors.py       回答提取接缝（DOM 主/UIA  │
│   │                     OCR 降级）                │
│   ├ author_profiler.py  作者文风蒸馏               │
│   └ perception.py/a11y_probe.py/action.py 感知与  │
│     旧坐标操作（降级/调试）                       │
├──────────────────────────────────────────────────┤
│  Layer 4: Workflows (流程编排)                    │
│  workflows/zhihu.py  →  知乎专属：采集→蒸馏注入    │
│                        →生成→评分→发布            │
├──────────────────────────────────────────────────┤
│  Layer 3: Adapters (适配器)                       │
│  web_drivers/  →  Web LLM 驱动(DeepSeek/Aizex)   │
│  llm_api.py    →  API LLM 调用 + 风格双层注入     │
│  kb_manager.py →  知识库管理（默认停用）           │
│  meta_learner.py → 元知识自学习（已停用）          │
├──────────────────────────────────────────────────┤
│  Layer 2: Core Capabilities (核心能力)            │
│  core/story_text.py       →  故事文本管线（清洗/   │
│                               断句/格式修复校验）   │
│  core/story_workspace.py  →  长文故事工作区        │
│                               （crash-safe 持久化）│
│  ocr_utils.py     →  通用 OCR 感知原语（降级用）   │
│  desktop_utils.py →  桌面原语（降级/调试用）       │
├──────────────────────────────────────────────────┤
│  Layer 1: Runtime (运行时)                        │
│  config.py  →  配置注入与模型加载                   │
│  main.py    →  生命周期、日志、CLI 入口            │
│  tools/     →  开发期工具（debug_legacy 等，       │
│               不在运行时路径上）                  │
└──────────────────────────────────────────────────┘
```

---

## 2. 项目结构

```text
AutoQuillV3.0/
├── main.py                        # 统一入口（CLI、日志、流程调度）
├── config.py                      # 框架级配置 + 模型服务商加载
├── llm_api.py                     # LLM API 调用（流式 + 作者风格双层注入）
├── ocr_utils.py                   # 通用 OCR 感知原语（降级通道）
├── desktop_utils.py               # 桌面操作原语（降级/调试用）
├── kb_manager.py                  # 知识库（默认停用）
├── meta_learner.py                # 元知识自学习（已停用）
├── rich_progress.py               # Rich 终端进度面板
├── llm_token_tracker.py           # Token 用量追踪
│
├── core/                          # ★ Layer 2: 创作核心
│   ├── story_text.py              #   故事文本管线（清洗、断句、格式修复/校验、章节拆分）
│   └── story_workspace.py         #   长文故事工作区（文件持久化，crash-safe）
│
├── applications/                  # ★ Layer 5: 应用实例
│   ├── zhihu_story/               #   知乎故事创作
│   │   ├── config.py              #     知乎专用业务参数
│   │   ├── prompts.py             #     知乎专用 LLM 提示词
│   │   ├── browser_adapter.py     #     ★ DOM 主通道（Playwright 持久化浏览器）
│   │   ├── extractors.py          #     回答提取接缝（DOM 主 + UIA/OCR 降级）
│   │   ├── author_profiler.py     #     作者文风蒸馏（统计 + LLM 剖析 → data/authors/）
│   │   ├── perception.py          #     知乎专用感知（降级/调试）
│   │   ├── a11y_probe.py          #     UIA 无障碍树读取（降级）
│   │   └── action.py              #     旧坐标操作（降级）
│   └── image_gen/                 #   图像生成
│       ├── config.py              #     图像生成参数（输出目录、默认提示词）
│       └── prompts.py             #     图像生成 prompt
│
├── workflows/                     # Layer 4: 流程编排
│   ├── base.py                    #   WorkflowBase — 标准生命周期（选题→提取→生成→发布）
│   ├── zhihu.py                   #   ZhihuWorkflow — 知乎平台专属实现
│   └── image_gen.py               #   ImageGenWorkflow — 图像生成编排
│
├── web_drivers/                   # Layer 3: Web LLM 驱动适配器（DOM 语义化）
│   ├── base.py                    #   WebLLMDriver 基类（Playwright DOM 技术栈）
│   ├── deepseek.py                #   DeepSeek 网站驱动（DOM，--probe 可探测 selector）
│   └── legacy/                    #   旧 OCR/坐标驱动（仅 --image-gen 的 Aizex 使用）
│
├── tools/                         # 独立工具（不在运行时路径上）
│   ├── debug_legacy.py            #   OCR/UIA 时代调试命令（main.py 移出，CLI 保留）
│   ├── collect_author_pw.py       #   作者页多故事采集（Playwright 通道）
│   ├── collect_author.py          #   作者页采集（UIA 通道，旧版）
│   ├── collect_story.py           #   单篇故事采集（UIA 通道）
│   └── generate_with_author.py    #   作者风格驱动生成
│
├── config/                        # 配置文件目录
│   ├── llm_providers.json         #   模型服务商注册表（含 API Key，不提交 Git）
│   ├── llm_providers.example.json #   注册表示例模板
│   ├── model_pricing.json         #   模型价格参考
│   └── browser_state.json         #   浏览器登录态（Cookie，不提交 Git）
│
├── data/                          # 运行时数据目录
│   ├── collected_stories.jsonl    #   故事采集库（作者页批量采集输出，含点赞数/发表时间）
│   ├── authors/                   #   文风签名（{作者}.json + _general.json 通用风格）
│   ├── browser_profile/           #   Playwright 持久化浏览器 profile（不提交 Git）
│   └── stories/                   #   生成的故事成品
│
├── output/                        # 生成内容输出
├── logs/                          # 运行日志
├── screenshots/                   # 调试截图
└── images/                        # 图标模板（用于 Web 驱动的完成/复制按钮匹配）
```

---

## 3. 核心概念

### 3.1 双脑模式

系统天然支持两种 LLM 调用通道，对调用方透明：

| 模式 | 说明 | 适用场景 |
|---|---|---|
| **API** (`LLM_MODE = "api"`) | 直接调用 LLM API，快速稳定 | 日常批量生成 |
| **Web** (`LLM_MODE = "web"`) | 通过浏览器操作网页版 LLM | 免费使用、API 不可用时 |

两者对流程层透明：统一经 `llm_api.py`（API 通道）与 `web_drivers/`（Web 通道）适配。

### 3.2 能力分层：原语 + 创作核心

系统能力由**原语函数**与**创作核心**组合，不做多余的抽象壳：

- **浏览器 DOM 通道**（`applications/zhihu_story/browser_adapter.py`）：Playwright 直连独立 Edge 实例，全部交互通过 DOM 指令（原生 `element.click()`、`set_input_files` 上传）触发——与物理鼠标/屏幕坐标/分辨率完全解绑，运行期间用户可干其他事
- **感知原语**（`ocr_utils.py`、`applications/zhihu_story/perception.py`、`a11y_probe.py`）：区域 OCR、文字查找、图标匹配、footer 解析、UIA 无障碍树读取（DOM 通道失败时的降级手段）
- **操作原语**（`desktop_utils.py`、`applications/zhihu_story/action.py`）：浏览器导航、窗口焦点、粘贴滚动、坐标校准（仅降级路径使用）
- **创作核心**（`core/story_text.py`、`core/story_workspace.py`）：纯文本管线（清洗、断句、格式修复与校验、章节拆分）与 crash-safe 故事工作区——零 GUI/网络依赖，可独立单元测试
- **回答提取接缝**（`applications/zhihu_story/extractors.py`）：UIA/OCR 双通道统一为 `(title, answer, footer)` 三元组，主通道失败（异常/超时/正文过短/缺赞同数）自动回退保底通道

流程层（`workflows/`）直接编排这些原语与核心函数，无需经过中间抽象层。

### 3.3 工作流生命周期

`WorkflowBase` 定义了内容创作的标准流水线：

```
选题 → 提取内容 → 提炼配方 → 生成故事 → 评分筛选 → 发布
```

- **选题**：默认从**创作中心「推荐问题」页**（`/creator/featured-question/recommend`，原 workflow 入口）解析候选——该页候选池为「等你来答」的优质问题，对写作选题天然对口（实测 40 候选全为故事/小说类），替代首页推荐流全品类大杂烩。按互动评分（关注×(回答+1)，热度 ×2）自动排序；关键词白名单强制生效——首屏无故事类问题时滚动扩池重扫，仍无命中则明确报错，绝不静默选非故事热门话题（线上曾因此选到「美伊战争」）
- **提取**：DOM 读取首答全文与互动数据；「撤销删除」等检测同样走 DOM，不依赖 OCR
- **配方提炼**：从优质回答中提取叙事配方（人设、结构、节奏、冲突设计等）
- **生成**：基于配方 + 标题 + 可选素材，LLM 创作故事；可注入已提炼的作者技能签名（`AUTHOR_PROFILE`）
- **评分**：多维度自动评分（文学性、知乎调性、故事张力和节奏、爽点密度等）
- **发布**：写回答 → 富文本粘贴（md 转 HTML + 剪贴板 + 真实 Ctrl+V，Draft.js 编辑器把 `<b>`/`<p>` 真实落盘，`## **1**` 不再以符号原样显示）；成功判定轮询服务端草稿 API——前端保存提示 toast 在程序化写入后可能不出现，以服务端草稿内容为准（可验证）。发布路径单跳定位：已在目标问题页时跳过整页重载，成功后无收尾刷新

> 浏览器操作与检测环节（点击、滚动、按钮查找、可回答性判断、数据读取）均走 DOM 通道，与鼠标/坐标解绑；OCR/UIA 仅在 DOM 失败时作为降级通道。发布不采用「导入文档 → 文件上传」路径：上传 API 全 200 但服务端草稿不更新（知乎程序化导入落盘不可靠，仅空草稿时偶发成功），且导入同样不转换 md 符号；富文本粘贴是唯一能落盘格式的可验证通道。

### 3.4 自进化系统（已停用）

```
批量生成 → 评分反馈 → 入蒸馏池 → 达阈值触发蒸馏 → 新版创作手册
                                                      ↓
                                          下次生成时注入新手册
```

- **知识库**（`kb_manager.py`）：配方提炼、题材分类、加权随机选取、评分回写
- **元学习**（`meta_learner.py`）：跨任务经验积累，LLM 蒸馏旧手册与新评分池，有机融合产出进化版手册

> **已停用**：元学习（评分回写/入池/蒸馏）默认关闭（`META_INJECT_DEFAULT`），知识库不进
> 入生成 prompt。风格学习改由**作者文风蒸馏**（见 3.6）承担——目标是模仿指定作者的
> 写作技法，而非积累全局创作手册。代码保留以便需要时恢复。

### 3.5 向后兼容的重导出模式

应用专用函数（知乎 footer 解析、推荐页解析等）已从通用模块迁移至 `applications/zhihu_story/`，但通过在通用模块末尾重导出，所有历史导入路径完全兼容：

```python
# 旧代码无需修改，以下导入仍然有效
from ocr_utils import parse_recommend_questions, extract_zhihu_question_and_answer
from desktop_utils import get_bounds

# 新代码推荐使用应用包直接导入
from applications.zhihu_story.perception import parse_recommend_questions
from applications.zhihu_story.action import get_bounds
```

### 3.6 作者风格链路（采集 → 提炼 → 双层注入生成）

系统支持针对某位知乎作者的风格学习与模仿生成，三个模块可独立运行：

```text
① 采集    tools/collect_author_pw.py（Playwright 通道，作者回答列表页批量采集）
② 提炼    python -m applications.zhihu_story.author_profiler 作者名
③ 生成    主流程自动注入（llm_api.build_story_prompt 双层注入）
```

- **采集**（`tools/collect_author_pw.py`）：作者回答列表页滚动加载 → 逐篇进详情页提取全文
  + 发表时间 + 点赞数 → 追加写入 `data/collected_stories.jsonl`，支持断点续采
- **提炼**（`applications/zhihu_story/author_profiler.py`）：确定性文本统计（句长、短句比、
  对话密度、第一人称密度、开头/结尾片段等）+ LLM 剖析，产出两层签名 →
  `data/authors/_general.json`（通用写作风格，30 篇跨作者样本）与
  `data/authors/{作者名}.json`（每作者专用风格）
- **样本权重**：经验权重 = log1p(点赞数) × 新鲜度（90 天内 1.0，730 天内线性衰减至
  0.3，未知日期 0.6）——高赞新作在风格剖析中占比更高
- **双层注入**（`llm_api.build_story_prompt`）：先注入通用风格层，再叠加作者专用层，
  日志以 `+通用风格 +作者:镜中花` 标记；生成时模仿其技法（禁止搬运情节）

---

## 4. 快速开始

### 4.1 环境要求

- **操作系统**：Windows 10/11（推荐）
- **Python**：3.10+
- **浏览器**：Microsoft Edge

### 4.2 安装依赖

```bash
pip install -r requirements.txt
```

若用浏览器登录态（见 4.4）首次需安装 Edge 驱动：

```bash
python -m playwright install msedge
```

> `pyautogui` / `rich` / `rapidocr` 等仅为旧坐标调试工具使用（`tools/`、`--calibrate`）；
> 主流程（DOM 通道 + Web 控制台）不依赖它们。

### 4.3 配置模型

```bash
# 1. 复制配置模板
cp config/llm_providers.example.json config/llm_providers.json

# 2. 编辑 config/llm_providers.json，填入你的 API Key
# 3. 在 config.py 中修改 LLM_PROVIDER / LLM_MODEL_ID 切换模型
```

### 4.4 浏览器登录态

DOM 通道使用 `data/browser_profile/` 持久化浏览器，登录知乎后登录态保存于
`config/browser_state.json`。两者均已被 `.gitignore` 隔离，**等同账号凭证，勿上传**。

### 4.5 功能验证

```bash
python main.py --test-api    # 测试 LLM API 连通性
python -m applications.zhihu_story.browser_adapter --login   # 验证浏览器通道并登录
```

### 4.6 正式运行

```bash
python main.py                # 批量模式（默认）：收集素材→生成→发布
python main.py --single       # 传统模式：逐轮生成即发布
python main.py --image-gen    # 图像生成模式
```

> 主流程不再需要坐标校准；`--calibrate`/`--test-ocr` 等仅旧坐标调试工具使用。

### 4.7 一键启动（Web 控制台）

**方式一：启动器 exe（推荐）**——双击项目根目录的 `AutoQuill.exe`，自动完成：
检查 Python 环境与依赖 → 后台启动服务 → 自动打开浏览器
（http://127.0.0.1:8787）。**关闭启动器窗口即停止服务**；服务已在运行时重复
双击只会重新打开浏览器，不会重复启动。

- exe 必须与项目根目录（与 `main.py` 同级）放在一起
- 首次使用需已安装依赖（见 4.2）并完成 API Key / 登录态配置（见 4.3、4.4）
- 服务日志见 `logs/webui.log`；重新打包：`pip install pyinstaller && python -m PyInstaller --onefile --console --name AutoQuill --specpath build --distpath dist tools/launcher.py`

**方式二：直接运行**——`python main.py --web` 后手动打开 http://127.0.0.1:8787

Web 控制台支持：环节测试（选题/提取/生成）、单轮/批量运行、实时日志、历史故事查看、
配置速览，以及**运行时切换模型**（服务商 + 模型下拉框，下次生成立即生效并持久化，
见 `config/webui_model.json`，已 gitignore 不入库）。

---

## 5. CLI 命令参考

### 主流程

| 命令 | 说明 |
|---|---|
| `python main.py` | 批量模式（默认）：收集素材 → 生成（注入作者风格）→ 发布 |
| `python main.py --single` | 传统模式：逐轮生成即发布，可设目标轮数 |
| `python main.py --image-gen` | 图像生成模式（Aizex 绘图，可设生成张数） |
| `python main.py --resume` | 断点续跑批量流程 |

### 工具与调试（旧坐标时代，见 `tools/debug_legacy.py`）

| 命令 | 说明 |
|---|---|
| `python main.py --calibrate` | 交互式屏幕坐标校准 |
| `python main.py --test-ocr` | 测试 OCR 区域识别 |
| `python main.py --debug-ocr-region` | 进入回答页并保存 OCR 区域标注图 |
| `python main.py --probe-a11y [--url URL]` | 只读导出当前 Edge 的无障碍树 |
| `python main.py --test-api` | 测试 LLM API 连通性 |

### 作者风格工具

| 命令 | 说明 |
|---|---|
| `python tools/collect_author_pw.py [--count N]` | 批量采集作者回答（含点赞/发表时间；经 Playwright MCP 从 Claude 会话内运行） |
| `python -m applications.zhihu_story.author_profiler 作者名` | 提炼文风签名（通用+专用双层） |
| `python tools/generate_with_author.py --question "..." --author 作者名` | 按作者风格生成 |

### 知识库管理（`kb_manager.py`）

| 命令 | 说明 |
|---|---|
| `python kb_manager.py --stats` | 查看知识库统计 |
| `python kb_manager.py --rebuild` | 从 `data/raw_materials.jsonl` 重建知识库 |
| `python kb_manager.py --compress` | 压缩合并知识库 |
| `python kb_manager.py --show [题材]` | 查看指定题材的配方 |
| `python kb_manager.py --ranking` | 查看配方评分排行 |

---

## 6. 配置参考

### 框架级配置（`config.py` 部分关键项）

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `LLM_MODE` | LLM 调用模式 | `"api"` |
| `LLM_PROVIDER` | 故事生成服务商 | `"DeepSeek"` |
| `LLM_MODEL_ID` | 故事生成模型 | `"deepseek-v4-flash"` |
| `KB_PROVIDER` | 知识库任务服务商 | `"DeepSeek"` |
| `KB_MODEL_ID` | 知识库任务模型 | `"deepseek-v4-flash"` |
| `QUESTION_SELECT_MODE` | 选题模式 (`"auto"`/`"manual"`) | `"auto"` |
| `STORY_MATERIAL_MODE` | 素材模式 (`"answer_only"`/`"recipe"`/`"meta"`) | `"meta"` |
| `ENABLE_STORY_FILTER` | 是否启用 LLM 筛选问题 | `True` |
| `DEFAULT_BATCH_GENERATE_COUNT` | 默认批量生成数量 | `3` |

### 知乎专用配置（`applications/zhihu_story/config.py`）

| 配置项 | 说明 |
|---|---|
| `OCR_MAX_SCROLLS` | 回答 OCR 最大滚动屏数 |
| `MIN_ANSWER_LENGTH` | 回答最短合格长度（字符） |
| `MAX_ANSWER_RETRIES` | 回答不合格时重试次数 |
| `ZHIHU_RECOMMEND_URL` | 知乎推荐页 URL |
| `BATCH_QUESTIONS_PER_PAGE` | 批量模式下每轮爬取问题数 |

---

## 7. 扩展指南

### 7.1 新增应用实例

以"论文写作审稿迭代"为例：

```
applications/paper_review/
├── __init__.py
├── config.py        # 论文审稿专用参数
├── prompts.py       # 审稿专用 prompt
├── perception.py    # 论文页面专用感知（如 PDF 文本抽取）
└── action.py        # 论文平台专用操作（如投稿系统导航）
```

### 7.2 新增工作流

继承 `WorkflowBase`，实现平台专属的 4 个步骤：

```python
from workflows.base import WorkflowBase

class PaperReviewWorkflow(WorkflowBase):
    name = "paper_review"

    def select_topic(self):       ...  # 选题 → 返回论文 URL
    def extract_content(self):    ...  # 提取论文内容
    def publish(self, story, ...): ...  # 提交审稿意见
```

### 7.3 新增 Web LLM 驱动

继承 `WebLLMDriver`，实现 `setup()` + `wait_complete()`：

```python
from web_drivers.base import WebLLMDriver

class NewSiteDriver(WebLLMDriver):
    name = "newsite"

    def setup(self):           ...  # 首次创建会话
    def wait_complete(self):   ...  # 等待生成完毕
```

---

## 8. 模块职责速查

| 模块 | 层级 | 职责 |
|---|---|---|
| `main.py` | Runtime | CLI 入口、DPI 适配、日志、流程调度 |
| `config.py` | Runtime | 框架级配置、模型服务商加载 |
| `core/story_text.py` | Core | 故事文本管线：清洗、断句、格式修复/校验、章节拆分、评分 JSON 解析 |
| `core/story_workspace.py` | Core | 长文故事工作区：文件持久化、crash-safe 读写 |
| `llm_api.py` | Adapter | LLM API 调用、SSE 流式解析、知乎 prompt 构造 |
| `ocr_utils.py` | Core | 通用 OCR 原语：引擎初始化、区域识别、文字查找、图标匹配、行去重 |
| `desktop_utils.py` | Core | 通用桌面原语：浏览器导航、窗口焦点、坐标校准、进度面板 |
| `web_drivers/` | Adapter | Web LLM 驱动：DeepSeek / Aizex 适配 + 并行调度 |
| `kb_manager.py` | Adapter | 知识库：配方提炼、题材分类、评分回写、压缩重建 |
| `meta_learner.py` | Adapter | 元学习：评分池蒸馏 → 创作手册进化 |
| `workflows/base.py` | Workflow | 标准生命周期基类 |
| `workflows/zhihu.py` | Workflow | 知乎平台专属流程 |
| `workflows/image_gen.py` | Workflow | 图像生成编排（Aizex 绘图→下载） |
| `applications/zhihu_story/` | App | 知乎专用配置、prompt、感知函数、UIA/OCR 提取接缝、操作函数 |
| `applications/image_gen/` | App | 图像生成配置、prompt |

---

## 9. 注意事项

- 运行中可将鼠标移到屏幕**左上角**触发 `pyautogui.FailSafeException` 紧急停止（仅旧坐标通道）
- 日志文件位于 `logs/` 目录，按时间戳命名
- `config/llm_providers.json` 含 API Key、`config/browser_state.json` 与 `data/browser_profile/` 含登录态 Cookie，**均不要提交到版本库**
- 若 API 调用报错，请优先检查 `config/llm_providers.json` 中的 Key 和网络连通性
- 批量模式运行中请勿用鼠标/键盘操作浏览器——DOM 通道不受影响，但降级路径（OCR/UIA）依赖屏幕状态

### 9.1 API Key 隔离与上传防护

真实 Key **只允许**放在 `config/llm_providers.json`（已在 `.gitignore` 中，永不上传）；
模板 `config/llm_providers.example.json` 中**只允许占位符**（`sk-your-xxx-key-here` 形式）。

仓库内置 pre-commit 钩子（`.githooks/pre-commit`）：任何提交若暂存区新增了疑似真实
API Key（`sk-` + 24 位十六进制），提交会被自动阻止。克隆仓库后需手动启用一次：

```bash
git config core.hooksPath .githooks
```

---

## 10. 版本历史

| 版本 | 主要变更 |
|---|---|
| **v3.0** | 大版本：核心抽象层精简（删除 `core/action`、`core/mind`、`core/perception` 空壳，`llm_api.py` 净减 1400+ 行）；问题页懒加载重写（检测-下滑-回位-展开-稳定五步）；参考采样模式（前 3000 字直接注入，零 LLM 调用）；**Web 控制台**（FastAPI + SSE 实时日志，`python main.py --web` 本地运行/测试全流程）；取消钩子（Web 停止按钮安全中断 Playwright 线程）；零宽空格剥离修复；214 项单元测试 |
| **v2.3** | 5 层架构重构：脑/眼/手抽象接口（`core/` 包）、应用插件化（知乎专用代码迁入 `applications/zhihu_story/`）、SSE 流式循环合并消除 140 行重复代码 |
| v2.3+ | 作者文风蒸馏升级（41 篇样本、点赞×新鲜度权重、通用+作者双层注入）；DOM 通道全面接管（Playwright 持久化浏览器，坐标/OCR 降级）；元学习停用；main.py 拆分（调试段移入 `tools/debug_legacy.py`，去掉坐标检查与 ensure_edge 残留）；E2E 四轮验证（看门狗+心跳日志防误杀） |
| v2.2 | Web 驱动并行模式、快速版 OCR 提取（截屏缓存+并行）、元知识自学习系统 |
| v2.1 | 目录结构重整（`config/`、`data/` 分层）、知识库评分回写与加权选取 |
| v2.0 | 双脑模式（API + Web）、多平台 Web 驱动、批量流水线、知识库冷启动 |
