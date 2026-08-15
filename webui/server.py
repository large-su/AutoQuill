# ============================================================
# webui/server.py — AutoQuill Web 控制台后端
#
# 本地单机控制台：启动/停止 workflow、实时日志（SSE）、
# 环节测试（选题/提取/生成）、历史故事查看、配置速览。
# 只监听 127.0.0.1（workflow 依赖本机桌面浏览器与登录态）。
#
# 入口：python main.py --web（main.py CLI 分发调用 run()）
#
# 结构：
#   TaskRunner 单例 —— 后台线程跑 workflow，stop/watchdog 中断
#   log_capture   —— root logger 捕获 + 里程碑解析
#   SSE /api/events —— 日志行 + 任务状态推给浏览器
# ============================================================

import builtins
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from webui import log_capture

log = logging.getLogger(__name__)

from core import paths

OUTPUT_DIR = Path(paths.data("output"))
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

HOST = "127.0.0.1"
PORT = 8787

# watchdog：日志超过 STALL 秒无进展或总时长超过 OVERALL 秒 → 判定卡死
# 240s 而非 180s：慢速页面/模型首 token 前等待窗口可能较长，留足余量
STALL_LIMIT = 240.0
OVERALL_LIMIT = 900.0

# run 线程最后一条日志的时间戳（CaptureHandler.emit 更新）
_last_log_ts = [0.0]


class _TimedHandler(logging.Handler):
    """记录日志时间戳的 capture handler（watchdog 卡死判定用）。"""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self._inner = log_capture.CaptureHandler()
        self._inner.setFormatter(logging.Formatter(log_capture._FORMAT))

    def emit(self, record):
        _last_log_ts[0] = time.time()
        self._inner.emit(record)


class _RunSpec(BaseModel):
    mode: str  # select | extract | generate | single | batch | profile | general_profile | collect
    gen_count: int = 5
    publish_count: int = 3
    author: str = ""   # profile 模式：要提炼文风的作者名（collect 模式作者名自动识别）
    url: str = ""      # collect 模式：作者回答列表页 URL
    count: int = 5     # collect 模式：本次最多新增篇数


