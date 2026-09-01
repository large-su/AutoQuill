# ============================================================
# core/user_feedback.py — 用户意见反馈记录
#
# 用户在使用 AutoQuill 时随手记录「遇到的问题 / 想改进的点」，
# 追加写入一份 Markdown 文件（feedback.md）。后续迭代时，开发 /
# AI agent 直接打开该文件即可翻看历史意见。
#
# 存储位置：core.paths.data("feedback.md")
#   - 源码态：项目根 feedback.md
#   - 安装态：%APPDATA%/AutoQuill/feedback.md
#
# 并发安全：同一进程内用一把线程锁串行写；单次 write 追加。
# ============================================================

import logging
import os
import re
import threading
from datetime import datetime

from core import paths

log = logging.getLogger(__name__)

FEEDBACK_FILE = paths.data("feedback.md")

# 预设分类（可选；也可自由填，未命中按原值记录）
CATEGORIES = ["选题", "生成", "发布", "界面", "其他"]

_write_lock = threading.Lock()

_HEADER = """# AutoQuill 用户意见反馈

> 使用过程中随手记录的问题与改进建议。每新增一条，追加到文件末尾。
> 后续迭代时请先翻阅本文件。

"""

# 条目头正则：## YYYY-MM-DD HH:MM:SS · 分类
_ENTRY_HEAD_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · ")


def _fmt_entry(ts, category, text, context):
    lines = [f"## {ts} · {category or '其他'}"]
    if context:
        lines.append(f"> 上下文：{context}")
    lines.append("")
    lines.append(text.strip())
    lines.append("")
    return "\n".join(lines)


def _ensure_header():
    if not os.path.exists(FEEDBACK_FILE):
        os.makedirs(os.path.dirname(FEEDBACK_FILE) or ".", exist_ok=True)
        with open(FEEDBACK_FILE, "w", encoding="utf-8") as fh:
            fh.write(_HEADER)


def record(text, category=None, context=None):
    """追加一条意见反馈。

    参数：
        text:     问题/建议正文（必填）
        category: 分类（可选，如「选题/生成/发布/界面/其他」）
        context:  上下文（可选，如问题链接、所处环节、现象）
    返回写入的条目字符串；text 为空返回 None。
    """
    text = (text or "").strip()
    if not text:
        log.warning("反馈内容为空，忽略")
        return None
    category = (category or "其他").strip() or "其他"
    context = (context or "").strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = _fmt_entry(ts, category, text, context)
    with _write_lock:
        try:
            _ensure_header()
            with open(FEEDBACK_FILE, "a", encoding="utf-8") as fh:
                fh.write("\n" + entry + "\n")
        except OSError as exc:
            log.error("写入反馈失败：%s", exc)
            return None
    log.info("已记录反馈[%s]：%s", category, text[:60])
    return entry


def read(limit=None):
    """读取历史反馈，返回 [{time, category, context, text}, ...]（新→旧）。"""
    if not os.path.exists(FEEDBACK_FILE):
        return []
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        log.error("读取反馈失败：%s", exc)
        return []
    entries = _parse(content)
    if limit is not None:
        entries = entries[: int(limit)]
    return entries


def _parse(content):
    """把 feedback.md 解析为条目列表（新→旧）。

    以每个「## YYYY-MM-DD …」条目头切块；文件顶部标题/说明块被跳过。
    """
    blocks = re.split(r"(?m)(?=^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · )",
                      content)
    entries = []
    for b in blocks:
        b = b.strip()
        if not b or not b.startswith("## "):
            continue
        lines = b.split("\n")
        head = lines[0][3:].strip()          # 去掉 "## "
        time_str, _, category = head.partition(" · ")
        category = (category or "其他").strip()
        context = ""
        body_lines = []
        for ln in lines[1:]:
            if ln.startswith("> 上下文："):
                context = ln[len("> 上下文："):].strip()
            elif ln.strip():
                body_lines.append(ln)
        entries.append({
            "time": time_str.strip(),
            "category": category,
            "context": context,
            "text": "\n".join(body_lines).strip(),
        })
    entries.reverse()  # 文件为旧→新，反转为新→旧
    return entries
