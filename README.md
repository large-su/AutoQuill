<div align="center">

# ✒️ AutoQuill

**Desktop AI Writing Automation via OCR & Human-like Interaction**

**基于 OCR 与类人操作的桌面 AI 写作自动化工具**

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](#english) · [中文](#中文)

</div>

---

<a name="english"></a>

## 🌍 English

### What is AutoQuill?

AutoQuill is a desktop automation tool that orchestrates AI-powered content creation by interacting with web-based LLMs and content platforms through **screen OCR recognition and human-like mouse/keyboard operations** — no APIs, no browser extensions, no injection scripts.

It reads the screen like a human, clicks like a human, and types like a human.

### How it Works

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│  1. Discover │────▶│  2. Extract  │────▶│  3. Generate │────▶│  4. Publish  │
│   (Select    │     │   (OCR Read  │     │   (Send to   │     │   (Paste &   │
│    Topic)    │     │    Content)  │     │    Web LLM)  │     │    Submit)   │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
   Manual or           Screenshot +         Keyboard input       OCR-locate
   Auto-scored         Page Down +          to DeepSeek +        buttons &
   selection           OCR parsing          poll completion      auto-fill
```

**Core Tech Stack:**

- **Screen OCR** (RapidOCR) — reads text from screenshots, locates buttons by recognizing on-screen text, completely bypasses copy-protection and anti-scraping mechanisms
- **Human-like I/O** (PyAutoGUI) — mouse movements with randomized duration, keyboard shortcuts, Page Down scrolling — indistinguishable from a real user
- **Window Management** (PowerShell + Win32 API) — automatic Edge browser focus switching between terminal and browser
- **Smart Detection** — polls screen content to detect page load completion, LLM generation completion, and content boundaries

### Current Demo: Zhihu Story Creation

The included demo automates a creative writing workflow on [Zhihu](https://zhihu.com) (China's Quora-like platform):

1. **Topic Discovery** — navigates to Zhihu's recommended questions, OCR-parses each question card (views, answers, followers), scores them, prioritizes trending ("飙升") topics
2. **Content Extraction** — enters a question page, OCR-reads the top-voted answer by scrolling page-by-page, auto-detects answer boundaries ("编辑于 + date"), skips short answers
3. **AI Generation** — opens DeepSeek (web chat), sends a structured prompt + reference material, polls screen every 5s to detect generation completion, auto-clicks the copy button
4. **Publishing** — returns to the question page, OCR-locates the "Write Answer" button, pastes content, OCR-locates the "Confirm & Parse Markdown" dialog — all in one fluid sequence

### Installation

```bash
git clone https://github.com/YOUR_USERNAME/AutoQuill.git
cd AutoQuill

pip install pyautogui pyperclip pillow rapidocr-onnxruntime
```

**Requirements:**
- Windows 10/11
- Python 3.8+
- Microsoft Edge browser
- Screen resolution: any (calibrated via setup)

### Quick Start

```bash
# Step 1: Calibrate screen boundaries (5 points, one-time setup)
python zhihu_auto.py --calibrate

# Step 2: Test OCR recognition
python zhihu_auto.py --test-ocr

# Step 3: Run
python zhihu_auto.py
```

### Calibration

AutoQuill needs 5 reference points on your screen (only once, saved to `coordinates.json`):

| # | Point | Where to Calibrate |
|---|-------|--------------------|
| 1 | Content area left edge | Zhihu answer page |
| 2 | Content area right edge | Zhihu answer page |
| 3 | Content area top edge | Zhihu answer page |
| 4 | Content area bottom edge | Zhihu answer page |
| 5 | DeepSeek copy icon button | DeepSeek chat page |

All other buttons ("Write Answer", "Confirm & Parse", DeepSeek input box) are located automatically via OCR — no manual calibration needed.

### Configuration

All timing parameters are centralized in `config.py` with Chinese comments:

```python
# --- Topic Selection Mode ---
QUESTION_SELECT_MODE = "manual"    # "manual" or "auto"

# --- Wait Times (seconds) — tune these for speed ---
WAIT_PAGE_LOAD = (2.5, 4.0)       # After navigating to a URL
WAIT_PAGE_DOWN = 0.6              # After each Page Down press
WAIT_DS_AFTER_SEND = 1.5          # After sending message to DeepSeek
WAIT_DRAFT_SAVE = 8               # After pasting to Zhihu editor
# ... 20+ tunable parameters
```

### Project Structure

```
AutoQuill/
├── zhihu_auto.py       # Main script — workflow orchestration
├── config.py           # All config, timing, and prompts
├── ocr_utils.py        # OCR engine, text detection, smart scrolling
├── coordinates.json    # Calibrated screen positions (auto-generated)
├── screenshots/        # Debug screenshots (auto-generated)
└── README.md
```

### Extending to Other Platforms

AutoQuill's architecture is platform-agnostic. The core components — `ocr_region()`, `click_by_text()`, `wait_for_answer_load()`, `wait_for_deepseek_complete()` — work with any website. To adapt for a different platform:

1. Write new `parse_xxx()` functions in `ocr_utils.py` for the target site's layout
2. Update the step functions in `zhihu_auto.py`
3. Adjust prompts in `config.py`

### Safety Features

- **Emergency stop** — move mouse to top-left corner of screen (PyAutoGUI failsafe)
- **Keyboard interrupt** — Ctrl+C in terminal
- **Graceful degradation** — if OCR fails at any step, falls back to manual mode
- **Debug screenshots** — auto-saved on errors for troubleshooting

---

<a name="中文"></a>

## 🇨🇳 中文

### AutoQuill 是什么？

AutoQuill（自动羽毛笔）是一个桌面自动化工具，通过**屏幕 OCR 识别 + 类人键鼠操作**来协调 AI 内容创作流程——不调用 API，不装浏览器插件，不注入任何脚本。

它像人一样读屏幕，像人一样点鼠标，像人一样敲键盘。

### 工作原理

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ 1. 发现选题│────▶│ 2. 提取内容│────▶│ 3. AI 生成│────▶│ 4. 发布内容│
│ 手动/自动  │     │ 截图+OCR  │     │ 发送到网页 │     │ 粘贴+提交 │
│ 评分选择   │     │ 翻页识别  │     │ 大模型    │     │ OCR定位   │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
```

**核心技术栈：**

- **屏幕 OCR**（RapidOCR）— 截图识别文字、定位按钮，完全绕过复制保护和反爬机制
- **类人操作**（PyAutoGUI）— 随机时长的鼠标移动、键盘快捷键、Page Down 翻页，和真人操作无异
- **窗口管理**（PowerShell + Win32 API）— 终端和浏览器之间自动切换焦点
- **智能检测** — 轮询屏幕内容，检测页面加载完成、大模型生成完成、内容边界

### 当前示例：知乎故事创作

项目自带的示例实现了知乎故事类回答的自动化创作流程：

1. **自动选题** — 打开知乎推荐问题页，OCR 解析每个问题的浏览量/回答量/关注量，评分排序，飙升标签优先
2. **自动提取** — 进入问题页，逐页截图 OCR 提取高赞回答，自动检测「编辑于 + 日期」结束标志，短回答自动跳过
3. **AI 生成** — 打开 DeepSeek 网页版，发送结构化提示词 + 参考材料，每 5 秒轮询检测生成完成，自动点击复制
4. **自动发布** — 返回问题页，OCR 定位「写回答」按钮，粘贴内容，OCR 定位「确认并解析」弹窗——一气呵成

### 安装

```bash
git clone https://github.com/YOUR_USERNAME/AutoQuill.git
cd AutoQuill

pip install pyautogui pyperclip pillow rapidocr-onnxruntime
```

**环境要求：**
- Windows 10/11
- Python 3.8+
- Microsoft Edge 浏览器
- 屏幕分辨率：任意（通过校准适配）

### 快速开始

```bash
# 第一步：校准屏幕边界（5 个点，只需做一次）
python zhihu_auto.py --calibrate

# 第二步：测试 OCR 识别效果
python zhihu_auto.py --test-ocr

# 第三步：运行
python zhihu_auto.py
```

### 校准说明

AutoQuill 需要 5 个屏幕参考点（一次性，保存到 `coordinates.json`）：

| # | 点位 | 在哪个页面校准 |
|---|------|--------------|
| 1 | 知乎正文区域左边界 | 知乎问题页 |
| 2 | 知乎正文区域右边界 | 知乎问题页 |
| 3 | 知乎内容上边界 | 知乎问题页 |
| 4 | 知乎内容下边界 | 知乎问题页 |
| 5 | DeepSeek 复制图标按钮 | DeepSeek 对话页 |

其他按钮（写回答、确认并解析、DeepSeek 输入框等）全部通过 OCR 自动定位，无需手动校准。

### 配置参数

所有等待时间集中在 `config.py`，按步骤分组并附中文注释：

```python
# --- 选题模式 ---
QUESTION_SELECT_MODE = "manual"    # "manual" = 手动选题 / "auto" = 全自动

# --- 等待时间（秒）— 调这里控制速度 ---
WAIT_PAGE_LOAD = (2.5, 4.0)       # 打开新 URL 后等待加载
WAIT_PAGE_DOWN = 0.6              # 每次 Page Down 后等待渲染
WAIT_DS_AFTER_SEND = 1.5          # 发送消息到 DeepSeek 后等待
WAIT_DRAFT_SAVE = 8               # 粘贴到知乎编辑器后等待草稿保存
# ... 20+ 个可调参数
```

### 项目结构

```
AutoQuill/
├── zhihu_auto.py       # 主脚本 — 流程编排
├── config.py           # 所有配置、等待时间、提示词模板
├── ocr_utils.py        # OCR 引擎、文字定位、智能滚动
├── coordinates.json    # 校准坐标（自动生成）
├── screenshots/        # 调试截图（自动生成）
└── README.md
```

### 扩展到其他平台

AutoQuill 的架构与平台无关。核心组件——`ocr_region()`、`click_by_text()`、`wait_for_answer_load()`、`wait_for_deepseek_complete()`——适用于任何网站。适配新平台只需：

1. 在 `ocr_utils.py` 中为目标网站编写新的 `parse_xxx()` 函数
2. 更新 `zhihu_auto.py` 中的步骤函数
3. 调整 `config.py` 中的提示词

### 安全机制

- **紧急停止** — 鼠标移到屏幕左上角（PyAutoGUI 安全机制）
- **键盘中断** — 终端按 Ctrl+C
- **优雅降级** — OCR 失败时自动切换到手动模式
- **调试截图** — 出错时自动保存截图便于排查

---

## ⚠️ Disclaimer / 免责声明

**English:**
AutoQuill is an open-source research project for studying desktop automation and OCR technology. Users are solely responsible for ensuring their use complies with all applicable laws, regulations, and platform terms of service. The authors do not endorse or encourage any use that violates third-party platform policies. Use at your own risk.

**中文：**
AutoQuill 是一个用于研究桌面自动化和 OCR 技术的开源项目。用户须自行确保其使用方式符合所有适用的法律法规及平台服务条款。作者不鼓励任何违反第三方平台政策的使用行为。使用风险由用户自行承担。

---

## 🤝 Contributing / 参与贡献

Issues and PRs are welcome. If you adapt AutoQuill to a new platform, consider contributing it back as a new demo module.

欢迎提 Issue 和 PR。如果你把 AutoQuill 适配到了新平台，欢迎贡献为新的示例模块。

---

## 📄 License

[MIT](LICENSE)
