# ============================================================
# AutoQuill 配置包 v3.1（2026-08 由顶层 config.py 迁入）
#
# 框架级通用配置。业务参数分层存放：
#   config/story.py                 故事创作域共享参数（单一事实来源）
#   config/*.json                   运行时数据（服务商注册表、模型定价等）
#   applications/zhihu_story/config.py  知乎应用层参数（re-export 兼容层）
#   applications/zhihu_story/prompts.py 知乎故事提示词
#   applications/image_gen/config.py    图像生成参数
# ============================================================

import random
import time

# ============================================================
# LLM 调用模式（框架级——决定走 API 还是浏览器）
# ============================================================

# "api" = API 直接调用（付费，快）
# "web" = 浏览器操作网页版（免费但慢）——默认通道：新用户零成本起步，
# 首启引导让用户选择并配置对应信息；已配置 API 的老用户不受影响
# （运行时状态 webui_model.json 覆盖此默认值）
LLM_MODE = "web"

# 浏览器是否无头运行（调试/工作模式）
# False = 弹到前台（默认，调试可观察；同账号下可见生成过程）
# True  = 无头后台运行（工作模式，不打扰电脑其他工作）
# 运行时可用 set_runtime_browser_headless() 切换，下次任务启动生效
BROWSER_HEADLESS = False

# ============================================================
# LLM API 配置
# ============================================================

# 模型服务商注册表
# 所有 API Key、模型列表、地址等集中管理在 config/llm_providers.json 中
# 首次使用请复制 config/llm_providers.example.json → config/llm_providers.json 并填入你的 Key
#
# 切换模型只需修改下面两行，无需改动其他代码：

LLM_PROVIDER = "DeepSeek"          # 故事生成用的服务商名称（对应 JSON 中的 name）
LLM_MODEL_ID = "deepseek-v4-pro"    # 故事生成用的模型 ID
KB_PROVIDER  = "DeepSeek"          # 知识库任务用的服务商（配方提炼、题材分类、评分等）
# ★ 曾用 deepseek-v4-flash：该模型是推理模型，reasoning_content 会吃光
# 全部输出预算（max_tokens 2700 → reasoning 2700，content 恒为空），
# 配方提炼 0/1 双失败。thinking=false 等参数实测无效，改回 pro 才能
# 完整输出配方 JSON（长文本生成任务与 flash 的推理行为不兼容）
KB_MODEL_ID  = "deepseek-v4-pro"    # 知识库/评分模型，正文同用 pro

# --- 以下为自动加载逻辑，一般无需修改 ---
import json as _json, os as _os

from core.paths import data as _data_path

_PROVIDERS_FILE = _data_path("config", "llm_providers.json")

def _load_provider_config(provider_name, model_id):
    """从 config/llm_providers.json 中解析指定服务商和模型的完整配置"""
    if not _os.path.exists(_PROVIDERS_FILE):
        raise FileNotFoundError(
            f"未找到 {_PROVIDERS_FILE}！\n"
            f"请复制 config/llm_providers.example.json 为 config/llm_providers.json 并填入 API Key。"
        )
    with open(_PROVIDERS_FILE, 'r', encoding='utf-8') as f:
        providers = _json.load(f)

    for p in providers:
        if p["name"] == provider_name:
            api_key = p.get("apiKey", "")
            for m in p.get("models", []):
                if m["id"] == model_id:
                    cfg = dict(m)
                    cfg["apiKey"] = api_key
                    cfg["baseUrl"] = m.get("baseUrl", "")
                    cfg["model"] = model_id
                    cfg["provider"] = provider_name
                    cfg["extra_body"] = dict(m.get("extra_body") or {})
                    return cfg
            # 找到服务商但没匹配到模型 → 用服务商下第一个模型的 baseUrl 兜底
            first_url = p["models"][0]["baseUrl"] if p["models"] else ""
            return {
                "apiKey": api_key,
                "baseUrl": first_url,
                "model": model_id,
                "provider": provider_name,
                "extra_body": {},
            }

    raise ValueError(f"config/llm_providers.json 中未找到服务商「{provider_name}」")


