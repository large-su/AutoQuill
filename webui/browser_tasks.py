"""看板 / 草稿箱后台任务：共享状态字典 + 浏览器占用互斥。

四个后台任务（看板刷新/看板删除/草稿刷新/草稿删除）共用同一持久化
浏览器 profile，必须互斥；状态字典由各自 API 模块与前端轮询共用。
"""

# 已发布内容看板：后台刷新 / 从知乎删除任务状态（前端轮询）
# need_login：本任务发现知乎登录态失效（页面跳登录页）→ 前端提示重新登录；
# removed：删除成功后本地快照同步剔除的条数（2026-09-19 起删除即同步本地）。
_DASH_REFRESH = {"status": "idle", "progress": "", "count": 0, "pct": None,
                "error": "", "need_login": False}
_DASH_DEL = {"status": "idle", "progress": "", "count": 0, "deleted": 0,
             "removed": 0, "error": "", "need_login": False}

# 草稿箱：后台刷新 / 批量删除任务状态
_DRAFTS_REFRESH = {"status": "idle", "progress": "", "count": 0, "pct": None,
                  "error": "", "need_login": False}
_DRAFTS_DEL = {"status": "idle", "progress": "", "count": 0, "deleted": 0,
               "removed": 0, "error": "", "need_login": False}

# 知乎登录态失效标记：抓取/删除链路发现页面跳登录页时置位，任一操作成功后清除。
# api_setup 的 /api/setup/status 会把它带给前端（首启引导据此提示重新登录）。
_ZHIHU_LOGIN_STALE = {"stale": False, "reason": ""}


def mark_zhihu_login_stale(reason=""):
    """标记知乎登录态失效（供前端提示重新登录）。"""
    _ZHIHU_LOGIN_STALE["stale"] = True
    _ZHIHU_LOGIN_STALE["reason"] = reason or ""


def clear_zhihu_login_stale():
    """清除失效标记（任一知乎操作成功后调用）。"""
    _ZHIHU_LOGIN_STALE["stale"] = False
    _ZHIHU_LOGIN_STALE["reason"] = ""


def zhihu_login_stale():
    """当前是否已知知乎登录态失效（含原因）。"""
    return dict(_ZHIHU_LOGIN_STALE)

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
