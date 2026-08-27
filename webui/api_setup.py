# ============================================================
# webui/api_setup.py — 安装引导域：状态/APIKey/连通测试/知乎与网页版登录
# P0 拆分自 server.py；处理函数逐字搬运，仅装饰器前缀 app->router。
# 行为守护：tests/test_webui_server 全量端点断言。
# ============================================================

import json
import logging
import os
import threading

import requests
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse
from pathlib import Path
from pydantic import BaseModel

from .common import (_llm_configured, _require_llm_ready)
from .run_manager import runner

log = logging.getLogger(__name__)

router = APIRouter()

_login_thread = None
_login_error = ""
_login_kind = ""  # 当前登录引导的站点："zhihu" / "deepseek" / ""

# web_llm_logged_in 检查要启动独立浏览器，约数秒；缓存避免首启轮询
# 反复拉起 Edge（_WEB_LLM_CACHE_TTL 秒内复用结果）
_WEB_LLM_CACHE_TTL = 15.0
_web_llm_cache = {"ts": 0.0, "ok": False}
_web_llm_cache_lock = threading.Lock()



def _setup_version():
    from core.version import VERSION
    return VERSION


def _web_llm_logged_in_cached():
    """带缓存的登录态检测。

    加锁去重：真实检测（独立浏览器，约 5s）进行中时，前端 setup/status
    每 2.5s 的并发轮询不再各自排队启动浏览器，而是等待同一份结果。
    """
    with _web_llm_cache_lock:
        ts, ok = _web_llm_cache["ts"], _web_llm_cache["ok"]
        if time.time() - ts < _WEB_LLM_CACHE_TTL:
            return ok
        try:
            from web_drivers.deepseek import web_llm_logged_in
            ok = web_llm_logged_in()
        except Exception:
            ok = False
        _web_llm_cache.update(ts=time.time(), ok=ok)
        return ok


@router.get("/api/setup/status")
def api_setup_status():
    """引导状态：Edge / API Key / 知乎登录 / Web 登录 就绪检查。

    setup_needed 语义（Web 为默认通道，但已配置任一通道即放行）：
    Edge 可用 且 知乎已登录 且（API Key 已配置 或 DeepSeek 网页版已登录）。
    """
    from applications.zhihu_story.browser_adapter import (
        EDGE_PATH, STORAGE_STATE_PATH)
    llm_configured = _llm_configured()
    edge_ok = bool(EDGE_PATH)
    zhihu_logged_in = os.path.exists(STORAGE_STATE_PATH)
    web_ok = _web_llm_logged_in_cached() if edge_ok else False
    login_running = (_login_thread is not None and _login_thread.is_alive())
    return {
        "version": _setup_version(),
        "edge_ok": edge_ok,
        "llm_configured": llm_configured,
        "web_llm_logged_in": web_ok,
        "zhihu_logged_in": zhihu_logged_in,
        "login_running": login_running,
        "login_kind": _login_kind if login_running else "",
        "login_error": _login_error,
        "setup_needed": not (edge_ok and zhihu_logged_in
                             and (llm_configured or web_ok)),
    }


class _ApiKeySpec(BaseModel):
    provider: str = "DeepSeek"
    api_key: str = ""


