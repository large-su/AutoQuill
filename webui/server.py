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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from webui import log_capture

log = logging.getLogger(__name__)

from core import paths

OUTPUT_DIR = Path(paths.data("output"))
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

HOST = "127.0.0.1"
from core.ports import WEB_PORT
PORT = WEB_PORT

# watchdog：日志超过 STALL 秒无进展或总时长超过 OVERALL 秒 → 判定卡死
# 240s 而非 180s：慢速页面/模型首 token 前等待窗口可能较长，留足余量
STALL_LIMIT = 240.0
OVERALL_LIMIT = 900.0
# 批量模式（Web 通道）单篇需 3-5 分钟，10 篇两两并行约 20-25 分钟；
# 若沿用 900s 一刀切总时长会被误杀（正常推进也会超时中断）。
OVERALL_LIMIT_BATCH = 3600.0

# run 线程最后一条日志的时间戳（CaptureHandler.emit 更新）
_last_log_ts = [0.0]

# 看板/草稿箱后台任务状态与浏览器互斥已迁移至 webui/browser_tasks.py


# ---------- LLM 通道可用性（任务前预检 / 切换 / 引导状态 / 测试连接共用） ----------

from .common import _llm_configured
from .api_library import router as library_router
from .api_runs import router as runs_router
from .api_settings import router as settings_router
from .api_setup import router as setup_router
from .api_feedback import router as feedback_router
from .run_manager import runner  # noqa: F401  (供存量扩展引用)


app = FastAPI(title="AutoQuill Web 控制台")

# 本地单机守卫：Host 白名单 + 同源 Origin 校验。
# 拦 DNS rebinding（攻击者域名解析到 127.0.0.1 后，浏览器带着攻击者
# Host 头直连本机端口）与跨站盲打；testserver 放行测试客户端。
_ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", "testserver"}
_ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}


@app.middleware("http")
async def _guard_localhost_only(request, call_next):
    host = (request.headers.get("host") or "").lower()
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "非法 Host：仅允许本机访问"},
                            status_code=403)
    origin = request.headers.get("origin")
    if origin and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "非法 Origin：跨站请求被拒绝"},
                            status_code=403)
    return await call_next(request)


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


@app.get("/echarts.min.js")
def _echarts_js():
    """看板图表库（本地打包，离线可用）。"""
    p = Path(__file__).resolve().parent / "static" / "echarts.min.js"
    if not p.is_file():
        raise HTTPException(404, "echarts.min.js 未找到")
    return FileResponse(str(p), media_type="application/javascript")


@app.get("/favicon.ico")
def _favicon():
    p = Path(__file__).resolve().parent / "static" / "favicon.ico"
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="image/x-icon")


def _static_css():
    p = Path(__file__).resolve().parent / "static" / "style.css"
    if not p.is_file():
        raise HTTPException(404, "style.css 未找到")
    return FileResponse(str(p), media_type="text/css; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


def _static_app_js():
    p = Path(__file__).resolve().parent / "static" / "app.js"
    if not p.is_file():
        raise HTTPException(404, "app.js 未找到")
    return FileResponse(str(p), media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


# 为保持与历史路径一致（index.html 引用），注册 http 路径
app.get("/style.css")(_static_css)
app.get("/app.js")(_static_app_js)



# —— P0 拆分：业务路由按域装配（实现见各 api_*.py / run_manager.py）
from webui.dashboard_api import register_dashboard
from webui.drafts_api import register_drafts
app.include_router(settings_router)
app.include_router(setup_router)
app.include_router(library_router)
app.include_router(runs_router)
app.include_router(feedback_router)

# —— 兼容门面：历史调用方(tests/外部脚本)习惯 webui.server.X 直接取用
from .api_library import *   # noqa: F401,F403
from .api_runs import *      # noqa: F401,F403
from .api_settings import *  # noqa: F401,F403
from .api_setup import *     # noqa: F401,F403
from .api_setup import _setup_version  # noqa: F401
from .run_manager import (   # noqa: F401
    _RunSpec, _TimedHandler, _profile_summary, _task_progress_log, TaskRunner,
)

def _register_snapshot_api():
    """挂载已发布内容看板与草稿箱素材的路由（保持 server 入口单一）。"""
    register_dashboard(app)
    register_drafts(app)


_register_snapshot_api()

def run(host=HOST, port=PORT):
    import uvicorn
    # 本地单机守卫按实际运行端口放行（否则改端口启动会全部 403）
    _ALLOWED_HOSTS.update({f"{host}:{port}", f"localhost:{port}"})
    _ALLOWED_ORIGINS.update({f"http://{host}:{port}", f"http://localhost:{port}"})
    # 取消钩子：browser_adapter 检查点据此中断 workflow（CLI 下为 None）
    from applications.zhihu_story import browser_adapter
    browser_adapter.set_cancel_hook(lambda: runner._stop_flag.is_set())
    print(f"\n  [AutoQuill] Web console: http://{host}:{port}")
    print(f"  Stop: Ctrl+C (interrupts running workflow first)\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
