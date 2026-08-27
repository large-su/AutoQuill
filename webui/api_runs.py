# ============================================================
# webui/api_runs.py — 运行域：启动/停止/状态/日志/SSE 事件流/故事库读取
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

from .common import (_llm_configured, _require_llm_ready,
                     _require_zhihu_url, _current_log_file)
from .run_manager import _RunSpec, runner
from core import paths

log = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/run")
def api_run(spec: _RunSpec):
    if spec.mode == "collect":
        _require_zhihu_url(spec.url)
    runner.start(spec)
    return {"ok": True}


@router.post("/api/stop")
def api_stop():
    return runner.stop()


@router.get("/api/status")
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



@router.get("/api/logs/latest")
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


@router.get("/api/events")
def api_events():
    """SSE 流：日志行 + 状态事件。断开自动清理。"""

    def event_stream():
        last_state = None
        try:
            while True:
                if runner.state != last_state:
                    yield _sse("state", {"state": runner.state,
                                         "message": runner.message,
                                         "guide_needed": runner.guide_needed})
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
