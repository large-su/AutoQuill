# ============================================================
# AutoQuill 配置文件 v2.3
#
# 框架级通用配置 + 知乎故事应用的向后兼容 re-export
# 知乎专用 prompt 已迁移至 applications/zhihu_story/prompts.py
# 知乎专用参数已迁移至 applications/zhihu_story/config.py
# ============================================================

import random
import time

# ============================================================
# 向后兼容 re-exports（知乎故事专用配置，v2.3 重构中迁移）
# 下游代码无需修改：from config import FILTER_PROMPT 等仍然有效
# ============================================================
from applications.zhihu_story.config import *
from applications.zhihu_story.prompts import *
from applications.image_gen.config import *

# ============================================================
# LLM 调用模式（框架级——决定走 API 还是浏览器）
# ============================================================

# "api" = API 直接调用（推荐，快速稳定）
# "web" = 浏览器操作网页版（免费但慢）
LLM_MODE = "api"

# ============================================================
# LLM API 配置
# ============================================================

# 模型服务商注册表
# 所有 API Key、模型列表、地址等集中管理在 config/llm_providers.json 中
# 首次使用请复制 config/llm_providers.example.json → config/llm_providers.json 并填入你的 Key
#
# 切换模型只需修改下面两行，无需改动其他代码：

# LLM_PROVIDER = "DeepSeek"          # 故事生成用的服务商名称（对应 JSON 中的 name）
# LLM_MODEL_ID = "deepseek-v4-flash"     # 故事生成用的模型 ID（对应 JSON 中 models[].id）
# KB_PROVIDER  = "DeepSeek"          # 知识库任务用的服务商（配方提炼、题材分类、评分等）
# KB_MODEL_ID  = "deepseek-v4-flash"     # 知识库任务用的模型 ID
LLM_PROVIDER = "XiaomiMimo"          # 故事生成用的服务商名称（对应 JSON 中的 name）
LLM_MODEL_ID = "mimo-v2.5-pro"    # 故事生成用的模型 ID（mimo-v2.5-pro 或 mimo-v2.5）
KB_PROVIDER  = "XiaomiMimo"          # 知识库任务用的服务商（配方提炼、题材分类、评分等）
KB_MODEL_ID  = "mimo-v2.5"    # 知识库/评分用更快模型，正文仍用 pro

# --- 以下为自动加载逻辑，一般无需修改 ---
import json as _json, os as _os

_PROVIDERS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "config", "llm_providers.json")

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

def _load_provider(provider_name, model_id):
    """兼容旧调用：返回 api_key, base_url, model。"""
    cfg = _load_provider_config(provider_name, model_id)
    return cfg.get("apiKey", ""), cfg.get("baseUrl", ""), cfg.get("model", model_id)

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

WEB_DRIVER_NAME = "DeepSeek"       # 当前使用的 Web 驱动："DeepSeek" / "Aizex"

WEB_DRIVERS = {
    "DeepSeek": {
        "url": "https://chat.deepseek.com/",
        "chat_placeholder": "给 DeepSeek 发送消息",
        "copy_icon": "images/deepseek_copy_icon.png",
        # 模式切换
        "mode": "expert",          # "fast" = 快速模式 / "expert" = 专家模式
        "deep_think": False,       # 深度思考（R1）
        "smart_search": False,     # 智能搜索
        # 等待时间
        "wait_load": 4.0,
        "wait_after_paste": 0.5,
        "wait_after_send": 1.5,
        "wait_before_url_cache": 3,
        "wait_copy_click": 0.6,
        "wait_scroll_end": 0.8,
        # 生成完成检测
        "wait_first_reply": 0,     # DeepSeek 响应快，不需要初始等待
        "poll_interval": 5,
        "stable_count": 2,
        "max_wait": 360,
        "pagedown_per_cycle": 1,   # 每次 OCR 前按一次 PageDown（防止鼠标误触中断自动滚动）
        # 并行模式参数（1 = 走旧的串行逻辑；>1 启用并行）
        # ⚠️ DeepSeek 网页端限制同一账号最多 2 个并发生成，超过会排队/失败
        # 所以这里实际有意义的值只能是 1 或 2
        "parallel_tabs": 2,
        "consecutive_fail_threshold": 2,      # 连续失败 N 次后重置该 slot 的会话
        "scan_interval": 2,                   # 主循环每轮扫描间隔（秒）
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
