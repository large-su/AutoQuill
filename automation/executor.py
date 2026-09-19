# -*- coding: utf-8 -*-
"""作业执行：把作业交给现有能力，并把结果归一成台账字段。

刻意的分层：本模块**不直接操作 DOM**，只调用既有能力：
  - full_chain → webui.run_manager.TaskRunner（经典/纯净完整链路，rounds=1 = 一篇）；
  - publish_drafts（M2）→ applications/zhihu_story 的草稿发布能力；
  - 其余类型（打卡/感谢/评论回复）→ 本期留接口，返回「未实现」而不是假装成功。

异常语义（调度器据此决策，不混为一谈）：
  BrowserBusy   浏览器被手动任务占用 → 排队稍后再来，**不算失败**；
  NeedHuman     登录失效 / 通道未配置 / 验证码 → 暂停全部自动化并通知用户。
"""

import logging
import time

from automation.planner import (
    STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED,
)

log = logging.getLogger(__name__)


class NeedHuman(RuntimeError):
    """需要人工介入（登录失效 / 未配置通道 / 验证码）。"""


class BrowserBusy(RuntimeError):
    """浏览器被其它任务占用（手动操作优先）。"""


def _browser_busy():
    from webui.browser_tasks import browser_busy
    return browser_busy()


def _full_chain(job, should_stop=None, progress=None):
    """全链路撰写一篇：复用 TaskRunner 的经典/纯净完整链路（rounds=1）。"""
    from webui.run_manager import _RunSpec, runner
    busy = _browser_busy()
    if busy:
        raise BrowserBusy("浏览器被占用：" + "、".join(busy))
    params = job.get("params") or {}
    mode = params.get("mode") if params.get("mode") in ("single", "clean") else "single"
    rounds = max(1, int(params.get("rounds") or 1))
    try:
        runner.start(_RunSpec(mode=mode, rounds=rounds))
    except Exception as exc:      # noqa: BLE001  （HTTPException 409 = 已有任务在跑）
        raise BrowserBusy("启动失败（可能已有任务在运行）：%s" % exc)
    st = {}
    while True:
        st = runner.status()
        state = st.get("state")
        if progress:
            try:
                progress(st)
            except Exception:      # noqa: BLE001
                pass
        if state in ("done", "error", "stopped", "timeout", "idle"):
            break
        if should_stop and should_stop():
            runner.stop()          # 用户停止：通知工作流在检查点中断
            log.info("自动化：收到停止请求，已请求中断当前撰写任务")
        time.sleep(2)
    if st.get("guide_needed"):
        raise NeedHuman("运行前检测未通过：%s" % st["guide_needed"])
    ok = state == "done"
    story = st.get("story") or {}
    return {
        "ok": ok,
        "units": 1 if ok else 0,
        "status": STATUS_DONE if ok else STATUS_FAILED,
        "message": st.get("message") or ("完成" if ok else "失败"),
        "artifacts": [story.get("md_path")] if story.get("md_path") else [],
    }


def _unimplemented(job, should_stop=None, progress=None):
    """留接口的任务类型（打卡 / 感谢 / 评论回复 / 草稿发布未接入时）。"""
    from automation.model import task_label
    return {"ok": False, "units": 0, "status": STATUS_SKIPPED,
            "message": "%s：能力尚未接入（预留接口）" % task_label(job.get("type")),
            "artifacts": []}


_HANDLERS = {
    "full_chain": _full_chain,
    "publish_drafts": _unimplemented,
    "checkin": _unimplemented,
    "thank": _unimplemented,
    "reply_comment": _unimplemented,
}


def execute(job, should_stop=None, progress=None):
    """执行一个作业。异常按语义抛出，由调度器统一记账。"""
    handler = _HANDLERS.get(job.get("type"))
    if handler is None:
        return {"ok": False, "units": 0, "status": STATUS_SKIPPED,
                "message": "未知任务类型：%s" % job.get("type"), "artifacts": []}
    return handler(job, should_stop=should_stop, progress=progress)
