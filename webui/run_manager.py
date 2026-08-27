# ============================================================
# webui/run_manager.py — 任务编排域（TaskRunner 及其日志/规格）
# P0 拆分自 server.py；无路由，供 api_runs 与应用装配层使用。
# ============================================================

from fastapi import HTTPException
from pathlib import Path
import builtins
import json
import logging
import os
import threading
import time

from webui import log_capture

from pydantic import BaseModel
# run 线程最后一条日志时间戳（log_capture.emit 更新 / watchdog 消费）
_last_log_ts = [0.0]

# 卡死判定（从 server.py 迁入）：日志超过 STALL 秒无进展或总时长
# 超过 OVERALL 秒 → 判定卡死。240s 而非 180s：慢速页面/首 token
# 等待窗口可能较长，留足余量。
STALL_LIMIT = 240.0
OVERALL_LIMIT = 900.0
# 批量模式（Web 通道）单篇需 3-5 分钟，10 篇两两并行约 20-25 分钟；
# 若沿用 900s 一刀切总时长会被误杀（正常推进也会超时中断）。
OVERALL_LIMIT_BATCH = 3600.0

log = logging.getLogger(__name__)

def _llm_configured():
    """当前服务商/模型的 API Key 是否就绪（非空、非占位符、非掩码）。"""
    from config import LLM_MODEL_ID, LLM_PROVIDER, _load_provider_config
    try:
        key = (_load_provider_config(LLM_PROVIDER, LLM_MODEL_ID)
               .get("apiKey") or "").strip()
    except Exception:
        return False
    return bool(key) and key != "密" and not key.startswith("sk-your-")


def _require_llm_ready(detail="API Key 未配置或仍是占位符"):
    """API 通道硬校验：未就绪抛 400（切换通道 / 测试连接用）。"""
    if not _llm_configured():
        raise HTTPException(400, detail)
    return True


def _require_zhihu_url(url):
    """采集 URL 域名白名单：仅接受 https 的 zhihu.com 及其子域。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "zhihu.com"
                                        or host.endswith(".zhihu.com")):
        raise HTTPException(400, "采集地址仅支持知乎域名"
                                "（https://*.zhihu.com/...）")



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
        self.guide_needed = None # 运行前检测失败："deepseek_login" / "api_key"（前端据此弹引导）
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
                "guide_needed": self.guide_needed,
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
            self.guide_needed = None  # 每次运行重新检测，清除上次引导标记
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
        # 闭环兜底：LLM 任务（生成/提炼）运行前检测当前通道可用性——
        # Web 通道查 DeepSeek 登录态、API 通道查 Key。检测失败不直接
        # 报错收场，而是置 guide_needed 让前端弹引导（与切换通道时的
        # 闭环预检同语义，覆盖「首启引导被跳过」的场景）。
        # 此时任务浏览器尚未启动（首次 get_browser 在 _dispatch 里），
        # 独立检测实例可安全使用同一 profile；运行中浏览器已占用，
        # 该检测会锁冲突误报，故只在此处（运行前）检查一次。
        if spec.mode in ("generate", "single", "batch",
                         "profile", "general_profile"):
            from config import LLM_MODE
            if LLM_MODE == "web":
                from web_drivers.deepseek import web_llm_logged_in
                if not web_llm_logged_in():
                    self._finish(
                        "error",
                        "Web 通道需要先登录 DeepSeek 网页版："
                        "请点右上角「设置」→ 引导窗口「打开 Edge 登录 "
                        "DeepSeek」，完成后重新运行",
                        guide="deepseek_login")
                    return
            else:
                if not _llm_configured():
                    self._finish(
                        "error",
                        "API 模式需要先配置 API Key："
                        "请点右上角「设置」→ 引导窗口填写 API Key，"
                        "完成后重新运行",
                        guide="api_key")
                    return
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
                from web_drivers.browser_pool import close_shared_browser
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
            story, _gen_ok = wf.generate_story_with_retry(title, answer)
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
                flavor = None
                try:
                    from tools.ai_flavor_check import check_text
                    got = check_text(story)
                    flavor = got[1] if got else None
                except Exception:
                    flavor = None
                self.last_story = {
                    "text": story,
                    "md_path": str(md_path),
                    "chars": len(story),
                    "ai_flavor": flavor,
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
            from web_drivers.browser_pool import get_browser
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

    def _finish(self, state, message, guide=None):
        # 先清进度再改状态：SSE/轮询看到 state 时 progress 必已为空，
        # 前端不会用过期进度覆盖完成态
        with self._lock:
            self.progress = None  # 任务结束，进度清空
            if guide is not None:
                self.guide_needed = guide  # 运行前检测失败：前端弹引导
        self._set_state(state, message)
        log.info("任务结束：%s", message)

    # ---------------- watchdog ----------------

    def _watchdog(self, spec: _RunSpec):
        # 批量模式放宽总时长上限（避免正常推进被一刀切误杀）
        overall = OVERALL_LIMIT_BATCH if spec.mode == "batch" else OVERALL_LIMIT
        deadline = time.time() + overall
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
                log.warning("watchdog：日志 %.0fs 无进展，判定卡死并中断（非用户操作）",
                            age)
                self._stop_flag.set()  # 取消钩子生效，检查点抛异常
                self._set_state("timeout",
                                f"日志 {age:.0f}s 无进展，已判定卡死")
                return
            if time.time() > deadline:
                log.warning("watchdog：总时长超 %.0fs（非用户操作），强制中断", overall)
                self._set_state("timeout", f"总时长超 {overall:.0f}s")
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

runner = TaskRunner()
