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
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from webui import log_capture

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = _PROJECT_ROOT / "output"
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

HOST = "127.0.0.1"
PORT = 8787

# watchdog：日志超过 STALL 秒无进展或总时长超过 OVERALL 秒 → 判定卡死
STALL_LIMIT = 180.0
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
    mode: str  # select | extract | generate | single | batch
    gen_count: int = 5
    publish_count: int = 3


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
        self._handler = None

    # ---------------- 状态 ----------------

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "context": self.last_context,
                "story": self.last_story,
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
                log.error("Web 任务异常：%s", exc)
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
            return wf.run_single()

        if mode == "batch":
            published = wf.run_batch(
                spec.gen_count, publish_count=spec.publish_count)
            return bool(published)

        raise HTTPException(400, f"未知模式：{mode}")

    def _finish(self, state, message):
        self._set_state(state, message)
        log.info("任务结束：%s", message)

    # ---------------- watchdog ----------------

    def _watchdog(self, spec: _RunSpec):
        deadline = time.time() + OVERALL_LIMIT
        while True:
            time.sleep(15)
            if self._stop_flag.is_set():
                return
            with self._lock:
                if self.state != "running":
                    return
            age = time.time() - _last_log_ts[0]
            if age > STALL_LIMIT:
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


runner = TaskRunner()
app = FastAPI(title="AutoQuill Web 控制台")


# ============================================================
# 页面与 API
# ============================================================

@app.get("/")
def index():
    if not INDEX_HTML.exists():
        raise HTTPException(404, "index.html 未找到")
    return FileResponse(str(INDEX_HTML))


@app.get("/api/config")
def api_config():
    """配置速览（只读，import 时一次性求值）。"""
    cfg = {}
    try:
        from applications.zhihu_story import config as sconfig
        for k in ("QUESTION_SELECT_MODE", "ENABLE_STORY_FILTER",
                  "STORY_MATERIAL_MODE", "AUTHOR_PROFILE",
                  "ENABLE_FORMAT_RETRY", "MIN_ANSWER_LENGTH",
                  "ENABLE_MATERIAL_LIKES_GATE", "MATERIAL_MIN_LIKES",
                  "LONG_FORM_MODE", "META_LEARN_ENABLE", "KB_ENABLE",
                  "LLM_MODEL"):
            cfg[k] = getattr(sconfig, k, None)
    except Exception as exc:
        cfg["_app_error"] = str(exc)
    try:
        from config import LLM_MODE, LLM_PROVIDER, WEB_DRIVER_NAME
        cfg["LLM_MODE"] = LLM_MODE
        cfg["LLM_PROVIDER"] = LLM_PROVIDER
        cfg["WEB_DRIVER_NAME"] = WEB_DRIVER_NAME
    except Exception as exc:
        cfg["_root_error"] = str(exc)
    return cfg


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