class TaskRunner:
    """单任务后台运行器（同一时刻只允许一个任务）。

    state: idle → running → done/error/stopped/timeout
    """

    def __init__(self):
        # RLock：start() 持锁时内部 _set_state() 还会再拿同一把锁
        self._lock = threading.RLock()
        self._thread = None
        self._stop_flag = threading.Event()
        self.state = "idle"
        self.message = ""
        self.last_context = {}   # {title, answer, footer, url}
        self.last_story = {}     # {text, md_path, chars}
        self.progress = None     # {text, pct}：阶段进度（SSE 断连时前端轮询）
        self._handler = None

    # ---------------- 状态 ----------------

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "context": self.last_context,
                "story": self.last_story,
                "progress": self.progress,
            }

    def _set_state(self, state, message=""):
        with self._lock:
            self.state = state
            self.message = message

    # ---------------- 启动 ----------------

    def start(self, spec: _RunSpec):
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise HTTPException(409, "已有任务在运行")
            self._thread = threading.Thread(
                target=self._run_task, args=(spec,), daemon=True)
            self._stop_flag.clear()
            self._set_state("running", f"任务启动：{spec.mode}")
            self._thread.start()

    # ---------------- 中断 ----------------

    def stop(self):
        """请求中断：置标志，browser_adapter 的取消钩子在检查点抛异常。

        不直接关浏览器（Playwright sync API 线程亲和，跨线程 close
        会挂起）；浏览器操作自带超时（goto 20s / evaluate 哨兵），
        超时后到检查点即抛 WorkflowCancelled，任务线程干净退出，
        finally 里同线程 close 浏览器。
        """
        self._stop_flag.set()
        self._set_state("stopping", "正在停止…")
        return {"ok": True, "message": "停止请求已发出（浏览器操作完成后中断）"}

    # ---------------- 任务体 ----------------

    def _run_task(self, spec: _RunSpec):
        # 防卡死：workflow 内部有阻塞 input()（base.py:98 API 失败回退、
        # zhihu.py:204 手动选题编号），Web 运行一律给默认值
        _orig_input = builtins.input
        builtins.input = lambda *a, **k: "1"
        # 重装 capture handler（每次运行独立队列，避免 handler 堆积）
        if self._handler is not None:
            log_capture.uninstall(self._handler)
        _last_log_ts[0] = time.time()
        self._handler = _TimedHandler()
        logging.getLogger().addHandler(self._handler)

        watchdog = threading.Thread(
            target=self._watchdog, args=(spec,), daemon=True)
        watchdog.start()

        try:
            if self._stop_flag.is_set():
                self._finish("stopped", "已取消")
                return
            result = self._dispatch(spec)
            if self._stop_flag.is_set():
                self._finish("stopped", "已中断")
            else:
                self._finish("done",
                             f"任务完成：{spec.mode} "
                             f"({'成功' if result else '未成功'})")
        except BaseException as exc:
            # BaseException：捕获 _StopInterrupt 注入（非 Exception 子类）
            if self._stop_flag.is_set():
                self._finish("stopped", f"已中断：{exc}")
            else:
                log.error("Web 任务异常：%s", exc, exc_info=True)
                self._finish("error", str(exc))
        finally:
            builtins.input = _orig_input
            try:
                from applications.zhihu_story.browser_adapter import (
                    close_shared_browser)
                close_shared_browser()
            except Exception:
                pass

    def _dispatch(self, spec: _RunSpec):
        from workflows.zhihu import ZhihuWorkflow

        wf = ZhihuWorkflow()
        mode = spec.mode

        if mode == "select":
            url = wf.select_topic()
            self.last_context = {"url": url or "", "title": "", "answer": "",
                                 "footer": {}}
            return bool(url)

        if mode == "extract":
            url = wf.select_topic()
            title, answer, footer, final_url = wf.extract_content()
            self.last_context = {
                "title": title, "answer": answer,
                "footer": footer or {}, "url": final_url or url or "",
            }
            # 采样注入预览（与 llm_api 采样分支同一函数）
            from core.story_text import sample_reference_sections
            self.last_context["sample_preview"] = \
                sample_reference_sections(answer) if answer else ""
            return bool(answer)

        if mode == "generate":
            url = wf.select_topic()
            title, answer, footer, final_url = wf.extract_content()
            self.last_context = {
                "title": title, "answer": answer,
                "footer": footer or {}, "url": final_url or url or "",
            }
            story = wf.generate_story(title, answer)
            if not story:
                raise RuntimeError("生成失败（模型无输出）")
            md_path = wf.save_story_file(story)
            self.last_story = {
                "text": story, "md_path": str(md_path),
                "chars": len(story),
            }
            return True

        if mode == "single":
            # run_single 内部完成提取与发布，通过回调把结果回填到
            # 上下文，让前端「提取结果」「生成故事」两张卡片在
            # 单轮全流程后都能展示
            def _on_extracted(title, answer, footer, url):
                self.last_context = {
                    "title": title, "answer": answer,
                    "footer": footer or {}, "url": url or "",
                }
                from core.story_text import sample_reference_sections
                self.last_context["sample_preview"] = \
                    sample_reference_sections(answer) if answer else ""

            def _on_story(story, md_path):
                self.last_story = {
                    "text": story,
                    "md_path": str(md_path),
                    "chars": len(story),
                }

            return wf.run_single(on_extracted=_on_extracted,
                                 on_story=_on_story)

        if mode == "batch":
            published = wf.run_batch(
                spec.gen_count, publish_count=spec.publish_count)
            return bool(published)

        if mode == "profile":
            # 作者文风提炼：读采集库 → 统计 → LLM 剖析 → 存 data/authors/
            from applications.zhihu_story.author_profiler import (
                AUTHORS_DIR, profile_author)
            if not spec.author:
                raise HTTPException(400, "缺少作者名（须与采集库 author 字段一致）")
            profile = profile_author(
                spec.author, progress=_task_progress_log)
            if not profile:
                raise RuntimeError(
                    f"提炼失败：作者「{spec.author}」在采集库中可用故事不足 2 篇"
                    f"或 LLM 剖析未返回有效签名")
            # 提炼即选择：立刻切换为当前文风，下次生成立即生效
            from config import set_runtime_author_profile
            eff = set_runtime_author_profile(spec.author)
            path = os.path.join(AUTHORS_DIR, f"{spec.author}.json")
            self.last_context = {
                "profile": {"title": f"文风签名：{spec.author}",
                            "path": path,
                            "summary": _profile_summary(profile)},
            }
            log.info("文风「%s」已保存 → %s（当前注入文风：%s）",
                     spec.author, path, eff["author_profile"])
            for line in _profile_summary(profile).splitlines():
                log.info("  %s", line)
            return True

        if mode == "collect":
            # 作者故事采集：URL → 自动识别作者 → 滚动列表去重采集
            if not spec.url:
                raise HTTPException(400, "缺少作者回答列表页 URL"
                                        "（zhihu.com/people/{token}/answers）")
            if spec.count < 1 or spec.count > 500:
                raise HTTPException(400, "采集数量须在 1-500 之间")
            from applications.zhihu_story.collector import collect_author_stories
            from applications.zhihu_story.browser_adapter import get_browser
            result = collect_author_stories(
                spec.url, count=spec.count, browser=get_browser())
            collected = result["collected"]
            author = result["author"]
            total = result.get("existing", 0) + len(collected)
            self.last_context = {
                "collect": {
                    "title": f"故事采集：{author}",
                    "summary": f"新增 {len(collected)} 篇"
                               f"（该作者库中共 {total} 篇；"
                               "重复/过短自动跳过）",
                },
            }
            log.info("采集结果：作者「%s」新增 %d 篇，该作者库中共 %d 篇",
                     author, len(collected), total)
            return bool(collected)

        if mode == "general_profile":
            # 通用写作风格提炼（跨作者顶层，存 _general.json）
            from applications.zhihu_story.author_profiler import (
                AUTHORS_DIR, GENERAL_PROFILE_FILE, profile_general)
            profile = profile_general(progress=_task_progress_log)
            if not profile:
                raise RuntimeError(
                    "通用风格提炼失败：采集库可用故事不足 3 篇"
                    "或 LLM 剖析未返回有效签名")
            from config import set_runtime_author_profile
            eff = set_runtime_author_profile("通用")
            path = os.path.join(AUTHORS_DIR, GENERAL_PROFILE_FILE)
            self.last_context = {
                "profile": {"title": "文风签名：通用（跨作者）",
                            "path": path,
                            "summary": _profile_summary(profile)},
            }
            log.info("通用文风已保存 → %s（当前注入文风：%s）",
                     path, eff["author_profile"])
            for line in _profile_summary(profile).splitlines():
                log.info("  %s", line)
            return True

        raise HTTPException(400, f"未知模式：{mode}")

    def _finish(self, state, message):
        # 先清进度再改状态：SSE/轮询看到 state 时 progress 必已为空，
        # 前端不会用过期进度覆盖完成态
        with self._lock:
            self.progress = None  # 任务结束，进度清空
        self._set_state(state, message)
        log.info("任务结束：%s", message)

    # ---------------- watchdog ----------------

    def _watchdog(self, spec: _RunSpec):
        deadline = time.time() + OVERALL_LIMIT
        # 文风提炼是一次性 LLM 剖析（非流式），调用期间无日志，
        # 单独放宽卡死阈值（剖析请求最长可达 10 分钟）
        stall = STALL_LIMIT * 3 if spec.mode in ("profile", "general_profile") \
            else STALL_LIMIT
        while True:
            time.sleep(15)
            if self._stop_flag.is_set():
                return
            with self._lock:
                if self.state != "running":
                    return
            age = time.time() - _last_log_ts[0]
            if age > stall:
                log.warning("watchdog：日志 %.0fs 无进展，判定卡死并中断",
                            age)
                self._stop_flag.set()  # 取消钩子生效，检查点抛异常
                self._set_state("timeout",
                                f"日志 {age:.0f}s 无进展，已判定卡死")
                return
            if time.time() > deadline:
                self._set_state("timeout", f"总时长超 {OVERALL_LIMIT:.0f}s")
                self._stop_flag.set()
                return


