# ============================================================
# webui/api_settings.py — 设置域：参数调优/模型/模式/浏览器/问题源/作者档案
# P0 拆分自 server.py；处理函数逐字搬运，仅装饰器前缀 app->router。
# 行为守护：tests/test_webui_server 全量端点断言。
# ============================================================

import json
import logging
import os
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse
from pathlib import Path
from pydantic import BaseModel

from .api_setup import _web_llm_logged_in_cached
from .common import (_llm_configured, _require_llm_ready,
                     _require_zhihu_url, _current_log_file)
from .run_manager import runner
from core import paths

log = logging.getLogger(__name__)

router = APIRouter()

@router.get("/api/config")
def api_config():
    """配置速览（只读）。模型字段取根 config 的实际生效值（运行时可切换）。"""
    cfg = {}
    try:
        from config import story as sconfig
        for k in ("QUESTION_SELECT_MODE", "QUESTION_SOURCE",
                  "ENABLE_STORY_FILTER", "STORY_MATERIAL_MODE",
                  "AUTHOR_PROFILE",
                  "ENABLE_FORMAT_RETRY", "MIN_ANSWER_LENGTH",
                  "ENABLE_MATERIAL_LIKES_GATE", "MATERIAL_MIN_LIKES",
                  "MAX_TOPIC_RETRY", "STORY_GENERATE_MAX_ATTEMPTS",
                  "KB_ENABLE"):
            cfg[k] = getattr(sconfig, k, None)
    except Exception as exc:
        cfg["_app_error"] = str(exc)
    try:
        # WEB_DRIVER_NAME 是早期网页驱动时代的残留（知乎 workflow
        # 固定走 browser_adapter 直连 Edge），不再对外展示
        from config import LLM_MODE, LLM_PROVIDER, LLM_MODEL_ID
        cfg["LLM_MODE"] = LLM_MODE
        cfg["LLM_PROVIDER"] = LLM_PROVIDER
        cfg["LLM_MODEL"] = LLM_MODEL_ID
    except Exception as exc:
        cfg["_root_error"] = str(exc)
    try:
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        dcfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        cfg["WEB_PRESET"] = {
            "preset": dcfg.get("preset", "fast"),
            "mode": dcfg.get("mode"),
            "deep_think": bool(dcfg.get("deep_think")),
            "smart_search": bool(dcfg.get("smart_search")),
            "allowed": ["fast", "expert"],
        }
    except Exception as exc:
        cfg["_web_error"] = str(exc)
    return cfg


# 允许前端通过「设置」面板修改的选题参数（其余 key 一律拒绝）
_TUNABLE_KEYS = {
    # key: (类型, 最小值, 最大值)
    "MAX_TOPIC_RETRY": (int, 0, 10),
    "MIN_ANSWER_LENGTH": (int, 100, 5000),
    "MATERIAL_MIN_LIKES": (int, 0, 100000),
    "STORY_GENERATE_MAX_ATTEMPTS": (int, 1, 10),
}


class _TunableSpec(BaseModel):
    key: str
    value: int


@router.post("/api/config")
def api_set_tunable(spec: _TunableSpec):
    """运行时修改选题参数（MAX_TOPIC_RETRY / MIN_ANSWER_LENGTH /
    MATERIAL_MIN_LIKES，整型并限幅），持久化到 webui_model.json
    （story_tunables 字段），下次启动自动恢复。"""
    from config import story
    if spec.key not in _TUNABLE_KEYS:
        raise HTTPException(400, f"不可修改的参数：{spec.key}")
    typ, lo, hi = _TUNABLE_KEYS[spec.key]
    val = typ(spec.value)
    val = max(lo, min(hi, val))
    setattr(story, spec.key, val)
    from config import _save_webui_state
    _save_webui_state(story_tunables={
        k: getattr(story, k) for k in _TUNABLE_KEYS})
    log.info("Web 控制台修改选题参数 %s → %s", spec.key, val)
    return {"ok": True, "key": spec.key, "value": val}


class _WebPresetSpec(BaseModel):
    preset: str  # fast（快速+深思+搜索）/ expert（专家+深思）


@router.post("/api/web-preset")
def api_set_web_preset(spec: _WebPresetSpec):
    """切换 DeepSeek 网页版模式预设（立即生效，持久化到 webui_model.json）。"""
    from config import set_web_mode_preset
    try:
        eff = set_web_mode_preset(spec.preset)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("Web 控制台切换网页模式预设 → %s", eff["preset"])
    return {"ok": True, "effective": eff}


class _QuestionSourceSpec(BaseModel):
    source: str   # recommend（推荐话题）/ invited（邀请回答）/ custom（自选问题）
    custom_url: str = ""   # source=custom 时的具体问题链接


@router.get("/api/question-source")
def api_question_source():
    """读取当前选题来源（推荐话题 / 邀请回答 / 自选问题）与自选链接。"""
    from config import story
    return {"source": story.QUESTION_SOURCE,
            "custom_url": story.CUSTOM_QUESTION_URL}


