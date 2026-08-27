# ============================================================
# webui/api_library.py — 内容库域：故事库/来源画像
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
from .run_manager import runner
from core import paths

log = logging.getLogger(__name__)

router = APIRouter()
OUTPUT_DIR = Path(paths.data("output"))

@router.get("/api/stories")
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


@router.get("/api/story")
def api_story(name: str):
    """单个故事全文；只用 basename，防路径穿越。"""
    if not name or name != Path(name).name:
        raise HTTPException(400, "非法文件名")
    path = OUTPUT_DIR / name
    if not path.is_file():
        raise HTTPException(404, "故事不存在")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"name": name, "text": text}

# ============================================================
# 已发布内容看板 / 草稿箱素材 API：模块化后由下述注册调用挂载
# ============================================================

from webui.dashboard_api import register_dashboard
from webui.drafts_api import register_drafts
