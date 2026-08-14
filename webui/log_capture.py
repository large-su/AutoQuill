# ============================================================
# webui/log_capture.py — workflow 日志捕获与里程碑解析
#
# Web 控制台要实时展示 workflow 运行日志。方案：在 root logger
# 挂一个 QueueHandler，把全量日志（workflows/llm_api/browser_adapter
# 全部 propagate 到 root）格式化成文本行放进内存队列；SSE 后端
# 从队列取行推给浏览器。
#
# parse_line 把日志行解析成事件类型，前端据此做高亮/进度条：
#   stage    「步骤 N：…」蓝色里程碑
#   progress 「生成中… 累计输出 N 字符」→ 估算进度（前端展示）
#   result   成功/失败关键行（提取成功/格式检测/流式生成完成/
#            草稿已保存/本轮完成）
#   error    含 ERROR/WARNING/失败 的行
#   log      其他
# ============================================================

import logging
import queue
import re

_QUEUE = queue.Queue()

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# 里程碑/进度/结果解析（按出现顺序匹配，返回 (event_type, payload)）
# 日志行带 "2026-08-11 12:00:00 [INFO] " 前缀，故不用 ^ 锚点
_STAGE_RE = re.compile(r"步骤\s*(\d+)\s*[:：]")
# 生成阶段心跳（正文增长）；旧文案「生成中… 累计输出 N 字符」保留兼容
_PROGRESS_RE = re.compile(r"故事生成中…\s*已生成\s*(\d+)\s*字")
_LEGACY_PROGRESS_RE = re.compile(r"生成中…\s*累计输出\s*(\d+)\s*字符")
# 思考阶段心跳（深度思考：正文为 0 时思考容器文本增长）
_THINK_PROGRESS_RE = re.compile(r"模型思考中…\s*已思考\s*(\d+)\s*字符")
# 任务阶段进度（文风提炼等无字符输出的任务）：文本 + 可选百分比；
# pct 缺失表示不确定进度（LLM 剖析中）
_TASK_PROGRESS_RE = re.compile(r"任务进度[：:]\s*(.+?)(?:\s*\|\s*(\d+)\s*%)?$")
_RESULT_OK_RE = re.compile(
    r"(提取成功|格式检测|流式生成完成|草稿已保存"
    r"|服务端草稿已确认|✓)")
_ERROR_RE = re.compile(r"\[(ERROR|WARNING)\]|失败|异常|放弃|降级")
_RUN_END_RE = re.compile(r"本轮完成|批量任务结束|目标达成|EXIT")


class CaptureHandler(logging.Handler):
    """把日志 record 格式化为文本行放进 _QUEUE。"""

    def __init__(self, q=None):
        super().__init__(level=logging.INFO)
        self._queue = q or _QUEUE

    def emit(self, record):
        try:
            line = self.format(record)
            self._queue.put_nowait(line)
        except Exception:
            pass


def install(q=None):
    """挂 CaptureHandler 到 root logger，返回 handler（uninstall 用）。

    顺带把 root level 放宽到 INFO（webui 需捕获 INFO 级日志；
    独立运行 webui 时 main.py 的 basicConfig 可能未生效）。
    """
    handler = CaptureHandler(q)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


def uninstall(handler):
    if handler is not None:
        logging.getLogger().removeHandler(handler)


def drain(q=None):
    """取走队列里所有行（SSE 轮询用），返回 list[str]。"""
    q = q or _QUEUE
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def parse_line(line):
    """解析一行日志 → (event_type, payload dict)。

    payload 固定带 text；progress 事件额外带 chars；stage 带 num。
    匹配顺序：stage → progress → run_end → result → error → log。
    """
    text = (line or "").strip()
    if not text:
        return None

    m = _STAGE_RE.search(text)
    if m:
        return ("stage", {"num": int(m.group(1)), "text": text})

    m = _PROGRESS_RE.search(text)
    if m:
        return ("progress", {"chars": int(m.group(1)), "text": text})

    m = _THINK_PROGRESS_RE.search(text)
    if m:
        return ("progress", {"think": True, "chars": int(m.group(1)),
                             "text": text})

    m = _LEGACY_PROGRESS_RE.search(text)
    if m:
        return ("progress", {"chars": int(m.group(1)), "text": text})

    m = _TASK_PROGRESS_RE.search(text)
    if m:
        pct = int(m.group(2)) if m.group(2) else None
        return ("progress", {"task": True, "pct": pct,
                             "text": m.group(1).strip()})

    if _RUN_END_RE.search(text):
        return ("run_end", {"text": text})

    if _RESULT_OK_RE.search(text):
        return ("result", {"ok": True, "text": text})

    if _ERROR_RE.search(text):
        return ("error", {"text": text})

    return ("log", {"text": text})


def summarize(line):
    """提取一行日志里最有信息量的中段（截断展示用），最多 90 字。"""
    text = (line or "").strip()
    return text if len(text) <= 90 else text[:90] + "…"