# 解析故事生成模型
LLM_PROVIDER_CONFIG = _load_provider_config(LLM_PROVIDER, LLM_MODEL_ID)
LLM_API_KEY = LLM_PROVIDER_CONFIG.get("apiKey", "")
LLM_API_BASE_URL = LLM_PROVIDER_CONFIG.get("baseUrl", "")
LLM_API_MODEL = LLM_PROVIDER_CONFIG.get("model", LLM_MODEL_ID)
LLM_API_EXTRA_BODY = dict(LLM_PROVIDER_CONFIG.get("extra_body") or {})

# 解析知识库模型（可以和故事生成用不同的服务商/模型）
KB_PROVIDER_CONFIG = _load_provider_config(KB_PROVIDER, KB_MODEL_ID)
# 如果 KB 用了不同的服务商，其 key/url 通过 KB_LLM_API_KEY / KB_LLM_BASE_URL 暴露
KB_LLM_API_KEY = KB_PROVIDER_CONFIG.get("apiKey", "")
KB_LLM_BASE_URL = KB_PROVIDER_CONFIG.get("baseUrl", "")
KB_LLM_MODEL = KB_PROVIDER_CONFIG.get("model", KB_MODEL_ID)
KB_LLM_EXTRA_BODY = dict(KB_PROVIDER_CONFIG.get("extra_body") or {})

# API 请求参数
LLM_API_MAX_TOKENS = int(LLM_PROVIDER_CONFIG.get("maxOutputTokens") or 65536)
LLM_API_TEMPERATURE = 0.9      # 温度：越高越有创意（0.0-2.0）
LLM_API_TIMEOUT = 300          # 兼容旧配置；流式请求主要使用下面两个超时
LLM_API_CONNECT_TIMEOUT = 20   # API 建连超时
LLM_API_STREAM_READ_TIMEOUT = 60  # Socket 读超时；服务端心跳会重置该计时
LLM_API_STREAM_FIRST_TOKEN_TIMEOUT = 45  # 建立流式响应后，45 秒未收到正文 token 则失败
LLM_API_STREAM_IDLE_TIMEOUT = 60  # 已开始生成后，连续 60 秒无正文 token 则失败
LLM_API_FREQUENCY_PENALTY = 0  # 频率惩罚：同一篇内已出现多次的词,再出现的概率降低(减少重复句式)
LLM_API_PRESENCE_PENALTY = 0   # 存在惩罚:已出现过的词,后续一律降低概率(鼓励用新词新表达)

# ============================================================
# Web LLM 驱动配置
# ============================================================
# 切换网站只需改 WEB_DRIVER_NAME，新增网站在 WEB_DRIVERS 中添加条目

WEB_DRIVER_NAME = "DeepSeek"       # 当前使用的 Web 驱动："DeepSeek"