@router.post("/api/setup/apikey")
def api_setup_apikey(spec: _ApiKeySpec):
    """写入服务商 API Key（llm_providers.json，DATA_ROOT）并立即生效。

    首启引导专用；写入后按该服务商切换故事生成模型（持久化）。
    """
    key = (spec.api_key or "").strip()
    if not key:
        raise HTTPException(400, "API Key 不能为空")
    from config import _PROVIDERS_FILE, set_runtime_model
    try:
        with open(_PROVIDERS_FILE, "r", encoding="utf-8") as f:
            providers = json.load(f)
    except OSError:
        raise HTTPException(500, "llm_providers.json 读取失败")
    p = next((p for p in providers if p["name"] == spec.provider), None)
    if p is None:
        raise HTTPException(
            400, f"llm_providers.json 中未找到服务商「{spec.provider}」")
    p["apiKey"] = key
    with open(_PROVIDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(providers, f, ensure_ascii=False, indent=2)
    eff = set_runtime_model(spec.provider, None, persist=True)
    runner.guide_needed = None  # Key 已配置：引导标记解除
    log.info("首启引导：已写入服务商「%s」的 API Key", spec.provider)
    return {"ok": True, "effective": eff}


@router.post("/api/setup/test-api")
def api_setup_test_api():
    """实测当前配置的 API 连接（首启引导「测试连接」按钮）。"""
    _require_llm_ready("API Key 未配置或仍是占位符")
    from config import LLM_API_BASE_URL
    if not LLM_API_BASE_URL:
        raise HTTPException(400, "缺少 baseUrl（服务商配置不完整）")
    from llm_client import call_llm_non_streaming
    content, _elapsed, error = call_llm_non_streaming(
        "请回复：连接成功", max_tokens=100, timeout=30,
        report_usage=False)
    if error:
        return {"ok": False, "detail": error}
    return {"ok": True, "detail": f"连接成功：{content[:60]}"}


def _start_login_thread(kind, flow_call, log_name):
    """通用登录引导：后台线程拉起可见 Edge；前端轮询 setup/status 收尾。"""
    global _login_error, _login_thread, _login_kind
    if _login_thread is not None and _login_thread.is_alive():
        raise HTTPException(409, "登录引导已在运行，请在弹出的 Edge 窗口完成登录")
    from applications.zhihu_story.browser_adapter import EDGE_PATH
    if not EDGE_PATH:
        raise HTTPException(400, "未找到 Microsoft Edge，请先安装 Edge 后重试")

    _login_error = ""
    _login_kind = kind

    def _run():
        global _login_error
        try:
            ok, msg = flow_call()
            log.info("首启引导：%s%s", log_name, "成功" if ok else f"失败：{msg}")
            if ok and kind == "deepseek":
                # 清缓存：登录刚完成时 setup/status 的 15s 缓存可能仍为
                # False，不立即反映会让切换/引导误判未登录
                with _web_llm_cache_lock:
                    _web_llm_cache.update(ts=0.0, ok=False)
                runner.guide_needed = None  # 登录完成：引导标记解除
            if not ok:
                _login_error = msg
        except Exception as exc:
            _login_error = str(exc)
            log.error("首启引导：%s异常：%s", log_name, exc, exc_info=True)

    _login_thread = threading.Thread(target=_run, daemon=True)
    _login_thread.start()


@router.post("/api/setup/zhihu-login")
def api_setup_zhihu_login():
    """后台线程拉起可见 Edge 引导登录知乎；前端轮询 setup/status 收尾。"""
    from applications.zhihu_story.browser_adapter import login_zhihu_flow
    _start_login_thread("zhihu", login_zhihu_flow, "知乎登录")
    return {"ok": True,
            "message": "请在弹出的 Edge 窗口中完成登录（扫码/短信），"
                       "检测到登录后自动保存并关闭"}


@router.post("/api/setup/web-login")
def api_setup_web_login():
    """后台线程拉起可见 Edge 引导登录 DeepSeek 网页版；轮询 status 收尾。"""
    from web_drivers.deepseek import login_deepseek_web_flow
    _start_login_thread("deepseek", login_deepseek_web_flow, "DeepSeek 网页版登录")
    return {"ok": True,
            "message": "请在弹出的 Edge 窗口中登录 DeepSeek 网页版，"
                       "检测到登录后自动保存并关闭"}


# ============================================================
# 检查更新（查询 GitHub Releases，60s 缓存）
# ============================================================

_UPDATE_REPO = "large-su/AutoQuill"
_UPDATE_TTL = 60.0
_update_cache = {"ts": 0.0, "data": None}


def _version_tuple(v):
    try:
        return tuple(int(x) for x in v.lstrip("vV").split("."))
    except (AttributeError, ValueError):
        return None


@router.get("/api/update/check")
def api_update_check():
    """检查 GitHub Releases 是否有新版本（60s 缓存；网络失败只报 error 不抛错）。"""
    from core.version import VERSION
    now = time.time()
    if _update_cache["data"] is not None and now - _update_cache["ts"] < _UPDATE_TTL:
        return _update_cache["data"]
    data = {
        "current": VERSION,
        "latest": None,
        "has_update": False,
        "url": f"https://github.com/{_UPDATE_REPO}/releases",
        "error": None,
    }
    try:
        r = requests.get(
            f"https://api.github.com/repos/{_UPDATE_REPO}/releases/latest",
            timeout=5,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "AutoQuill"},
        )
        r.raise_for_status()
        info = r.json()
        latest = info.get("tag_name", "")
        data["latest"] = latest.lstrip("vV")
        cur, new = _version_tuple(VERSION), _version_tuple(latest)
        data["has_update"] = bool(cur and new and new > cur)
        data["url"] = info.get("html_url") or data["url"]
    except Exception as exc:
        data["error"] = f"无法连接更新服务器：{exc.__class__.__name__}"
    _update_cache.update(ts=now, data=data)
    return data
