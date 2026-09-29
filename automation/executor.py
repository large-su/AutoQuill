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


def _sync_progress(browser=None, *, note="", force=False, shared=False):
    """搭车校核线上进度（计数器必须来自线上，不能只信本地台账）。

    ★ 只在**浏览器已经开着**的时候调——校核本身不额外拉起浏览器，
      因此对任务时长的影响只有两次只读请求（实测 <2 秒）。
    ★ 这里也是 automation 层唯一接触 webui 的职能点；具体怎么读、怎么解析
      全在 webui.site_progress / core.progress，executor 不关心。
    ★ 读不到、解析失败、落盘失败一律只记日志：校核绝不能拖垮任务本身。

    shared=True：取共享浏览器单例（**不创建**）。
      Playwright 的 sync API 有线程粘性：在调度线程里 get_browser() 创建出来的
      单例，被另一个线程使用时会抛「Sync API inside the asyncio loop」，
      整个作业直接崩（2026-09-29 事故）。所以这里宁可「这次不校核」，
      也绝不在错误的线程里把浏览器唤醒。
    """
    try:
        if browser is None:
            if not shared:
                return None
            from web_drivers.browser_pool import get_shared_browser
            browser = get_shared_browser()
            if browser is None:
                log.debug("进度校核跳过：共享浏览器尚未启动（不在本线程创建）")
                return None
        from automation import store as _store
        from webui import site_progress
        day = _store.now_str()[:10]
        ledger_units = _store.done_counts(day).get("publish_drafts", 0)
        return site_progress.refresh(browser, ledger_units=ledger_units,
                                     note=note, force=force)
    except Exception as exc:                # noqa: BLE001
        log.warning("进度校核跳过（不影响任务）：%s", exc)
        return None


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
    # ★ 这里**刻意不做**进度校核（2026-09-29 事故复盘）：
    #   full_chain 的浏览器是 TaskRunner 在**另一个线程**里创建的，而本函数跑在
    #   调度线程；在这里调 get_browser() 会在调度线程把共享浏览器单例唤醒，
    #   之后 Playwright 的 sync API 跨线程使用就直接抛
    #   「It looks like you are using Playwright Sync API inside the asyncio loop」，
    #   把 publish_drafts 连同 full_chain 一起打挂。
    #   进度校核只在「本线程自己创建浏览器」的作业里搭车（发布/打卡/回复），
    #   这些点已经足够覆盖（发布前 + 发布失败后各一次）。
    # 打卡互动上下文：写草稿时顺带关注/赞同（打卡任务没启用 → 空上下文，
    # 工作流那侧零行为变化）。翻转兜底只在当天最后一班（is_last_of_day）。
    checkin_ctx = _checkin_context(job)
    _set_checkin_context(checkin_ctx)
    # ★ 上下文必须只活在「自动化派出的这一班」里：整段用 try/finally 包死，
    #   任何异常路径（启动失败/超时/人工介入）都清理干净。手动跑完整链路
    #   从不写这个上下文，所以永远不会触发关注/赞同（用户 2026-09-27 要求）。
    try:
        try:
            runner.start(_RunSpec(mode=mode, rounds=rounds))
        except Exception as exc:  # noqa: BLE001  （HTTPException 409 = 已有任务在跑）
            raise BrowserBusy("启动失败（可能已有任务在运行）：%s" % exc)
        st = {}
        while True:
            st = runner.status()
            state = st.get("state")
            if progress:
                try:
                    progress(st)
                except Exception:  # noqa: BLE001
                    pass
            if state in ("done", "error", "stopped", "timeout", "idle"):
                break
            if should_stop and should_stop():
                runner.stop()      # 用户停止：通知工作流在检查点中断
                log.info("自动化：收到停止请求，已请求中断当前撰写任务")
            time.sleep(2)
        if st.get("guide_needed"):
            raise NeedHuman("运行前检测未通过：%s" % st["guide_needed"])
    finally:
        _set_checkin_context(None)     # 作业结束（含异常路径）立即清理
    ok = state == "done"
    story = st.get("story") or {}
    message = st.get("message") or ("完成" if ok else "失败")
    if checkin_ctx:
        # 把「顺带完成的打卡互动」写进作业说明，时间轴/通知里一眼能看到
        try:
            from core import checkin as _ck
            line = (_ck.summary(_ck.load_state()) or {}).get("line") or ""
            if line:
                message = "%s；%s" % (message, line)
        except Exception:          # noqa: BLE001
            pass
    return {
        "ok": ok,
        "units": 1 if ok else 0,
        "status": STATUS_DONE if ok else STATUS_FAILED,
        "message": message,
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
            _sync_progress(b, note="发布前校核")
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
            if not r.get("ok") and r.get("reason") != "empty":
                # 发布没成功：**当场再校核一次**。这篇草稿如果已经不在草稿箱里，
                # 说明其实发出去了（今天的真实事故）——下一次排班就会用线上数字，
                # 不会再把已发布的算成没发、重复排班。
                _sync_progress(b, note="发布失败后核对", force=True)
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


def _set_checkin_context(ctx):
    """写/清打卡互动上下文（core.checkin 的进程内单例）。"""
    try:
        from core import checkin as _ck
        if ctx:
            _ck.set_context(ctx)
        else:
            _ck.clear_context()
    except Exception:              # noqa: BLE001
        pass


def _checkin_context(job):
    """当天这一班写草稿要不要顺带做打卡互动（关注 / 赞同）。

    打卡任务没启用时返回 {} → 工作流那侧完全不走互动逻辑。
    返回 {follow: do|toggle|done|skip, vote: 同, is_last: bool}。
    """
    try:
        from automation.store import load_plan
        from automation.model import TASK_TYPES
        cfg = (load_plan().get("tasks") or {}).get("checkin") or {}
        if not (cfg.get("enabled") and TASK_TYPES["checkin"]["implemented"]):
            return {}
        from core import checkin as _ck
        state = _ck.load_state()
        is_last = bool((job.get("params") or {}).get("is_last_of_day"))
        return {
            "is_last": is_last,
            "follow": _ck.decide(state, "follow", is_last=is_last),
            "vote": _ck.decide(state, "vote", is_last=is_last),
        }
    except Exception as exc:       # noqa: BLE001
        log.debug("打卡上下文构建失败（不影响撰写）：%s", exc)
        return {}


def _checkin(job, should_stop=None, progress=None):
    """打卡巡检（晚间兜底）：读当期打卡页，没达成时自己找目标补做。

    「写草稿顺带互动」是主路径，这一班是保险：当天草稿全失败 / 最后一班
    被跳过时，还有一次机会把关注、赞同补上（含取关再关注的翻转兜底）。
    另外它每次都会刷新当日打卡快照，界面据此显示今天打卡成没成。
    """
    from web_drivers.browser_pool import (
        ProfileBusy, _browser_lock, profile_in_use,
    )
    busy = _browser_busy()
    if busy:
        raise BrowserBusy("浏览器被占用：" + "、".join(busy))
    if profile_in_use():
        raise BrowserBusy("浏览器正被登录引导或其它实例占用，稍后顺延")
    from applications.zhihu_story.browser_adapter import (
        LOGIN_EXPIRED_MSG, ZhihuBrowser,
    )
    from applications.zhihu_story import checkin_task
    with _browser_lock:
        b = ZhihuBrowser(headless=True)
        try:
            b.start()
            if not b.is_logged_in():
                raise NeedHuman(LOGIN_EXPIRED_MSG)
            _sync_progress(b, note="打卡顺带校核")

            def _say(text):
                if progress:
                    try:
                        progress({"message": text})
                    except Exception:      # noqa: BLE001
                        pass

            r = checkin_task.run_checkin_job(b, progress=_say)
        except ProfileBusy as exc:
            raise BrowserBusy(str(exc))
        finally:
            try:
                b.close()
            except Exception:          # noqa: BLE001
                pass
    ok = bool(r.get("ok"))
    # 失败记 FAILED（触发当日补位重试，最晚仍在自己的时段内）；
    # 「今天不需要补做」是成功（ok=True, units=0），不是失败。
    return {
        "ok": ok,
        "units": max(0, int(r.get("units") or 0)),
        "status": STATUS_DONE if ok else STATUS_FAILED,
        "message": r.get("detail") or ("完成" if ok else "未完成"),
        "artifacts": [],
    }




def _reply_comment(job, should_stop=None, progress=None):
    """回复读者评论（每天 N 条，一次跑完；默认演练：只生成不发送）。

    与 publish_drafts / checkin 不同，本任务**同时要用共享浏览器和网页版大模型**
    （挑评论、写回复都走网页版），所以必须用共享实例 get_browser()——网页版
    驱动的页面就挂在同一个 context 上（独立 page，互不干扰）；另起独占实例会
    撞同一个 profile 锁（exitCode=21）。

    收尾顺序沿用 run_manager 的纪律：先删网页会话（需要页面/登录态还在），
    再关共享浏览器。

    ★ 2026-09-27 事故：这里最初把 `profile_in_use` 漏在了 import 之外，处理器
    一进来就 NameError——单测全绿、装到真机连续失败 3 次还被熔断自动停用。
    现在**所有前置检查要用的东西一律先 import 再使用**，并由
    tests/test_static_checks.py 的 pyflakes 扫描 + 处理器冒烟测试守住。
    """
    from web_drivers.browser_pool import (
        close_shared_browser, get_browser, profile_in_use,
    )
    from applications.zhihu_story.browser_adapter import LOGIN_EXPIRED_MSG
    busy = _browser_busy()
    if busy:
        raise BrowserBusy("浏览器被占用：" + "、".join(busy))
    if profile_in_use():
        raise BrowserBusy("浏览器正被登录引导或其它实例占用，稍后顺延")
    from applications.zhihu_story import reply_task
    params = job.get("params") or {}
    count = max(1, int(params.get("count") or 1))
    dry_run = bool(params.get("dry_run", True))

    def _say(text):
        if progress:
            try:
                progress({"message": text})
            except Exception:      # noqa: BLE001
                pass

    r = {}
    b = get_browser()          # 共享实例（懒启动，内部自己拿锁）
    try:
        if not b.is_logged_in():
            raise NeedHuman(LOGIN_EXPIRED_MSG)
        _sync_progress(b, note="回复评论前校核")
        r = reply_task.run_reply_job(b, count=count, dry_run=dry_run,
                                     progress=_say) or {}
    finally:
        try:
            from web_drivers import reset_driver
            reset_driver(delete_session=True)   # 会话纪律：用完即删
        except Exception:          # noqa: BLE001
            pass
        try:
            close_shared_browser()
        except Exception:          # noqa: BLE001
            pass
    ok = bool(r.get("ok"))
    return {
        "ok": ok,
        "units": max(0, int(r.get("units") or 0)),
        "status": STATUS_DONE if ok else STATUS_FAILED,
        "message": r.get("detail") or ("完成" if ok else "未完成"),
        "artifacts": [],
    }


_HANDLERS = {
    "full_chain": _full_chain,
    "publish_drafts": _publish_drafts,
    "checkin": _checkin,
    "reply_comment": _reply_comment,
}


def execute(job, should_stop=None, progress=None):
    """执行一个作业。异常按语义抛出，由调度器统一记账。"""
    handler = _HANDLERS.get(job.get("type"))
    if handler is None:
        return {"ok": False, "units": 0, "status": STATUS_SKIPPED,
                "message": "未知任务类型：%s" % job.get("type"), "artifacts": []}
    return handler(job, should_stop=should_stop, progress=progress)