def _task_progress_log(text, pct=None):
    """progress 回调 → 「任务进度」日志行（log_capture 解析成 progress
    事件推给前端；pct=None 表示不确定进度，前端显示动画+计时）。

    同时写入 runner.progress：SSE 断连（如任务由命令行/其他浏览器
    触发）时前端轮询 /api/status 也能拿到当前阶段进度。
    """
    line = f"任务进度：{text}" + (f" | {pct}%" if pct is not None else "")
    log.info(line)
    with runner._lock:
        runner.progress = {"text": text, "pct": pct}


def _profile_summary(profile):
    """把提炼出的技能签名渲染成可读摘要（前端展示用）。"""
    sig = profile.get("signature") or {}
    lines = [f"文风：{sig.get('style', '（未提炼）')}",
             f"基调：{sig.get('tone', '（未提炼）')}",
             f"句法节奏：{sig.get('sentence_rhythm', '（未提炼）')}"]
    for k, label in (("opening_patterns", "开头技法"),
                     ("narrative_techniques", "叙事技法"),
                     ("signature_phrases", "惯用句式"),
                     ("avoid", "回避清单")):
        items = sig.get(k) or []
        if items:
            lines.append(f"{label}（{len(items)} 条）：")
            lines += [f"  - {i}" for i in items[:6]]
    src = profile.get("source_stories") or []
    lines.append(f"样本：{len(src)} 篇"
                 f"（{profile.get('profiled_at', '')}）")
    return "\n".join(lines)


