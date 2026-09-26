# -*- coding: utf-8 -*-
"""作业执行：把作业交给现有能力，并把结果归一成台账字段。

刻意的分层：本模块**不直接操作 DOM**，只调用既有能力：
  - full_chain → webui.run_manager.TaskRunner（经典/纯净完整链路，rounds=1 = 一篇）；
  - publish_drafts（M2）→ applications/zhihu_story 的草稿发布能力；
  - 任务类型只有两个（用户 2026-09-24 口径）：full_chain（写故事）、
    publish_drafts（发布草稿）。打卡/互动类不做——登记表里没有的类型
    一律返回「未知任务类型」，不假装成功。

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
    from web_drivers.browser_pool import profile_in_use
    if profile_in_use():
        # 登录引导/其它实例正独占 profile（2026-09-26）：这次不硬闯，
        # 记 BrowserBusy 顺延重排（不计失败、不触发熔断）
        raise BrowserBusy("浏览器正被登录引导或其它实例占用，稍后顺延")
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


def _publish_drafts(job, should_stop=None, progress=None):
    """发布草稿箱里最旧的一篇（不可逆：草稿变公开回答）。

    复用 applications/zhihu_story/browser_write.publish_draft（真机探针确认的 DOM：
    草稿列表 → 编辑页 → 「发布回答」→ 确认弹窗 → 校验）。
    失败不自动重试（不可逆动作），交给调度器的熔断与人工介入；
    登录失效统一转成 NeedHuman，避免把「未登录」误判成发布失败而反复重试。

    ★ 2026-09-23 两处修（自动化发布「一直跑不通」的直接原因）：
      · 登录预检原本写在这句 page_needs_login(b.page)——此刻 page 还是
        about:blank（b.start() 只拉起上下文，没有任何导航），判断恒为假。
        于是登录失效时照样去开草稿箱页、拿到 0 张卡，被归成「草稿箱里
        没有可发布的草稿 = 跳过」：不失败、不熔断、不通知，无人值守时
        静默空转。识别下沉到 publish_draft（草稿箱页真被 302 到 /signin
        才判），这里只按 reason 归一成 NeedHuman；
      · 独占 profile 必须持 _browser_lock（与共享浏览器、登录引导、网页版
        登录检查串行）——原先裸起实例，撞上网页版登录检查就是一次莫名的
        启动失败（Chromium 单例锁禁止同目录并发）。
    """
    from applications.zhihu_story.browser_adapter import (
        LOGIN_EXPIRED_MSG, ZhihuBrowser, ZhihuLoginRequired,
    )
    from web_drivers.browser_pool import (
        ProfileBusy, _browser_lock, profile_in_use,
    )
    busy = _browser_busy()
    if busy:
        raise BrowserBusy("浏览器被占用：" + "、".join(busy))
    if profile_in_use():
        raise BrowserBusy("浏览器正被登录引导或其它实例占用，稍后顺延")
    with _browser_lock:
        b = ZhihuBrowser(headless=True)
        try:
            b.start()
            params = job.get("params") or {}
            qid = str(params.get("qid") or "")

            def _say(text):
                """浏览器层只回报文本；这里转成调度器的状态字典（界面据此显示进度）。"""
                if progress:
                    try:
                        progress({"message": text})
                    except Exception:      # noqa: BLE001
                        pass

            r = b.publish_draft(qid=qid, progress=_say,
                                dry_run=bool(job.get("dry_run")))
        except ProfileBusy as exc:
            # 租约被抢（登录引导/并发实例）：不算失败，顺延重排
            raise BrowserBusy(str(exc))
        except ZhihuLoginRequired as exc:
            raise NeedHuman(str(exc))
        finally:
            try:
                b.close()
            except Exception:          # noqa: BLE001
                pass
        if r.get("reason") == "need_login":
            # 登录失效：调度器收到 NeedHuman 会暂停全部自动化并通知人工，
            # 绝不记成「发布失败」而反复重试（也不该静默跳过）
            raise NeedHuman(r.get("detail") or LOGIN_EXPIRED_MSG)
        ok = bool(r.get("ok"))
        if not ok and r.get("reason") == "dry_run":
            # 演练：走完「找草稿 → 开编辑页 → 确认发布按钮」，绝不点发布。
            # 通过记跳过（不是发布成功，也不占配额）；未通过才是真问题。
            return {"ok": False, "units": 0,
                    "status": (STATUS_SKIPPED if r.get("rehearsed")
                               else STATUS_FAILED),
                    "message": r.get("detail") or "演练完成（未发布）",
                    "artifacts": []}
        if not ok and r.get("reason") == "empty":
            # 草稿箱空 = 今天没有可发的，记「跳过」：不计失败、不触发熔断，
            # 也不占用当日配额（done_counts 只累加 status=done 的 units）。
            return {"ok": False, "units": 0, "status": STATUS_SKIPPED,
                    "message": r.get("detail") or "草稿箱里没有待发布的草稿",
                    "artifacts": []}
        return {
            "ok": ok,
            "units": 1 if ok else 0,
            "status": STATUS_DONE if ok else STATUS_FAILED,
            "message": ("已发布《%s》" % r.get("title")) if ok
                       else (r.get("detail") or "发布未确认"),
            "artifacts": [r.get("url")] if r.get("url") else [],
        }


_HANDLERS = {
    "full_chain": _full_chain,
    "publish_drafts": _publish_drafts,
}


def execute(job, should_stop=None, progress=None):
    """执行一个作业。异常按语义抛出，由调度器统一记账。"""
    handler = _HANDLERS.get(job.get("type"))
    if handler is None:
        return {"ok": False, "units": 0, "status": STATUS_SKIPPED,
                "message": "未知任务类型：%s" % job.get("type"), "artifacts": []}
    return handler(job, should_stop=should_stop, progress=progress)