WEB_DRIVERS = {
    # DeepSeek 条目为 DOM 驱动（web_drivers/deepseek.py）使用；
    # 旧 OCR 参数（copy_icon 等）已随重写移除，并行参数为 DOM 版
    "DeepSeek": {
        "url": "https://chat.deepseek.com/",
        # 模式预设（set_web_mode_preset 运行时改写；setup() 按目标先读后点）
        #   fast   → mode=fast  深度思考开 智能搜索开（默认，用户习惯）
        #   expert → mode=expert 深度思考开 智能搜索关
        "preset": "fast",
        "mode": "fast",            # "fast" = 快速模式 / "expert" = 专家模式
        "deep_think": True,        # 深度思考（R1）
        "smart_search": True,      # 智能搜索（仅快速模式存在）
        # 生成完成检测
        "poll_interval": 4,        # 轮询间隔（秒）
        "stable_count": 2,         # 文本长度连续 N 轮不变 → 完成
        "max_wait": 600,           # 单次生成最长等待（秒）
        # 并行模式参数（parallel_tabs > 1 且任务数 > 1 → 走 DOM 并行调度器）
        "parallel_tabs": 2,                  # 并行页面数；DeepSeek 网页版同账号并发上限实测为 2
        "consecutive_fail_threshold": 2,     # 连续失败 N 次后重置该 slot 的会话
        "scan_interval": 2,                  # 主循环每轮扫描间隔（秒）
    },
    "Aizex": {
        "url": "https://leopard-x.memofun.net/",
        "chat_placeholder": "有问题，尽管问",
        "copy_icon": "images/aizex_copy_icon.png",
        "completion_icon": "images/aizex_completion_icon.png",
        # 模型选择（通过校准坐标打开菜单，OCR 定位模型名称）
        "model": "GPT-5.5 Thinking Extended",
        "model_menu": {
            "_top_level": [
                "Auto", "GPT-5.5 Thinking", "GPT-5.5 Thinking Extended",
            ],
            "Grok 系列": ["Grok 4.2 Expert", "Grok 4.2 Auto", "Grok 4.2 Fast"],
            "Claude 系列": ["Claude Sonnet 4.6 Thinking", "Claude Opus 4.7 Thinking", "Claude Opus 4.6 Thinking"],
            "Gemini 系列": [
                "Gemini 3 Flash Thinking", "Gemini 3 Flash",
                "Gemini 3.1 Pro", "Gemini 3.1 Pro [API]",
            ],
            "香蕉模型 [Nano Banana]": ["Nano Banana Pro", "Nano Banana 2"],
            "DeepSeek 系列": [],
        },
        # 等待时间
        "wait_load": 4.0,
        "wait_after_paste": 0.5,
        "wait_after_send": 1.5,
        "wait_before_url_cache": 8,   # Aizex 响应慢
        "wait_copy_click": 0.6,
        "wait_scroll_end": 0.8,
        # 生成完成检测（页面不自动滚动，需主动 PageDown）
        "wait_first_reply": 6,        # 模型初始思考静默期
        "poll_interval": 5,
        "pagedown_per_cycle": 5,      # 每次OCR前按几次PageDown
        "stable_count": 3,            # 连续3次PageDown后不变→完成
        "max_wait": 360,
        # 并行模式参数（1 = 走旧的串行逻辑；>1 启用并行）
        # Aizex 没有已知并发限制，可按网络/机器性能调整
        "parallel_tabs": 3,                   # 并行 tab 数（1-8）
        "consecutive_fail_threshold": 2,      # 连续失败 N 次后重置该 slot 的会话
        "scan_interval": 2,                   # 主循环每轮扫描间隔（秒）
    },
}

# ============================================================
# 全局键鼠参数
# ============================================================

PYAUTOGUI_PAUSE = 0.1
MOUSE_MOVE_DURATION = (0.1, 0.25)

# ============================================================
# 各环节等待时间（秒）—— 通用操作
# ============================================================

# --- 通用操作 ---
WAIT_HOTKEY = (0.05, 0.15)
WAIT_PASTE = (0.1, 0.2)
WAIT_PAGE_LOAD = (1.5, 2.2)
WAIT_TAB_OPEN = (1.0, 1.5)

# --- 步骤 1：选题 ---
WAIT_RECOMMEND_PAGE = 2.0
WAIT_QUESTION_ENTER = 0.7

# --- 步骤 2：OCR 提取 ---
WAIT_BEFORE_OCR = 0.3
WAIT_EXPAND_CLICK = 0.5
WAIT_PAGE_DOWN = 0.18
WAIT_SCROLL_NEXT_ANSWER = 0.2

# --- 轮次间 ---
WAIT_BETWEEN_CYCLES = (1.5, 3)

# ============================================================
# OCR 参数
# ============================================================

OCR_MAX_SCROLLS = 10

# ============================================================
# 辅助函数
# ============================================================

