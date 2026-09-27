# ============================================================
# webui/api_setup.py — 安装引导域：状态/APIKey/连通测试/知乎与网页版登录
# P0 拆分自 server.py；处理函数逐字搬运，仅装饰器前缀 app->router。
# 行为守护：tests/test_webui_server 全量端点断言。
# ============================================================

import json
import logging
import os
import re
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
# 反复拉起 Edge（_WEB_LLM_CACHE_TTL 秒内复用结果）。
# ★ 按驱动名分别缓存（2026-09 修复）：DeepSeek 已登录 ≠ Doubao 已登录，
# 切换网页版大模型后若沿用旧缓存会误放行（用户实测：切豆包不弹登录）。
_WEB_LLM_CACHE_TTL = 15.0
_web_llm_cache = {}           # {驱动名: {"ts": float, "ok": bool}}
_web_llm_cache_lock = threading.Lock()


def _web_llm_cache_key():
    from config import WEB_DRIVER_NAME
    return WEB_DRIVER_NAME


def _setup_version():
    from core.version import VERSION
    return VERSION


def _browser_lock_free():
    """独占 profile 的浏览器锁当前是否空闲（非阻塞探测）。

    为什么必须探这一下（2026-09-23 修）：登录引导会把 _browser_lock 持满整个
    等待窗口（最长 5 分钟）。setup/status 若照旧去发起真实检测，就会一直阻塞
    在锁上——前端每 2.5~3 秒轮询一次，很快把 FastAPI 线程池占满，整个控制台
    （包括「登录完成没有」的那个轮询）一起卡死。锁被占时不做真实检测：沿用
    上次结果（没有就按未登录，此时界面本来就在引导登录），并且**不写缓存**，
    等对方放手后下一轮再实测。
    """
    try:
        from web_drivers.browser_pool import (
            _browser_lock, live_browsers, profile_in_use,
        )
    except Exception:             # noqa: BLE001 理论上不会发生
        return True
    # ★ 2026-09-26：有浏览器活着（共享任务浏览器 / 登录引导 / 抓取）也算忙——
    # 这时再起一个实例必然 exitCode=21 失败，旧代码还会触发「清理残留进程」
    # 把正在干活的浏览器杀掉。忙就只回报缓存值、不发起检测。
    if profile_in_use() or live_browsers() > 0:
        return False
    if not _browser_lock.acquire(blocking=False):
        return False
    _browser_lock.release()
    return True


def _web_llm_logged_in_cached(driver=None):
    """带缓存的登录态检测（按 driver 区分缓存；默认当前驱动）。

    driver: 指定目标驱动名（切换网页版大模型时预检用，避免用旧驱动的
    缓存结果）。加锁去重：真实检测（独立浏览器，约 5s）进行中时，
    前端 setup/status 每 2.5s 的并发轮询不再各自排队启动浏览器。
    浏览器被登录引导独占时不做检测、也不写缓存（见 _browser_lock_free）。
    """
    from config import WEB_DRIVER_NAME
    key = driver or WEB_DRIVER_NAME
    with _web_llm_cache_lock:
        entry = _web_llm_cache.get(key)
        now = time.time()
        if entry and now - entry["ts"] < _WEB_LLM_CACHE_TTL:
            return entry["ok"]
        if not _browser_lock_free():
            # 登录引导/任务正独占 profile：直接返回（绝不排队等锁）
            return entry["ok"] if entry else False
        ok = _web_llm_logged_in_for(key)
        _web_llm_cache[key] = {"ts": now, "ok": ok}
        return ok


def _web_llm_logged_in_for(driver):
    """对指定驱动做真实登录态检测（无论当前 WEB_DRIVER_NAME 是谁）。"""
    try:
        import importlib
        from web_drivers import _DRIVER_REGISTRY
        module_path, _cls = _DRIVER_REGISTRY[driver]
        mod = importlib.import_module(module_path)
        return bool(mod.web_llm_logged_in())
    except Exception:
        return False