@router.post("/api/question-source")
def api_set_question_source(spec: _QuestionSourceSpec):
    """切换选题来源（立即生效，持久化到 webui_model.json）。

    custom 模式需要同时带上具体问题链接（运行时还会再做一次校验）。"""
    from config import set_runtime_question_source, \
        set_runtime_custom_question_url
    try:
        eff = set_runtime_question_source(spec.source)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if spec.source == "custom":
        url_eff = set_runtime_custom_question_url(spec.custom_url)
        eff.update(url_eff)
    log.info("Web 控制台切换选题来源 → %s", eff)
    return {"ok": True, "effective": eff}


@router.get("/api/models")
def api_models():
    """列出可切换的服务商与模型（不含任何密钥）。"""
    from config import _PROVIDERS_FILE, LLM_PROVIDER, LLM_MODEL_ID
    try:
        with open(_PROVIDERS_FILE, "r", encoding="utf-8") as f:
            providers = json.load(f)
    except OSError:
        raise HTTPException(500, "llm_providers.json 读取失败")
    return {
        "current": {"provider": LLM_PROVIDER, "model_id": LLM_MODEL_ID},
        "providers": [
            {"name": p["name"],
             "models": [{"id": m["id"]} for m in p.get("models", [])]}
            for p in providers
        ],
    }


class _ModelSpec(BaseModel):
    provider: str
    model_id: str


class _ModeSpec(BaseModel):
    mode: str


class _BrowserSpec(BaseModel):
    headless: bool  # False=前台调试 / True=无头工作


@router.post("/api/model")
def api_set_model(spec: _ModelSpec):
    """运行时切换故事生成模型（立即生效，持久化到 webui_model.json）。"""
    from config import set_runtime_model
    try:
        eff = set_runtime_model(spec.provider, spec.model_id)
    except Exception as exc:
        raise HTTPException(400, f"模型切换失败：{exc}")
    log.info("Web 控制台切换模型 → %s / %s", eff["provider"], eff["model_id"])
    return {"ok": True, "effective": eff}


@router.get("/api/mode")
def api_mode():
    """生成通道：api（API 调用）/ web（网页版浏览器操作）。"""
    from config import LLM_MODE
    return {"mode": LLM_MODE, "allowed": ["api", "web"]}