def random_delay(delay_range):
    if isinstance(delay_range, (int, float)):
        time.sleep(delay_range)
        return delay_range
    delay = random.uniform(delay_range[0], delay_range[1])
    time.sleep(delay)
    return delay

def random_mouse_duration():
    return random.uniform(MOUSE_MOVE_DURATION[0], MOUSE_MOVE_DURATION[1])

# ============================================================
# 运行时模型切换（Web 控制台用）
#
# 原理：llm_api 等模块在函数内 `from config import ...`，每次
# 调用都读取当前模块属性——直接重赋值 LLM_PROVIDER / LLM_MODEL_ID
# 及其派生常量即可让切换立即生效，无需重启服务。
# 选择持久化到 config/webui_model.json（已 gitignore，不入库），
# 下次启动自动恢复。
# ============================================================

_WEBUI_MODEL_FILE = _data_path("config", "webui_model.json")


def _save_webui_state(**extra):
    """持久化 Web 控制台运行时选择到 webui_model.json。

    先读旧文件再合并：模型/通道/浏览器/文风 四组字段共存，
    任何一次切换都不能覆盖掉其他字段（否则重启后丢失）。
    仅当当前 provider 真实存在于注册表时落盘——测试里用假服务商
    切换时 persist=True 会污染真实配置文件，启动恢复即炸。"""
    try:
        _load_provider_config(LLM_PROVIDER, LLM_MODEL_ID)
    except ValueError:
        return
    data = {}
    try:
        with open(_WEBUI_MODEL_FILE, encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, _json.JSONDecodeError):
        pass
    if not isinstance(data, dict):
        data = {}
    data["provider"] = LLM_PROVIDER
    data["model_id"] = LLM_MODEL_ID
    data.update(extra)
    try:
        with open(_WEBUI_MODEL_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def set_runtime_model(provider=None, model_id=None, persist=True):
    """运行时切换故事生成模型（服务商 + 模型 ID），返回生效配置。

    provider / model_id 缺省则保持当前值；persist=False 不落盘。
    仅影响故事生成（LLM_*）；知识库任务（KB_*）保持独立配置。
    """
    global LLM_PROVIDER, LLM_MODEL_ID
    global LLM_PROVIDER_CONFIG, LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL
    global LLM_API_EXTRA_BODY, LLM_API_MAX_TOKENS

    # 先 resolve 目标再验证：无效 provider 抛 ValueError 时全局不被污染，
    # 否则测试里假服务商切换会污染真实配置、后续恢复即炸
    target_provider = provider or LLM_PROVIDER
    target_model = model_id or LLM_MODEL_ID
    cfg = _load_provider_config(target_provider, target_model)

    LLM_PROVIDER = target_provider
    LLM_MODEL_ID = target_model
    LLM_PROVIDER_CONFIG = cfg
    LLM_API_KEY = cfg.get("apiKey", "")
    LLM_API_BASE_URL = cfg.get("baseUrl", "")
    LLM_API_MODEL = cfg.get("model", LLM_MODEL_ID)
    LLM_API_EXTRA_BODY = dict(cfg.get("extra_body") or {})
    LLM_API_MAX_TOKENS = int(cfg.get("maxOutputTokens") or 65536)

    if persist:
        _save_webui_state(mode=LLM_MODE)

    return {"provider": LLM_PROVIDER, "model_id": LLM_MODEL_ID,
            "api_model": LLM_API_MODEL}


def set_runtime_mode(mode, persist=True):
    """运行时切换生成通道：api（API 调用）/ web（网页版浏览器操作）。

    LLM_MODE 在 workflows 里都是函数内动态读取，重赋值后下次生成
    立即生效；持久化到 webui_model.json（已 gitignore，不入库），
    下次启动自动恢复。
    """
    global LLM_MODE
    if mode not in ("api", "web"):
        raise ValueError(f"未知生成通道：{mode}，可选：api / web")
    LLM_MODE = mode
    if persist:
        _save_webui_state(mode=mode)
    return {"mode": LLM_MODE}


def set_web_mode_preset(preset, persist=True):
    """切换 DeepSeek 网页版模式预设，把预设翻译成 WEB_DRIVERS 目标字段。

    预设 → 目标字段（setup() 按目标先读后点，不破坏页面手动状态）：
      "fast"   → mode="fast", deep_think=True, smart_search=True
      "expert" → mode="expert", deep_think=True, smart_search=False
    smart_search 只在快速模式存在，专家模式下自动忽略。
    """
    global WEB_DRIVERS
    presets = {
        "fast": {"mode": "fast", "deep_think": True, "smart_search": True},
        "expert": {"mode": "expert", "deep_think": True,
                   "smart_search": False},
    }
    if preset not in presets:
        raise ValueError(f"未知网页模式预设：{preset}，可选：fast / expert")
    cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
    cfg.update(presets[preset])
    cfg["preset"] = preset
    if persist:
        _save_webui_state(web_preset=preset)
    return {"preset": preset, "config": {k: cfg[k] for k in
            ("mode", "deep_think", "smart_search")}}


def set_runtime_browser_headless(headless, persist=True):
    """运行时切换浏览器无头模式（调试 False=弹前台 / 工作 True=后台）。

    get_browser() 每次任务启动时动态读取，切换后下一次任务生效
    （当前运行中的任务不受影响）；持久化到 webui_model.json。
    """
    global BROWSER_HEADLESS
    BROWSER_HEADLESS = bool(headless)
    if persist:
        _save_webui_state(headless=BROWSER_HEADLESS)
    return {"headless": BROWSER_HEADLESS}


def set_runtime_author_profile(name, persist=True):
    """运行时切换故事生成注入的作者文风（空串/None = 不注入）。

    直接重赋值 config.story.AUTHOR_PROFILE（单一事实来源）——workflow
    每次任务新建实例时函数内重新 import 读取，切换后下一任务立即生效；
    持久化到 webui_model.json（author_profile 字段），启动自动恢复。
    """
    from config import story
    story.AUTHOR_PROFILE = name or ""
    if persist:
        _save_webui_state(author_profile=story.AUTHOR_PROFILE)
    return {"author_profile": story.AUTHOR_PROFILE}


def _apply_webui_model_override():
    """启动时恢复 Web 控制台上次选择的模型与生成通道（若存在且仍有效）。"""
    try:
        if not _os.path.exists(_WEBUI_MODEL_FILE):
            return
        with open(_WEBUI_MODEL_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        provider, model_id = data.get("provider"), data.get("model_id")
        if provider and model_id:
            set_runtime_model(provider, model_id, persist=False)
        mode = data.get("mode")
        if mode in ("api", "web"):
            set_runtime_mode(mode, persist=False)
        headless = data.get("headless")
        if isinstance(headless, bool):
            set_runtime_browser_headless(headless, persist=False)
        if "author_profile" in data:
            name = data["author_profile"]
            # 具体作者签名文件已不存在（如换了电脑/清了数据）→ 回退内置通用文风
            if name and name != "通用":
                safe = "".join(
                    "_" if c in '\\/:*?"<>|' else c for c in name)
                if not _os.path.exists(
                        _data_path("data", "authors", f"{safe}.json")):
                    import logging as _logging
                    _logging.getLogger("config").warning(
                        "文风「%s」签名不存在，回退为「通用」", name)
                    name = "通用"
            set_runtime_author_profile(name, persist=False)
        web_preset = data.get("web_preset")
        if web_preset in ("fast", "expert"):
            set_web_mode_preset(web_preset, persist=False)
        tunables = data.get("story_tunables")
        if isinstance(tunables, dict):
            from config import story as _story
            for k, v in tunables.items():
                if hasattr(_story, k) and isinstance(v, int):
                    setattr(_story, k, v)
    except Exception:
        pass  # 配置损坏/服务商被移除 → 保持默认


_apply_webui_model_override()