runner = TaskRunner()
app = FastAPI(title="AutoQuill Web 控制台")


# ============================================================
# 页面与 API
# ============================================================

@app.get("/")
def index():
    if not INDEX_HTML.exists():
        raise HTTPException(404, "index.html 未找到")
    # 本地控制台：禁用静态缓存，避免浏览器加载旧版 HTML
    # （曾出现改了前端但 304 缓存旧版，日志历史不生效）
    return FileResponse(str(INDEX_HTML),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def api_config():
    """配置速览（只读）。模型字段取根 config 的实际生效值（运行时可切换）。"""
    cfg = {}
    try:
        from config import story as sconfig
        for k in ("QUESTION_SELECT_MODE", "ENABLE_STORY_FILTER",
                  "STORY_MATERIAL_MODE", "AUTHOR_PROFILE",
                  "ENABLE_FORMAT_RETRY", "MIN_ANSWER_LENGTH",
                  "ENABLE_MATERIAL_LIKES_GATE", "MATERIAL_MIN_LIKES",
                  "LONG_FORM_MODE", "KB_ENABLE"):
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


class _WebPresetSpec(BaseModel):
    preset: str  # fast（快速+深思+搜索）/ expert（专家+深思）


@app.post("/api/web-preset")
def api_set_web_preset(spec: _WebPresetSpec):
    """切换 DeepSeek 网页版模式预设（立即生效，持久化到 webui_model.json）。"""
    from config import set_web_mode_preset
    try:
        eff = set_web_mode_preset(spec.preset)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("Web 控制台切换网页模式预设 → %s", eff["preset"])
    return {"ok": True, "effective": eff}


@app.get("/api/models")
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


@app.post("/api/model")
def api_set_model(spec: _ModelSpec):
    """运行时切换故事生成模型（立即生效，持久化到 webui_model.json）。"""
    from config import set_runtime_model
    try:
        eff = set_runtime_model(spec.provider, spec.model_id)
    except Exception as exc:
        raise HTTPException(400, f"模型切换失败：{exc}")
    log.info("Web 控制台切换模型 → %s / %s", eff["provider"], eff["model_id"])
    return {"ok": True, "effective": eff}


@app.get("/api/mode")
def api_mode():
    """生成通道：api（API 调用）/ web（网页版浏览器操作）。"""
    from config import LLM_MODE
    return {"mode": LLM_MODE, "allowed": ["api", "web"]}


@app.post("/api/mode")
def api_set_mode(spec: _ModeSpec):
    """运行时切换生成通道（立即生效，持久化到 webui_model.json）。"""
    from config import set_runtime_mode
    try:
        eff = set_runtime_mode(spec.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("Web 控制台切换生成通道 → %s", eff["mode"])
    return {"ok": True, "effective": eff}


@app.get("/api/browser")
def api_browser():
    """浏览器模式：headless=False 前台调试 / True 无头工作。"""
    from config import BROWSER_HEADLESS
    return {"headless": BROWSER_HEADLESS}


@app.post("/api/browser")
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

_login_thread = None
_login_error = ""


def _setup_version():
    try:
        from core.version import VERSION
        return VERSION
    except ImportError:
        return "0.0.0"


@app.get("/api/setup/status")
def api_setup_status():
    """引导状态：Edge / API Key / 知乎登录 三项就绪检查。"""
    from applications.zhihu_story.browser_adapter import (
        EDGE_PATH, STORAGE_STATE_PATH)
    from config import LLM_MODEL_ID, LLM_PROVIDER, _load_provider_config
    llm_configured = False
    try:
        key = (_load_provider_config(LLM_PROVIDER, LLM_MODEL_ID)
               .get("apiKey") or "").strip()
        llm_configured = bool(key) and not key.startswith("sk-your-")
    except Exception:
        pass
    edge_ok = bool(EDGE_PATH)
    zhihu_logged_in = os.path.exists(STORAGE_STATE_PATH)
    return {
        "version": _setup_version(),
        "edge_ok": edge_ok,
        "llm_configured": llm_configured,
        "zhihu_logged_in": zhihu_logged_in,
        "login_running": (_login_thread is not None
                          and _login_thread.is_alive()),
        "login_error": _login_error,
        "setup_needed": not (edge_ok and llm_configured and zhihu_logged_in),
    }


class _ApiKeySpec(BaseModel):
    provider: str = "DeepSeek"
    api_key: str = ""


@app.post("/api/setup/apikey")
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
    log.info("首启引导：已写入服务商「%s」的 API Key", spec.provider)
    return {"ok": True, "effective": eff}


@app.post("/api/setup/test-api")
def api_setup_test_api():
    """实测当前配置的 API 连接（首启引导「测试连接」按钮）。"""
    from config import (LLM_API_BASE_URL, LLM_API_EXTRA_BODY,
                        LLM_API_KEY, LLM_API_MODEL)
    if not LLM_API_KEY or LLM_API_KEY.startswith("sk-your-"):
        raise HTTPException(400, "API Key 未配置或仍是占位符")
    if not LLM_API_BASE_URL:
        raise HTTPException(400, "缺少 baseUrl（服务商配置不完整）")
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": "请回复：连接成功"}],
        "max_tokens": 100,
        "stream": False,
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)
    try:
        resp = requests.post(
            f"{LLM_API_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LLM_API_KEY}"},
            json=payload, timeout=30)
        if resp.status_code == 200:
            reply = (resp.json().get("choices") or [{}])[0] \
                .get("message", {}).get("content", "")
            return {"ok": True, "detail": f"连接成功：{reply[:60]}"}
        return {"ok": False, "detail": f"HTTP {resp.status_code}："
                                       f"{resp.text[:200]}"}
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "detail": str(exc)}