@router.get("/api/setup/status")
def api_setup_status():
    """引导状态：Edge / API Key / 知乎登录 / Web 登录 就绪检查。

    setup_needed 语义（Web 为默认通道，但已配置任一通道即放行）：
    Edge 可用 且 知乎已登录 且（API Key 已配置 或 DeepSeek 网页版已登录）。
    """
    from applications.zhihu_story.browser_adapter import (
        EDGE_PATH, STORAGE_STATE_PATH)
    from config import WEB_DRIVER_NAME
    llm_configured = _llm_configured()
    edge_ok = bool(EDGE_PATH)
    zhihu_logged_in = os.path.exists(STORAGE_STATE_PATH)
    web_ok = _web_llm_logged_in_cached() if edge_ok else False
    login_running = (_login_thread is not None and _login_thread.is_alive())
    # 登录态失效标记（2026-09-19）：cookie 还在但服务端已不认时，抓取/删除链路
    # 会把页面跳登录页的事实记下来，这里带给前端提示「重新登录知乎」。
    from webui.browser_tasks import zhihu_login_stale
    stale = zhihu_login_stale()
    return {
        "version": _setup_version(),
        "edge_ok": edge_ok,
        "llm_configured": llm_configured,
        "web_llm_logged_in": web_ok,
        "web_driver": WEB_DRIVER_NAME,
        "zhihu_logged_in": zhihu_logged_in,
        "zhihu_login_stale": bool(stale.get("stale")),
        "zhihu_login_stale_reason": stale.get("reason", ""),
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
                    _web_llm_cache.clear()
                runner.guide_needed = None  # 登录完成：引导标记解除
            if ok and kind == "zhihu":
                # 登录成功即清「登录态已失效」标记：不清的话设置页/首启引导
                # 仍挂着红色警示，用户会以为白登了（这一步只去掉陈旧提示，
                # 前端随后照旧用 zhihu-check 实测确认）
                from webui.browser_tasks import clear_zhihu_login_stale
                clear_zhihu_login_stale()
            if not ok:
                _login_error = msg
        except Exception as exc:
            # profile 被占（有任务在跑/另一个实例在开）时说人话，别把
            # Playwright 的英文栈丢给用户（2026-09-26：这是「重新登录打不开
            # 窗口」最常见的原因）
            from web_drivers.browser_pool import ProfileBusy
            if isinstance(exc, ProfileBusy):
                _login_error = str(exc)
                log.warning("首启引导：%s未开始（%s）", log_name, exc)
            else:
                _login_error = str(exc)
                log.error("首启引导：%s异常：%s", log_name, exc, exc_info=True)

    _login_thread = threading.Thread(target=_run, daemon=True)
    _login_thread.start()


@router.post("/api/setup/zhihu-check")
def api_setup_zhihu_check():
    """真实检查知乎登录态（打开知乎首页看是否跳登录页），并同步失效标记。

    「有登录态文件」不等于「服务端还认」——cookie 还在但会话被登出时，必须靠
    这一步才能发现（2026-09-19 看板/草稿箱刷新失败的真因）。前端「设置 →
    知乎账号 → 检查登录状态」调用。"""
    from webui.browser_tasks import (
        browser_busy, clear_zhihu_login_stale, mark_zhihu_login_stale,
    )
    busy = browser_busy()
    if busy:
        return {"ok": False, "status": "busy",
                "message": "「" + busy[0] + "」任务进行中，请完成后再检查登录状态"}
    from applications.zhihu_story.browser_adapter import verify_zhihu_login
    logged_in, detail = verify_zhihu_login(headless=True)
    if logged_in is None:
        # 有任务在跑 → 这次没判定（浏览器被独占，硬查会起第二个实例互相残杀）。
        # 只回报「暂缓」，**绝不**据此把登录态标成失效（2026-09-26 修）。
        log.info("知乎登录态检查：暂缓（%s）", detail)
        return {"ok": False, "status": "busy", "message": detail}
    if logged_in:
        clear_zhihu_login_stale()
    else:
        mark_zhihu_login_stale(detail)
    log.info("知乎登录态检查：%s（%s）", "有效" if logged_in else "失效", detail)
    return {"ok": True, "logged_in": logged_in, "detail": detail}


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
    """后台线程拉起可见 Edge 引导登录当前网页版大模型；轮询 status 收尾。"""
    from config import WEB_DRIVER_NAME
    from web_drivers import login_web_flow
    _start_login_thread("deepseek", login_web_flow,
                        f"{WEB_DRIVER_NAME} 网页版登录")
    return {"ok": True,
            "message": f"请在弹出的 Edge 窗口中登录 {WEB_DRIVER_NAME} 网页版，"
                       "检测到登录后自动保存并关闭"}


# ============================================================
# 检查更新（查询 GitHub Releases）
#
# 2026-09-27 用户反馈「有时能检查到、有时弹无法连接」——原因是原来的实现
# 只有一条通道：匿名打 api.github.com（每小时 60 次，共用出口 IP / 代理时很
# 容易被 403 限流），且拿不到就立刻放弃、把异常类名（HTTPError）直接当消息。
# 现在改成：
#   1) 三条通道依次兜底：API（信息最全）→ releases/latest 网页跳转 → releases.atom
#      订阅源（后两条是普通网页请求，不吃 API 限流）；
#   2) 瞬时错误（429/5xx）自动重试一次；
#   3) 报错翻译成人话，并给出「直接打开发布页」的出路；
#   4) 成功结果缓存 60 秒、失败只缓存 10 秒（用户再点一次能真的重试）。
# ============================================================

_UPDATE_REPO = "large-su/AutoQuill"
_UPDATE_TTL = 60.0            # 成功结果缓存（避免连点把限流额度烧掉）
_UPDATE_ERR_TTL = 10.0        # 失败结果缓存（短：再点一次要能真重试）
_UPDATE_TIMEOUT = 6
_update_cache = {"ts": 0.0, "data": None, "ok": False, "last_ok": None}
_UA = {"User-Agent": "AutoQuill", "Accept": "application/vnd.github+json"}
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _version_tuple(v):
    """'4.9.6' → (4, 9, 6)；解析不了返回 None（宁可说『没更新』也不误报）。"""
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("."))
    except (AttributeError, ValueError):
        return None


