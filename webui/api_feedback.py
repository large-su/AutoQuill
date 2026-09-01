# ============================================================
# webui/api_feedback.py — 意见反馈域：记录 / 读取用户随手反馈
#
# 前端「反馈」按钮 → POST /api/feedback；GET 用于回显最近记录。
# 存储与解析见 core/user_feedback.py。
# ============================================================

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from core.user_feedback import read, record

log = logging.getLogger(__name__)

router = APIRouter()


class _FeedbackBody(BaseModel):
    text: str
    category: str = "其他"
    context: str = ""


@router.post("/api/feedback")
def api_feedback_post(body: _FeedbackBody):
    """追加一条意见反馈。"""
    entry = record(body.text, category=body.category, context=body.context)
    if not entry:
        return {"ok": False, "error": "内容为空"}
    return {"ok": True}


@router.get("/api/feedback")
def api_feedback_list(limit: int = 20):
    """读取最近 N 条反馈（新→旧）。"""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    return {"items": read(limit=limit)}
