# AutoQuill 开发者文档

> 面向开发者的架构说明、运行方式、CLI 命令、配置参考与扩展指南。
> 使用者请阅读 [README.md](../README.md)。

---

## 1. 系统概览

AutoQuill 是一套以 LLM 为认知中枢、以 **Playwright DOM 通道**为唯一浏览器交互方式的类人 Agent 框架（OCR/坐标时代代码已整体归档至 `archive/`）。当前成熟实例：知乎故事创作自动化（作者文风蒸馏 → 双层风格注入 → 批量生成发布）。

### 1.1 四条核心原则

| 原则 | 含义 |
|---|---|
| **DOM 直连优先** | 目标环境有 DOM/接口可编程交互时直接用 Playwright 原生操作，与屏幕坐标/分辨率解绑 |
| **能力优先于流程** | 脑/眼/手是长期稳定能力，不同业务只是对这些能力的不同编排 |
| **框架优先于实例** | 任何单一业务不应反向定义框架，抽象必须能同时容纳多种场景 |
| **行动闭环优先于单次调用** | 最小闭环单元是：观察→理解→规划→执行→校验→必要时恢复 |

### 1.2 分层架构（5 层）

```
┌──────────────────────────────────────────────────┐
│  Layer 5: Applications / Scenarios               │
│  applications/zhihu_story/  (知乎故事创作实例)     │
│   ├ browser_adapter.py  DOM 通道（Playwright）    │
│   │                    + 浏览器工厂注册           │
│   ├ collector.py        作者故事采集（断点续采）   │
│   ├ author_profiler.py  作者文风蒸馏               │
│   └ prompts.py/config.py 业务 prompt 与参数        │
├──────────────────────────────────────────────────┤
│  Layer 4: Workflows (流程编排)                    │
│  workflows/zhihu.py  →  知乎专属：采集→蒸馏注入    │
│                        →生成→评分→发布            │
├──────────────────────────────────────────────────┤
│  Layer 3: Adapters (适配器)                       │
│  web_drivers/  →  Web LLM 驱动(DeepSeek)         │
│   ├ browser_pool.py  浏览器基础设施（共享单例/     │
│   │                 取消钩子/有界交互；工厂由      │
│   │                 应用层注册，不依赖上层）       │
│   ├ deepseek.py      DeepSeek 网页版驱动+登录判定  │
│   └ base.py          WebLLMDriver 基类            │
│  llm_api.py    →  API LLM 调用 + 风格双层注入     │
│  kb_manager.py →  知识库管理（默认停用）           │
├──────────────────────────────────────────────────┤
│  Layer 2: Core Capabilities (核心能力)            │
│  core/story_text.py  →  故事文本管线（清洗/断句/   │
│                          格式修复校验）            │
│  core/paths.py       →  程序/数据目录解析（安装版   │
│                          分离）                   │
│  core/version.py     →  版本号单一来源             │
│  story_* 模块        →  生成编排/prompt/评分       │
│  desktop_utils.py    →  桌面原语                  │
├──────────────────────────────────────────────────┤
│  Layer 1: Runtime (运行时)                        │
│  main.py    →  生命周期、日志、CLI 入口            │
│  config/    →  框架配置 + JSON 运行时数据          │
│  webui/     →  本地 Web 控制台（FastAPI+SSE）      │
│  tools/     →  开发期工具与打包脚本                │
└──────────────────────────────────────────────────┘
```

---

## 2. 源码运行

### 2.1 环境要求

- Windows 10/11、Python 3.10+、Microsoft Edge

### 2.2 安装依赖

```bash
pip install -r requirements.txt
python -m playwright install msedge   # 若用浏览器登录态
```

### 2.3 配置模型

```bash
cp config/llm_providers.example.json config/llm_providers.json
# 编辑 config/llm_providers.json 填入 API Key
# 在 config/__init__.py 修改 LLM_PROVIDER / LLM_MODEL_ID 切换模型
```

### 2.4 常用命令

```bash
python main.py --web                 # Web 控制台（127.0.0.1:8787，推荐）
python main.py                       # 批量模式：收集素材→生成→发布
python main.py --single              # 传统模式：逐轮生成即发布
python main.py --headless            # 浏览器无头运行（工作模式）
python main.py --test-api            # 测试 LLM API 连通性
python tools/launcher.py             # 一键启动器（源码态检查环境后拉起服务）
```

旧坐标/OCR 时代命令（`--calibrate` / `--test-ocr` / `--debug-ocr-region` / `--probe-a11y` / `--resume` / `--image-gen`）已随对应代码归档移除，见 `archive/`。

调试/探测脚本（tools/，需真实浏览器与登录态，选择器改版时先用它们实测 DOM）：

```bash
python -m web_drivers.deepseek --probe   # DeepSeek 关键 selector 探测
python tools/probe_stop_button.py        # 停止按钮 selector 探测
python tools/verify_dom_clicks.py        # DOM 点击链路验证
python tools/debug_ds_modes.py           # DeepSeek 模式切换调试
```

