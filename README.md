# AutoQuill v2.3

> 一套以 LLM 为认知中枢、以视觉/OCR 为环境感知、以桌面与网页控制为动作执行基础的**类人 Agent 框架**。
>
> 当前成熟实例：知乎故事创作自动化。未来可扩展至论文写作审稿迭代、后台系统操作等场景。

---

## 1. 架构哲学

### 1.1 系统定义

AutoQuill 不是一个面向单一平台的自动化脚本集合，而是一个能够在真实数字环境中像人一样 **"看 → 想 → 做 → 校验 → 修正"** 的执行型框架。

### 1.2 四条核心原则

| 原则 | 含义 |
|---|---|
| **类人而非纯接口** | 不假设目标环境提供理想 API，依靠截图、OCR、鼠标键盘等类人交互完成任务 |
| **能力优先于流程** | 脑/眼/手是长期稳定能力，不同业务只是对这些能力的不同编排 |
| **框架优先于实例** | 任何单一业务不应反向定义框架，抽象必须能同时容纳多种场景 |
| **行动闭环优先于单次调用** | 最小闭环单元是：观察→理解→规划→执行→校验→必要时恢复 |

### 1.3 分层架构（5 层）

```
┌──────────────────────────────────────────────────┐
│  Layer 5: Applications / Scenarios               │
│  applications/zhihu_story/  (知乎故事创作实例)     │
│  未来: paper_review/, backend_ops/, ...          │
├──────────────────────────────────────────────────┤
│  Layer 4: Workflows (流程编排)                    │
│  workflows/base.py  →  标准生命周期               │
│  workflows/zhihu.py  →  平台专属步骤              │
├──────────────────────────────────────────────────┤
│  Layer 3: Adapters (适配器)                       │
│  web_drivers/  →  Web LLM 驱动(DeepSeek/Aizex)   │
│  llm_api.py    →  API LLM 调用                   │
│  kb_manager.py →  知识库管理                      │
│  meta_learner.py → 元知识自学习                   │
├──────────────────────────────────────────────────┤
│  Layer 2: Core Capabilities (核心能力)            │
│  core/mind/       →  脑: LLM 认知抽象             │
│  core/perception/ →  眼: OCR/视觉感知抽象          │
│  core/action/     →  手: 桌面/浏览器操作抽象       │
│  ocr_utils.py     →  通用 OCR 感知原语            │
│  desktop_utils.py →  通用桌面/浏览器操作原语       │
├──────────────────────────────────────────────────┤
│  Layer 1: Runtime (运行时)                        │
│  config.py  →  配置注入与模型加载                   │
│  main.py    →  生命周期、日志、CLI 入口            │
└──────────────────────────────────────────────────┘
```

---

## 2. 项目结构