@app.post("/api/setup/zhihu-login")
def api_setup_zhihu_login():
    """后台线程拉起可见 Edge 引导登录知乎；前端轮询 setup/status 收尾。"""
    global _login_error
    global _login_thread
    if _login_thread is not None and _login_thread.is_alive():
        raise HTTPException(409, "登录引导已在运行，请在弹出的 Edge 窗口完成登录")
    from applications.zhihu_story.browser_adapter import EDGE_PATH
    if not EDGE_PATH:
        raise HTTPException(400, "未找到 Microsoft Edge，请先安装 Edge 后重试")

    _login_error = ""

    def _run():
        global _login_error
        try:
            from applications.zhihu_story.browser_adapter import (
                login_zhihu_flow)
            ok, msg = login_zhihu_flow()
            log.info("首启引导：知乎登录%s", "成功" if ok else f"失败：{msg}")
            if not ok:
                _login_error = msg
        except Exception as exc:
            _login_error = str(exc)
            log.error("首启引导：知乎登录异常：%s", exc, exc_info=True)

    _login_thread = threading.Thread(target=_run, daemon=True)
    _login_thread.start()
    return {"ok": True,
            "message": "请在弹出的 Edge 窗口中完成登录（扫码/短信），"
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


@app.get("/api/update/check")
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


class _AuthorSpec(BaseModel):
    name: str  # 作者名；空串 = 不注入文风


@app.get("/api/authors")
def api_authors():
    """已提炼的文风签名列表（data/authors/*.json）+ 当前注入选择。"""
    from applications.zhihu_story.author_profiler import (
        AUTHORS_DIR, GENERAL_PROFILE_FILE, load_author_profile)
    from config.story import AUTHOR_PROFILE
    authors = []
    if os.path.isdir(AUTHORS_DIR):
        for f in sorted(os.listdir(AUTHORS_DIR)):
            if not f.endswith(".json"):
                continue
            is_general = (f == GENERAL_PROFILE_FILE)
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
    return {"authors": authors, "current": AUTHOR_PROFILE}


@app.post("/api/author")
def api_set_author(spec: _AuthorSpec):
    """运行时切换故事生成注入的作者文风（空串=不注入，持久化）。"""
    from config import set_runtime_author_profile
    try:
        eff = set_runtime_author_profile(spec.name)
    except Exception as exc:
        raise HTTPException(400, f"文风切换失败：{exc}")
    log.info("Web 控制台切换文风 → %r", eff["author_profile"])
    return {"ok": True, "effective": eff}


def _norm_storylib_url(url):
    """采集记录 answer_url 规范化（去 hash/query），删除匹配用。"""
    if not url:
        return ""
    return url.split("#")[0].split("?")[0]


def _read_storylib():
    """读采集库全部记录（跳过空行/坏行）。"""
    from applications.zhihu_story.author_profiler import STORY_LIB
    if not os.path.exists(STORY_LIB):
        return []
    records = []
    with open(STORY_LIB, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


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
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)
        authors.append({
            "name": name,
            "records": count,
            "has_profile": os.path.exists(
                os.path.join(AUTHORS_DIR, f"{safe}.json")),
        })
    return authors


@app.get("/api/storylib")
def api_storylib(author: str = ""):
    """采集库管理数据：不带 author 按作者聚合（含签名存在标记）；
    带 author 返回该作者的记录详情（单条删除用）。"""
    records = _read_storylib()
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


@app.delete("/api/storylib")
def api_storylib_delete(spec: _StoryLibDelSpec):
    """删除采集记录：author 整删该作者 / url 单条删（规范化匹配）。

    任务运行中拒绝（采集可能正在写库，重写会冲突）。"""
    if runner.state in ("running", "stopping"):
        raise HTTPException(409, "任务运行中，请先停止任务再清理采集库")
    if not spec.author and not spec.url:
        raise HTTPException(400, "须指定 author（整删）或 url（单条删）")
    from applications.zhihu_story.author_profiler import STORY_LIB
    if not os.path.exists(STORY_LIB):
        return {"ok": True, "removed": 0, "authors": []}

    target = _norm_storylib_url(spec.url)
    kept, removed = [], 0
    for rec in _read_storylib():
        if spec.author:
            if (rec.get("author") or "").strip() == spec.author:
                removed += 1
                continue
        elif target:
            if _norm_storylib_url(
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


@app.get("/api/profile-sources")
def api_profile_sources():
    """采集库（collected_stories.jsonl）中可提炼的作者与篇数。"""
    from applications.zhihu_story.author_profiler import (
        STORY_LIB, load_author_stories)
    if not os.path.exists(STORY_LIB):
        return {"authors": []}
    seen = {}
    with open(STORY_LIB, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            author = (rec.get("author") or "").strip()
            if not author:
                continue
            seen[author] = seen.get(author, 0) + 1
    return {"authors": [
        {"name": k, "records": v} for k, v in
        sorted(seen.items(), key=lambda kv: -kv[1])]}


@app.post("/api/run")
def api_run(spec: _RunSpec):
    runner.start(spec)
    return {"ok": True}


@app.post("/api/stop")
def api_stop():
    return runner.stop()


@app.get("/api/status")
def api_status():
    return runner.status()


def _current_log_file():
    """定位当前进程的业务日志文件（main.py basicConfig 的 FileHandler）。"""
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.FileHandler):
            p = Path(h.baseFilename)
            if p.name.startswith("autoquill_"):
                return p
    return None


@app.get("/api/logs/latest")
def api_logs_latest(lines: int = 100):
    """当前进程日志文件路径 + 尾部最近 N 行（前端刷新后回放用）。

    SSE 只推实时日志；刷新页面后历史不可见，用户曾误以为
    "运行日志里没有记录"。此端点补上历史回放入口。
    """
    log_file = _current_log_file()
    if log_file is None or not log_file.exists():
        return {"path": None, "lines": []}
    n = max(1, min(int(lines), 500))
    try:
        with open(log_file, "r", encoding="utf-8",
                  errors="replace") as fh:
            tail = fh.readlines()[-n:]
        return {"path": str(log_file), "lines": [l.rstrip("\r\n")
                                                 for l in tail]}
    except OSError:
        return {"path": str(log_file), "lines": []}


@app.get("/api/events")
def api_events():
    """SSE 流：日志行 + 状态事件。断开自动清理。"""

    def event_stream():
        last_state = None
        try:
            while True:
                if runner.state != last_state:
                    yield _sse("state", {"state": runner.state,
                                         "message": runner.message})
                    last_state = runner.state
                for line in log_capture.drain():
                    parsed = log_capture.parse_line(line)
                    if parsed:
                        ev, payload = parsed
                        payload["raw"] = line
                        yield _sse(ev, payload)
                time.sleep(0.3)
        except Exception:
            return

    return StreamingResponse(event_stream(),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/stories")
def api_stories():
    """output/ 故事列表（倒序，最新在前）。"""
    if not OUTPUT_DIR.exists():
        return {"stories": []}
    stories = []
    for f in sorted(OUTPUT_DIR.glob("story_*.md"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        stories.append({
            "name": f.name,
            "size": f.stat().st_size,
            "mtime": f.stat().st_mtime,
        })
    return {"stories": stories}


@app.get("/api/story")
def api_story(name: str):
    """单个故事全文；只用 basename，防路径穿越。"""
    if not name or name != Path(name).name:
        raise HTTPException(400, "非法文件名")
    path = OUTPUT_DIR / name
    if not path.is_file():
        raise HTTPException(404, "故事不存在")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"name": name, "text": text}


def run(host=HOST, port=PORT):
    import uvicorn
    # 取消钩子：browser_adapter 检查点据此中断 workflow（CLI 下为 None）
    from applications.zhihu_story import browser_adapter
    browser_adapter.set_cancel_hook(lambda: runner._stop_flag.is_set())
    print(f"\n  [AutoQuill] Web console: http://{host}:{port}")
    print(f"  Stop: Ctrl+C (interrupts running workflow first)\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