作者风格工具：

```bash
python tools/collect_author_pw.py [--count N]   # 批量采集作者回答
python -m applications.zhihu_story.author_profiler 作者名  # 提炼文风签名
python tools/generate_with_author.py --question "..." --author 作者名
```

知识库管理（`kb_manager.py`）：`--stats` / `--rebuild` / `--compress` / `--show [题材]` / `--ranking`

### 2.5 测试

```bash
python -m unittest discover -s tests    # 全量测试（430+ 项，含安全回归与浏览器池并发）
```

**提交门禁**：任何提交前必须 py_compile + 全量测试通过。

---

## 3. 目录解析（core/paths.py）

V4 起程序文件与用户数据分离：

- `PROGRAM_ROOT`：冻结态 = PyInstaller `_MEIPASS`（onedir 的 `_internal/`）；源码态 = 项目根
- `DATA_ROOT`：`AQ_DATA_DIR` 环境变量 > 冻结态 `%APPDATA%\AutoQuill` > 源码态项目根
- `program(*parts)` / `data(*parts)`：只读程序文件 / 可写用户数据
- `ensure_provider_file()`：安装态首启把 example 配置复制为 llm_providers.json（占位 key）
- `migrate_legacy_data()`：旧版（V3.x 解压目录）数据一次性迁移到 DATA_ROOT，失败不阻塞启动

原则：**只读文件走 PROGRAM_ROOT，可写文件走 DATA_ROOT**。

---

## 4. 首启引导（webui）

安装版首次打开控制台会弹出三步引导（Edge → API Key → 知乎登录）：

| 端点 | 说明 |
|---|---|
| `GET /api/setup/status` | 引导状态：`edge_ok` / `llm_configured` / `zhihu_logged_in` / `web_llm_logged_in` / `setup_needed` |
| `POST /api/setup/apikey` | 写入服务商 API Key（DATA_ROOT 的 llm_providers.json）并立即生效 |
| `POST /api/setup/test-api` | 实测当前配置的 API 连接（返回 ok + 服务端回复） |
| `POST /api/setup/zhihu-login` | 后台线程拉起可见 Edge 登录知乎（前端轮询 status 收尾） |
| `POST /api/setup/web-login` | 后台线程拉起可见 Edge 登录 DeepSeek 网页版（前端轮询 status 收尾） |

知乎登录逻辑抽为 `browser_adapter.login_zhihu_flow()`（CLI `--login` 与引导共用）；DeepSeek 网页版登录/判定在 `web_drivers/deepseek.web_llm_logged_in()` / `login_deepseek_web_flow()`（随驱动下沉 Layer 3，经 browser_pool 工厂创建独立实例）。

---

## 5. 打包与发布

### 5.1 PyInstaller 打包

```bash
python -m PyInstaller build/AutoQuill.spec --noconfirm
# 产物：dist/AutoQuill/（onedir：AutoQuill.exe + _internal/）
```

spec 要点：

- datas：`webui/static/`、`config/llm_providers.example.json`、`config/model_pricing.json`、`images/`
- **排除**：`llm_providers.json`（真实 key）、`browser_state.json`（登录态）、`webui_model.json`、`data/`
- hiddenimports：uvicorn 全部动态 import 子模块
- 入口 `tools/launcher.py`：冻结态 `sys.executable --service` 自拉起服务进程

冒烟：`AQ_DATA_DIR=$(mktemp -d)` 启动 dist 中的 exe，验证 /api/status 与 example 引导复制。

### 5.2 安装包

```bash
ISCC installer/AutoQuill.iss    # 需要 Inno Setup 6（winget install JRSoftware.InnoSetup）
# 产物：release/AutoQuill-Setup-<VERSION>.exe
```

设计约束：`PrivilegesRequired=lowest` 用户目录安装；卸载**不删** `%APPDATA%\AutoQuill`（用户数据）。

### 5.3 版本号

`core/version.py` 的 `VERSION` 是单一事实来源（banner / webui / README / iss / git tag 均引用）。

---

## 6. 核心概念

### 6.1 双脑模式

| 模式 | 说明 | 适用场景 |
|---|---|---|
| **API** (`LLM_MODE = "api"`) | 直接调用 LLM API，快速稳定 | 日常批量生成 |
| **Web** (`LLM_MODE = "web"`) | 通过浏览器操作网页版 LLM | 免费使用、API 不可用时 |

### 6.2 工作流生命周期

`WorkflowBase` 定义标准流水线：`选题 → 提取内容 → 提炼配方 → 生成故事 → 评分筛选 → 发布`。

- **选题**：创作中心推荐页（`/creator/featured-question/recommend`）解析候选，按互动评分自动排序，关键词白名单强制生效
- **提取**：DOM 读取首答全文与互动数据；「撤销删除」检测走 DOM
- **发布**：富文本粘贴（md→HTML + Ctrl+V，Draft.js 落盘格式）；成功判定轮询服务端草稿 API（可验证）

### 6.3 作者风格链路