```text
AutoQuillV2.3/
├── main.py                        # 统一入口（CLI、日志、流程调度）
├── config.py                      # 框架级配置 + 模型服务商加载
├── llm_api.py                     # LLM API 调用（通用 + 知乎 prompt 构造）
├── ocr_utils.py                   # 通用 OCR 感知原语（引擎、查找、行合并、图标匹配）
├── desktop_utils.py               # 通用桌面操作原语（浏览器、窗口、坐标、进度面板）
├── kb_manager.py                  # 自生长知识库：配方提炼、分类、评分、压缩
├── meta_learner.py                # 跨任务元知识自学习（蒸馏池→创作手册进化）
├── rich_progress.py               # Rich 终端进度面板
├── llm_token_tracker.py           # Token 用量追踪
│
├── core/                          # ★ Layer 2: 核心能力抽象接口
│   ├── mind/                      #   脑 — LLM 认知中枢
│   │   ├── base.py                #     Mind 抽象类
│   │   ├── tasks.py               #     MindTask / GenerateTask / ClassifyTask / ...
│   │   └── results.py             #     MindResult
│   ├── perception/                #   眼 — 环境感知
│   │   ├── base.py                #     Perception 抽象类
│   │   └── observation.py         #     Observation
│   └── action/                    #   手 — 动作执行
│       ├── base.py                #     Action 抽象类
│       ├── commands.py            #     ActionCommand
│       └── results.py             #     ActionResult
│
├── applications/                  # ★ Layer 5: 应用实例
│   ├── zhihu_story/               #   知乎故事创作
│   │   ├── config.py              #     知乎专用业务参数
│   │   ├── prompts.py             #     知乎专用 LLM 提示词
│   │   ├── perception.py          #     知乎专用感知（footer 解析、推荐页解析、内容提取）
│   │   └── action.py              #     知乎专用操作（编辑器等待、坐标准备）
│   └── image_gen/                 #   图像生成
│       ├── config.py              #     图像生成参数（输出目录、默认提示词）
│       └── prompts.py             #     图像生成 prompt
│
├── workflows/                     # Layer 4: 流程编排
│   ├── base.py                    #   WorkflowBase — 标准生命周期（选题→提取→生成→发布）
│   ├── zhihu.py                   #   ZhihuWorkflow — 知乎平台专属实现
│   └── image_gen.py               #   ImageGenWorkflow — 图像生成编排
│
├── web_drivers/                   # Layer 3: Web LLM 驱动适配器
│   ├── base.py                    #   WebLLMDriver 基类
│   ├── deepseek.py                #   DeepSeek 网站驱动
│   ├── aizex.py                   #   Aizex 网站驱动
│   └── parallel_runner.py         #   并行 Web 模式多标签页调度
│
├── config/                        # 配置文件目录
│   ├── llm_providers.json         #   模型服务商注册表（含 API Key，不提交 Git）
│   ├── llm_providers.example.json #   注册表示例模板
│   └── model_pricing.json         #   模型价格参考
│
├── data/                          # 运行时数据目录
│   ├── coordinates.json           #   屏幕坐标校准数据
│   ├── knowledge_base.json        #   知识库（配方库）
│   ├── raw_materials.jsonl        #   原始素材采集归档
│   └── meta/                      #   元知识自学习
│       ├── meta_knowledge.md      #     当前版创作手册
│       ├── pool_pending.jsonl     #     待蒸馏评分池
│       ├── pool_consumed.jsonl    #     已蒸馏归档
│       └── history/               #     历史版本手册（自动版本化）
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

两者都是 `Mind` 抽象的不同适配器，流程层无需感知底层差异。

### 3.2 脑/眼/手 抽象（Core Capabilities）

```
Brain (Mind)          Eyes (Perception)      Hands (Action)
─────────────────     ─────────────────      ─────────────────
Mind.run(task)   →    Perception.observe()   Action.execute(cmd)
  ├─ generate          ├─ ocr_region           ├─ click
  ├─ classify          ├─ find_text            ├─ hotkey
  ├─ score             ├─ find_icon            ├─ paste/scroll
  ├─ extract           ├─ capture_screen       ├─ navigate
  ├─ plan              └─ inspect_state        └─ focus/upload
  └─ rewrite
```

能力以**接口**定义（`core/mind/base.py` 等），实例以**原语**实现（`ocr_utils.py`、`desktop_utils.py`、`llm_api.py`），流程以**编排**调用。

### 3.3 工作流生命周期

`WorkflowBase` 定义了内容创作的标准流水线：

```
选题 → 提取内容 → 提炼配方 → 生成故事 → 评分筛选 → 发布
```

- **选题**：从推荐页 OCR 解析问题列表，按热度/评分自动排序
- **提取**：OCR 滚动阅读全文，解析 footer（赞同/评论/收藏/发布时间）
- **配方提炼**：从优质回答中提取叙事配方（人设、结构、节奏、冲突设计等）
- **生成**：基于配方 + 标题 + 可选素材，LLM 创作故事
- **评分**：多维度自动评分（文学性、知乎调性、故事张力和节奏、爽点密度等）
- **发布**：自动粘贴到知乎编辑器

### 3.4 自进化系统

```
批量生成 → 评分反馈 → 入蒸馏池 → 达阈值触发蒸馏 → 新版创作手册
                                                      ↓
                                          下次生成时注入新手册
```

- **知识库**（`kb_manager.py`）：配方提炼、题材分类、加权随机选取、评分回写
- **元学习**（`meta_learner.py`）：跨任务经验积累，LLM 蒸馏旧手册与新评分池，有机融合产出进化版手册

### 3.5 向后兼容的重导出模式

应用专用函数（知乎 footer 解析、推荐页解析等）已从通用模块迁移至 `applications/zhihu_story/`，但通过在通用模块末尾重导出，所有历史导入路径完全兼容：

```python
# 旧代码无需修改，以下导入仍然有效
from ocr_utils import parse_recommend_questions, extract_zhihu_question_and_answer
from desktop_utils import get_bounds, wait_editor_ready

# 新代码推荐使用应用包直接导入
from applications.zhihu_story.perception import parse_recommend_questions
from applications.zhihu_story.action import get_bounds
```

---

## 4. 快速开始

### 4.1 环境要求

- **操作系统**：Windows 10/11（推荐）
- **Python**：3.10+
- **浏览器**：Microsoft Edge

### 4.2 安装依赖

```bash
pip install pyautogui pyperclip requests rapidocr-onnxruntime pillow numpy
```

可选（提升图标匹配精度）：

```bash
pip install opencv-python
```

可选（彩色终端进度面板）：

```bash
pip install rich
```

### 4.3 配置模型

```bash
# 1. 复制配置模板
cp config/llm_providers.example.json config/llm_providers.json