@router.post("/api/mode")
def api_set_mode(spec: _ModeSpec):
    """运行时切换生成通道（立即生效，持久化到 webui_model.json）。

    闭环预检：切 web 要求 DeepSeek 网页版已登录、切 api 要求已配置
    API Key——未满足则拒绝切换，返回 needs 标记让前端弹对应引导
    （登录/填 key 完成后可再次切换）。
    """
    from config import set_runtime_mode
    try:
        if spec.mode not in ("api", "web"):
            raise HTTPException(400, f"未知生成通道：{spec.mode}")
        from config import LLM_MODE
        if spec.mode == LLM_MODE:
            return {"ok": True, "effective": {"mode": spec.mode}}
        if spec.mode == "web":
            # 走 15s 缓存：登录成功回调会立即清缓存（ts=0），登录刚完成
            # 时此处必定真实检测，不会误拦；重复切换/轮询则命中缓存秒回
            if not _web_llm_logged_in_cached():
                raise HTTPException(400, {
                    "detail": "DeepSeek 网页版尚未登录，无法切换。"
                              "请在引导窗口中打开 Edge 完成登录后重试。",
                    "needs": "deepseek_login",
                })
        else:
            _require_llm_ready({
                "detail": "尚未配置 API Key，无法切换。"
                          "请在设置中填写有效的 API Key 后重试。",
                "needs": "api_key",
            })
        eff = set_runtime_mode(spec.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("Web 控制台切换生成通道 → %s", eff["mode"])
    return {"ok": True, "effective": eff}


@router.get("/api/browser")
def api_browser():
    """浏览器模式：headless=False 前台调试 / True 无头工作。"""
    from config import BROWSER_HEADLESS
    return {"headless": BROWSER_HEADLESS}


@router.post("/api/browser")
def api_set_browser(spec: _BrowserSpec):
    """运行时切换浏览器模式（下次任务启动生效，持久化）。"""
    from config import set_runtime_browser_headless
    eff = set_runtime_browser_headless(spec.headless)
    log.info("Web 控制台切换浏览器模式 → %s",
             "无头（工作）" if eff["headless"] else "前台（调试）")
    return {"ok": True, "effective": eff}


# ============================================================
# 首启引导（安装版：Edge 检测 → API Key → 知乎登录 三步）
# ============================================================


class _AuthorSpec(BaseModel):
    name: str  # 作者名；空串 = 不注入文风


@router.get("/api/authors")
def api_authors():
    """已提炼的文风签名列表（data/authors/*.json）+ 当前注入选择。"""
    from applications.zhihu_story.author_profiler import (
        AUTHORS_DIR, GENERAL_PROFILE_FILE, load_author_profile,
        load_general_profile)
    from config.story import AUTHOR_PROFILE
    authors = []
    has_general = False
    if os.path.isdir(AUTHORS_DIR):
        for f in sorted(os.listdir(AUTHORS_DIR)):
            if not f.endswith(".json"):
                continue
            is_general = (f == GENERAL_PROFILE_FILE)
            if is_general:
                has_general = True
            name = "通用" if is_general else f[:-5]
            profile = load_author_profile(
                name, filename=f) if is_general else load_author_profile(name)
            if not profile:
                continue
            sig = profile.get("signature") or {}
            authors.append({
                "name": name,
                "general": is_general,
                "profiled_at": profile.get("profiled_at", ""),
                "stories_count": len(profile.get("source_stories") or []),
                "style": (sig.get("style") or "")[:40],
            })
    if not has_general:
        # 未提炼通用风格时也列出内置通用规则，保证下拉可选
        builtin = load_general_profile()
        if builtin:
            sig = builtin.get("signature") or {}
            authors.insert(0, {
                "name": "通用",
                "general": True,
                "builtin": True,
                "profiled_at": "",
                "stories_count": 0,
                "style": (sig.get("style") or "")[:40],
            })
    return {"authors": authors, "current": AUTHOR_PROFILE}


@router.post("/api/author")
def api_set_author(spec: _AuthorSpec):
    """运行时切换故事生成注入的作者文风（空串=不注入，持久化）。"""
    from config import set_runtime_author_profile
    try:
        eff = set_runtime_author_profile(spec.name)
    except Exception as exc:
        raise HTTPException(400, f"文风切换失败：{exc}")
    log.info("Web 控制台切换文风 → %r", eff["author_profile"])
    return {"ok": True, "effective": eff}


def _storylib_authors(records):
    """按作者聚合 + 是否已有文风签名文件（删除时提示存在）。"""
    from applications.zhihu_story.author_profiler import AUTHORS_DIR
    seen = {}
    for rec in records:
        a = (rec.get("author") or "").strip()
        if a:
            seen[a] = seen.get(a, 0) + 1
    authors = []
    for name, count in sorted(seen.items(), key=lambda kv: -kv[1]):
        safe = paths.sanitize_filename(name)
        authors.append({
            "name": name,
            "records": count,
            "has_profile": os.path.exists(
                os.path.join(AUTHORS_DIR, f"{safe}.json")),
        })
    return authors


@router.get("/api/storylib")
def api_storylib(author: str = ""):
    """采集库管理数据：不带 author 按作者聚合（含签名存在标记）；
    带 author 返回该作者的记录详情（单条删除用）。"""
    from applications.zhihu_story.collector import iter_collected_stories
    records = list(iter_collected_stories())
    if author:
        details = []
        for rec in records:
            if (rec.get("author") or "").strip() != author:
                continue
            footer = rec.get("footer") or {}
            details.append({
                "title": (rec.get("title") or "（无标题）")[:60],
                "collected_at": rec.get("collected_at", ""),
                "answer_url": footer.get("answer_url", ""),
                "chars": len(rec.get("answer") or ""),
            })
        return {"author": author, "records": details}
    return {"authors": _storylib_authors(records)}


class _StoryLibDelSpec(BaseModel):
    author: str = ""   # 按作者整删
    url: str = ""      # 按 answer_url 单条删


@router.delete("/api/storylib")
def api_storylib_delete(spec: _StoryLibDelSpec):
    """删除采集记录：author 整删该作者 / url 单条删（规范化匹配）。

    任务运行中拒绝（采集可能正在写库，重写会冲突）。"""
    if runner.state in ("running", "stopping"):
        raise HTTPException(409, "任务运行中，请先停止任务再清理采集库")
    if not spec.author and not spec.url:
        raise HTTPException(400, "须指定 author（整删）或 url（单条删）")
    from applications.zhihu_story.collector import (
        _norm_url, iter_collected_stories, STORY_LIB)
    if not os.path.exists(STORY_LIB):
        return {"ok": True, "removed": 0, "authors": []}

    target = _norm_url(spec.url)
    kept, removed = [], 0
    for rec in iter_collected_stories():
        if spec.author:
            if (rec.get("author") or "").strip() == spec.author:
                removed += 1
                continue
        elif target:
            if _norm_url(
                    (rec.get("footer") or {}).get("answer_url") or "") == target:
                removed += 1
                continue
        kept.append(rec)
    if not removed:
        raise HTTPException(404, "未找到要删除的记录")

    tmp = STORY_LIB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in kept:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, STORY_LIB)
    log.info("采集库清理：删除 %d 条记录（author=%r url=%r），"
             "剩余 %d 条", removed, spec.author, spec.url, len(kept))
    return {"ok": True, "removed": removed,
            "authors": _storylib_authors(kept)}


@router.get("/api/profile-sources")
def api_profile_sources():
    """采集库（collected_stories.jsonl）中可提炼的作者与篇数。"""
    from applications.zhihu_story.collector import STORY_LIB, load_author_counts
    counts = load_author_counts(STORY_LIB)
    return {"authors": [
        {"name": k, "records": v} for k, v in
        sorted(counts.items(), key=lambda kv: -kv[1])]}