```
采集（collector.py，断点续采）→ 提炼（author_profiler.py，统计+LLM 剖析）
  → 双层注入（llm_api.build_story_prompt：通用层 + 作者层）
```

样本权重 = log1p(点赞数) × 新鲜度衰减（90 天 1.0 → 730 天 0.3）。

### 6.4 Web 控制台结构

- `webui/server.py`：FastAPI 单进程（127.0.0.1:8787，Host/Origin 白名单防跨站盲打）、TaskRunner 单任务线程、watchdog 卡死判定（240s 无进展 / 900s 总时长）、取消钩子注入 web_drivers/browser_pool（浏览器工厂由 browser_adapter 注册）
- `webui/log_capture.py`：root logger 捕获 + 里程碑解析 → SSE 事件
- `webui/static/index.html`：单页控制台（运行控制 / 配置速览 / 实时日志 / 结果卡片 / 首启引导）

---

## 7. 配置参考

### 框架级配置（config/__init__.py）

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `LLM_MODE` | LLM 调用模式 | `"api"` |
| `LLM_PROVIDER` | 故事生成服务商 | `"DeepSeek"` |
| `LLM_MODEL_ID` | 故事生成模型 | `"deepseek-v4-pro"` |
| `KB_PROVIDER` / `KB_MODEL_ID` | 知识库任务服务商/模型 | `"DeepSeek"` / `"deepseek-v4-pro"` |
| `BROWSER_HEADLESS` | 浏览器无头模式 | `False` |
| `WEB_DRIVER_NAME` / `WEB_DRIVERS` | 网页版驱动注册表 | `"DeepSeek"` |

运行时切换（`config.set_runtime_*`）：模型 / 通道 / 浏览器模式 / 文风 / 网页预设，全部持久化到 `config/webui_model.json`（gitignored），启动自动恢复。

### 知乎专用配置（applications/zhihu_story/config.py）

`MIN_ANSWER_LENGTH`、`MAX_ANSWER_RETRIES`、`ZHIHU_RECOMMEND_URL`、`ZHIHU_INVITED_URL`、`QUESTION_SELECT_MODE`、`STORY_MATERIAL_MODE`、`MATERIAL_MIN_LIKES` 等（OCR_*/WAIT_* 常量已随 OCR 栈归档移除）。

---

## 8. 扩展指南

### 8.1 新增应用实例

```
applications/paper_review/
├── __init__.py
├── config.py        # 业务专用参数
├── prompts.py       # 专用 prompt
├── perception.py    # 页面感知
└── action.py        # 平台操作
```

### 8.2 新增工作流

```python
from workflows.base import WorkflowBase

class PaperReviewWorkflow(WorkflowBase):
    name = "paper_review"

    def select_topic(self):       ...  # 选题 → 返回 URL
    def extract_content(self):    ...  # 提取内容
    def publish(self, story, ...): ...  # 提交/发布
```

### 8.3 新增 Web LLM 驱动

```python
from web_drivers.base import WebLLMDriver

class NewSiteDriver(WebLLMDriver):
    name = "newsite"

    def setup(self):           ...  # 首次创建会话
    def wait_complete(self):   ...  # 等待生成完毕
```

浏览器基础设施一律走 `web_drivers/browser_pool`：共享单例 `get_browser()`（锁内懒启动）、`safe_evaluate()`（有界交互）、`set_cancel_hook()`（停止按钮）。浏览器本体（知乎域）由应用层注册工厂——`browser_pool` 自身禁止 import applications。

---

## 10. 设计债务（已记录，暂不修复）

- **prompts.py 反向引用**：`applications/zhihu_story/prompts.py` 被 llm_client / story_generation 等 Layer 3 模块引用，违反「上层不能反向依赖下层」约定。当前以「生成 prompt 是业务语义、先集中存放」为理由保留；若 prompts 持续增多，应下沉 core/ 或随应用实例移动。
- **workflows → applications 残留**：`workflows/zhihu.py` 的 `normalize_question_url` 从 browser_adapter 导入（纯 URL 函数，属 Layer 4 引用 Layer 5）。可后续下沉 web_drivers 或 core。
- **tools/ 探测脚本**：probe_*/debug_* 多为一次性诊断脚本，保留在仓库作为选择器改版时的回归参考。

---

## 9. 安全与注意事项

- **API Key 隔离**：真实 Key 只允许在 `config/llm_providers.json`（gitignored）；example 模板只允许占位符（`sk-your-*`）。pre-commit 钩子（`.githooks/pre-commit`）阻止含真实 Key 的提交，克隆后执行 `git config core.hooksPath .githooks` 启用
- **登录态等同账号凭证**：`config/browser_state.json`、`data/browser_profile/` 不入库、不打进安装包
- 修改 Web 代码前**先探测 DOM**（feedback_web_dom_first）：不臆想页面结构
- 运行中可将鼠标移到屏幕**左上角**触发 FailSafe 紧急停止（仅旧坐标通道）
- 日志在 `logs/` 按时间戳命名；Web 服务日志 `logs/webui.log`