# 2. 编辑 config/llm_providers.json，填入你的 API Key
# 3. 在 config.py 中修改 LLM_PROVIDER / LLM_MODEL_ID 切换模型
```

### 4.4 首次校准

```bash
python main.py --calibrate
```

按提示将鼠标移到知乎页面正文区域的四个边界（左上/右上/左下/右下），校准结果保存到 `data/coordinates.json`。

### 4.5 功能验证

```bash
python main.py --test-ocr    # 测试 OCR 区域识别
python main.py --test-api    # 测试 LLM API 连通性
```

### 4.6 正式运行

```bash
python main.py                # 批量模式（默认）：收集→生成→发布→元学习
python main.py --single       # 传统模式：逐轮生成即发布
python main.py --use-meta     # 注入元知识到生成 prompt
python main.py --image-gen    # 图像生成模式
```

---

## 5. CLI 命令参考

### 主流程

| 命令 | 说明 |
|---|---|
| `python main.py` | 批量模式（默认）：收集素材 → 并行生成 → 发布 → 元学习 |
| `python main.py --single` | 传统模式：逐轮生成即发布，可设目标轮数 |
| `python main.py --use-meta` | 注入元知识到生成 prompt（可搭配批量/传统模式） |
| `python main.py --no-meta` | 强制不注入元知识（覆盖 config 默认值） |
| `python main.py --image-gen` | 图像生成模式（Aizex 绘图，可设生成张数） |

### 工具与调试

| 命令 | 说明 |
|---|---|
| `python main.py --calibrate` | 交互式屏幕坐标校准 |
| `python main.py --test-ocr` | 测试 OCR 区域识别 |
| `python main.py --test-api` | 测试 LLM API 连通性 |

### 知识库管理（`kb_manager.py`）

| 命令 | 说明 |
|---|---|
| `python kb_manager.py --stats` | 查看知识库统计 |
| `python kb_manager.py --cold-start` | 自动采集文章并冷启动知识库 |
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
| `config.py` | Runtime | 框架级配置、模型服务商加载、知乎配置重导出 |
| `core/mind/` | Core | 脑抽象：`Mind`, `MindTask`, `MindResult` |
| `core/perception/` | Core | 眼抽象：`Perception`, `Observation` |
| `core/action/` | Core | 手抽象：`Action`, `ActionCommand`, `ActionResult` |
| `llm_api.py` | Adapter | LLM API 调用、SSE 流式解析、知乎 prompt 构造 |
| `ocr_utils.py` | Core | 通用 OCR 原语：引擎初始化、区域识别、文字查找、图标匹配、行去重 |
| `desktop_utils.py` | Core | 通用桌面原语：浏览器导航、窗口焦点、坐标校准、进度面板 |
| `web_drivers/` | Adapter | Web LLM 驱动：DeepSeek / Aizex 适配 + 并行调度 |
| `kb_manager.py` | Adapter | 知识库：配方提炼、题材分类、评分回写、压缩重建 |
| `meta_learner.py` | Adapter | 元学习：评分池蒸馏 → 创作手册进化 |
| `workflows/base.py` | Workflow | 标准生命周期基类 |
| `workflows/zhihu.py` | Workflow | 知乎平台专属流程 |
| `workflows/image_gen.py` | Workflow | 图像生成编排（Aizex 绘图→下载） |
| `applications/zhihu_story/` | App | 知乎专用配置、prompt、感知函数、操作函数 |
| `applications/image_gen/` | App | 图像生成配置、prompt |

---

## 9. 注意事项

- 运行中可将鼠标移到屏幕**左上角**触发 `pyautogui.FailSafeException` 紧急停止
- 日志文件位于 `logs/` 目录，按时间戳命名
- `config/llm_providers.json` 含 API Key，**不要提交到版本库**
- 若 OCR 不稳定，请重新执行 `--calibrate` 校准坐标
- 若 API 调用报错，请优先检查 `config/llm_providers.json` 中的 Key 和网络连通性

---

## 10. 版本历史

| 版本 | 主要变更 |
|---|---|
| **v2.3** | 5 层架构重构：脑/眼/手抽象接口（`core/` 包）、应用插件化（知乎专用代码迁入 `applications/zhihu_story/`）、SSE 流式循环合并消除 140 行重复代码 |
| v2.2 | Web 驱动并行模式、快速版 OCR 提取（截屏缓存+并行）、元知识自学习系统 |
| v2.1 | 目录结构重整（`config/`、`data/` 分层）、知识库评分回写与加权选取 |
| v2.0 | 双脑模式（API + Web）、多平台 Web 驱动、批量流水线、知识库冷启动 |