def _clean_tag(tag):
    """'v4.9.6' → '4.9.6'；空/异常返回 None。"""
    text = str(tag or "").strip().lstrip("vV").strip()
    return text or None


def _latest_via_api(timeout):
    """通道 1：GitHub API（带 html_url，信息最全）——会被匿名限流。"""
    r = requests.get(
        "https://api.github.com/repos/%s/releases/latest" % _UPDATE_REPO,
        timeout=timeout, headers=_UA)
    r.raise_for_status()
    info = r.json() or {}
    return _clean_tag(info.get("tag_name")), (info.get("html_url") or "")


def _latest_via_redirect(timeout):
    """通道 2：releases/latest 的跳转地址（普通网页请求，不吃 API 限流）。"""
    url = "https://github.com/%s/releases/latest" % _UPDATE_REPO
    r = requests.get(url, timeout=timeout, allow_redirects=True, stream=True,
                     headers={"User-Agent": "AutoQuill"})
    try:
        r.raise_for_status()
        final = r.url or ""
    finally:
        try:
            r.close()
        except Exception:              # noqa: BLE001
            pass
    m = re.search(r"/releases/tag/([^/?#]+)", final)
    if not m:
        raise RuntimeError("跳转地址里没有版本号（%s）" % (final or "?"))
    return _clean_tag(m.group(1)), final


