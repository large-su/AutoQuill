"""看板 / 草稿箱后台任务：共享状态字典 + 浏览器占用互斥。

四个后台任务（看板刷新/看板删除/草稿刷新/草稿删除）共用同一持久化
浏览器 profile，必须互斥；状态字典由各自 API 模块与前端轮询共用。
"""

# 已发布内容看板：后台刷新 / 从知乎删除任务状态（前端轮询）
_DASH_REFRESH = {"status": "idle", "progress": "", "count": 0, "pct": None, "error": ""}
_DASH_DEL = {"status": "idle", "progress": "", "count": 0, "deleted": 0, "error": ""}

# 草稿箱：后台刷新 / 批量删除任务状态
_DRAFTS_REFRESH = {"status": "idle", "progress": "", "count": 0, "pct": None, "error": ""}
_DRAFTS_DEL = {"status": "idle", "progress": "", "count": 0, "deleted": 0, "error": ""}

_TASK_LABELS = (
    (_DASH_REFRESH, "看板刷新"),
    (_DASH_DEL, "看板删除"),
    (_DRAFTS_REFRESH, "草稿刷新"),
    (_DRAFTS_DEL, "草稿删除"),
)


def browser_busy():
    """返回正在占用浏览器的任务名列表（全部任务共用 profile，必须互斥）。"""
    return [label for state, label in _TASK_LABELS if state["status"] == "running"]


def busy_message():
    """互斥拒绝时的提示；无占用返回 None。"""
    busy = browser_busy()
    return "「" + busy[0] + "」任务进行中，请完成后再试" if busy else None