def _latest_via_atom(timeout):
    """通道 3：releases.atom 订阅源（最省流量，几乎不会被限流）。"""
    r = requests.get("https://github.com/%s/releases.atom" % _UPDATE_REPO,
                     timeout=timeout, headers={"User-Agent": "AutoQuill"})
    r.raise_for_status()
    m = re.search(r"/releases/tag/([^\"'<>\s]+)", r.text or "")
    if not m:
        raise RuntimeError("订阅源里没有版本条目")
    tag = m.group(1)
    return _clean_tag(tag), "https://github.com/%s/releases/tag/%s" % (_UPDATE_REPO, tag)


def _fetch_latest(timeout=_UPDATE_TIMEOUT):
    """按 API → 跳转 → 订阅源 依次尝试，返回 (版本号, 链接, 通道名)。"""
    errors = []
    for name, fetch in (("api", _latest_via_api),
                        ("redirect", _latest_via_redirect),
                        ("atom", _latest_via_atom)):
        for attempt in (1, 2):          # 瞬时错误重试一次
            try:
                version, url = fetch(timeout)
                if version:
                    return version, url, name
                errors.append("%s：没取到版本号" % name)
                break
            except requests.exceptions.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                errors.append("%s：HTTP %s" % (name, status))
                if status in _TRANSIENT_STATUS and attempt == 1:
                    time.sleep(0.8)
                    continue
                break
            except Exception as exc:    # noqa: BLE001
                errors.append("%s：%s" % (name, exc.__class__.__name__))
                break
    raise RuntimeError("；".join(errors) or "未知错误")


def _friendly_update_error(raw):
    """底层报错 → 用户看得懂、且能据此行动的一句话。"""
    text = str(raw or "")
    if "HTTP 403" in text or "HTTP 429" in text:
        return ("更新服务器限流（GitHub 匿名接口每小时 60 次，共用出口 IP 或走代理时"
                "更容易触发）。已自动试过备用通道，请稍后再试，或直接打开下载页手动查看")
    if "HTTP 404" in text:
        return "发布仓库不存在或已改名；请直接打开下载页手动查看"
    if any(("HTTP %d" % s) in text for s in _TRANSIENT_STATUS[1:]):
        return "GitHub 服务器暂时不可用（已自动重试）；请稍后再试"
    if "Timeout" in text or "ConnectionError" in text or "SSLError" in text:
        return "连接 GitHub 超时或失败（可能是网络/代理问题）；请检查网络后重试"
    return "连接更新服务器失败（%s）；请稍后重试" % (text[:120] or "未知原因")


@router.get("/api/update/check")
def api_update_check():
    """检查 GitHub Releases 是否有新版本。

    永远返回 200：失败时给 error（人话）+ 下载页链接，绝不把异常抛给前端。
    成功缓存 60s、失败缓存 10s；失败但此前成功过时，附带上次结果供界面显示。
    """
    from core.version import VERSION
    now = time.time()
    cached = _update_cache.get("data")
    if cached is not None:
        ttl = _UPDATE_TTL if _update_cache.get("ok") else _UPDATE_ERR_TTL
        if now - float(_update_cache.get("ts") or 0) < ttl:
            return cached
    data = {
        "current": VERSION,
        "latest": None,
        "has_update": False,
        "url": f"https://github.com/{_UPDATE_REPO}/releases",
        "error": None,
        "channel": "",
    }
    try:
        latest, url, channel = _fetch_latest()
        data["latest"] = latest
        data["channel"] = channel
        cur, new = _version_tuple(VERSION), _version_tuple(latest)
        data["has_update"] = bool(cur and new and new > cur)
        if url:
            data["url"] = url
        _update_cache.update(ts=now, data=data, ok=True, last_ok=dict(data))
    except Exception as exc:            # noqa: BLE001
        data["error"] = _friendly_update_error(exc)
        last = _update_cache.get("last_ok") or {}
        if last.get("latest"):
            # 别让用户两眼一抹黑：上次成功检查到的结果还是有价值的
            data["last_latest"] = last.get("latest")
            data["last_has_update"] = bool(last.get("has_update"))
        _update_cache.update(ts=now, data=data, ok=False)
    return data

