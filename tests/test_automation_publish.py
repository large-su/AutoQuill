# -*- coding: utf-8 -*-
"""发布草稿（自动化 M2）回归：执行器语义 + 「最旧优先」的目标选择。

不碰真机：浏览器对象与页面交互全部用假件注入，真机 DOM（卡片选择器、
「发布回答」按钮、「发布设置」弹窗）由探针结论守护——
tools/archive/probes/probe_draft_publish.py。

用户口径 → 断言映射：
  - 草稿按「从旧到新」发布 → 列表倒序时取最后一张（最旧）；
  - 不可逆动作失败不能当成功 → ok=False → units=0 / STATUS_FAILED；
  - 登录失效不是发布失败 → NeedHuman（暂停全部自动化，不反复重试）；
  - 手动任务优先 → BrowserBusy（顺延，不计失败）。
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from core import paths
from automation import planner
from automation.executor import BrowserBusy, NeedHuman, execute
from automation.model import normalize_plan
from applications.zhihu_story.browser_write import WriteActionsMixin

ANSWER_URL = "https://www.zhihu.com/question/100/answer/999"
SIGNIN_URL = "https://www.zhihu.com/signin?next=%2Fcreator%2Fmanage%2Fcreation%2Fdraft"


class _FakePage:
    """只记录导航：publish_draft 的跳转顺序靠它断言。

    redirect 用来模拟「登录态失效」：知乎把草稿箱页 302 到 /signin，
    页面 URL 停在登录页（此时卡片必然解析为 0 张）。
    """

    def __init__(self, redirect=""):
        self.url = ""
        self.gotos = []
        self.redirect = redirect
        self.listeners = []          # 发布回执监听器（真实页面是 page.on 挂的）

    def goto(self, url, **kw):
        self.url = self.redirect or url
        self.gotos.append(url)

    def on(self, event, fn):
        self.listeners.append((event, fn))

    def remove_listener(self, event, fn, **kw):
        # Playwright 的 page.remove_listener(event, fn, boolean=False)：
        # 假件必须接受这个多余参数，否则真代码里的调用会抛 TypeError 被
        # 「摘监听失败也不影响发布」的兜底吞掉（测试就测不出泄漏）。
        self.listeners = [x for x in self.listeners if x != (event, fn)]


class _FakeWriteBrowser(WriteActionsMixin):
    """真实跑 WriteActionsMixin.publish_draft，只把页面交互换成脚本。

    这样「选哪张卡」「什么时候算发布成功」走的是生产代码，
    而不是测试里另写一遍逻辑。
    """

    def __init__(self, cards, has_button=True, redirect=""):
        self.page = _FakePage(redirect)
        self.cards = cards
        self.has_button = has_button
        self.clicked = 0            # JS 点击兜底次数
        self.native = 0             # 真实鼠标点击次数（优先走这条）
        self.probed = 0

    def native_click(self):
        """模拟一次成功的「真实鼠标点击」并计数。"""
        self.native += 1
        return {"ok": True, "how": "native"}

    def emits(self, body, status=200):
        """模拟服务端回执：喂给真实监听器（走生产的 _publish_receipt 解析）。"""
        for event, fn in list(self.page.listeners):
            if event == "response":
                fn(_FakeResponse("https://www.zhihu.com/api/v4/content/publish",
                                 status, body))
        return self

    def _click_publish_native(self):
        """按生产代码的真实优先级点发布：真实鼠标点击优先，失败才退回 JS 点击。

        2026-09-28 真机对照：JS 的 hit.click() 那次发布请求根本没发出去，
        必须用 Playwright 真实点击（假页面没有 get_by_role，这里直接模拟成功）。
        """
        if not self.has_button:
            return {"ok": False, "how": "", "reason": "no-button"}
        return self.native_click()

    def _safe_evaluate(self, js, *args, **kw):
        if js is WriteActionsMixin._DRAFT_CARDS_JS:
            return self.cards
        if js is WriteActionsMixin._PUBLISH_BTN_JS:
            self.probed += 1
            click = bool(args and args[0])
            if not self.has_button:
                return None
            if click:
                self.clicked += 1
            # 命中返回按钮信息（2026-09-28 起契约由 bool 改为 dict，
            # 供日志记录命中的 class/disabled 状态）
            return {"text": "发布回答", "cls": "Button Button--primary",
                    "disabled": False}
        if js is WriteActionsMixin._PUBLISH_CONFIRM_JS:
            return ""
        return True                              # 滚动/编辑器就绪等

    def get_draft_content(self, question_id=None):
        return ""


def _card(i, qid):
    """草稿卡：DOM 顺序 = 「编辑于」倒序（越靠前越新）。"""
    return {"index": i, "qid": str(qid), "title": "第%s篇" % qid,
            "href": "https://www.zhihu.com/question/%s#write" % qid}


class DraftPublishTest(unittest.TestCase):
    """草稿发布本体（浏览器层）。"""

    def setUp(self):
        # list_draft_cards 为等懒加载有多处 sleep：测试里全部短路
        self._sleep = mock.patch("time.sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()

    def test_oldest_card_is_chosen_when_qid_empty(self):
        # 列表倒序：111 最新、333 最旧 → 必须发 333
        b = _FakeWriteBrowser([_card(0, 111), _card(1, 222), _card(2, 333)])
        r = b.publish_draft()
        self.assertTrue(r["ok"])
        self.assertEqual(r["qid"], "333")
        self.assertEqual(b.page.gotos[-1],
                         "https://www.zhihu.com/question/333#write")
        # 点了「发布回答」：优先真实鼠标点击（Playwright 不可用时退回 JS 点击，
        # 假页面没有 get_by_role → 计数落在 JS 兜底那一次上）
        self.assertEqual(b.clicked, 1)

    def test_explicit_qid_targets_that_card(self):
        b = _FakeWriteBrowser([_card(0, 111), _card(1, 222), _card(2, 333)])
        r = b.publish_draft(qid="111")
        self.assertTrue(r["ok"])
        self.assertEqual(r["qid"], "111")
        self.assertEqual(b.page.gotos[-1],
                         "https://www.zhihu.com/question/111#write")

    def test_empty_draft_box_is_graceful(self):
        b = _FakeWriteBrowser([])
        r = b.publish_draft()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "empty")   # 空箱≠失败，调度器据此记跳过
        self.assertIn("没有可发布的草稿", r["detail"])
        self.assertEqual(b.clicked, 0)           # 没草稿就不该点任何发布按钮

    def test_missing_publish_button_is_reported_not_raised(self):
        b = _FakeWriteBrowser([_card(0, 111)], has_button=False)
        r = b.publish_draft()
        self.assertFalse(r["ok"])
        self.assertIn("发布回答", r["detail"])
        self.assertEqual(b.page.url,
                         "https://www.zhihu.com/question/111#write")

    def test_dry_run_probes_button_without_clicking(self):
        """演练：定位到「发布回答」就停手——发布不可逆，演练绝不能点。"""
        b = _FakeWriteBrowser([_card(0, 111), _card(1, 222)])
        r = b.publish_draft(dry_run=True)
        self.assertEqual(r["reason"], "dry_run")
        self.assertTrue(r["rehearsed"])
        self.assertEqual(r["qid"], "222")        # 仍然选最旧的一篇
        self.assertEqual(b.clicked, 0)           # 关键：没点
        self.assertEqual(b.probed, 1)
        self.assertNotEqual(b.page.url, ANSWER_URL)
        self.assertIn("草稿箱 2 篇", r["detail"])   # 演练报告要能人工核对

    def test_dry_run_fails_when_button_missing(self):
        b = _FakeWriteBrowser([_card(0, 111)], has_button=False)
        r = b.publish_draft(dry_run=True)
        self.assertEqual(r["reason"], "dry_run")
        self.assertFalse(r["rehearsed"])
        self.assertIn("演练未通过", r["detail"])

    def test_unknown_qid_reports_detail(self):
        b = _FakeWriteBrowser([_card(0, 111)])
        r = b.publish_draft(qid="777")
        self.assertFalse(r["ok"])
        self.assertIn("777", r["detail"])

    def test_signin_redirect_reports_need_login_not_empty(self):
        """登录态失效必须报「需要登录」，不能报「草稿箱空」。

        草稿箱页被 302 到 /signin 时卡片必然 0 张：若照此报成「草稿箱里没有
        可发布的草稿」，调度器会记「跳过」（不失败、不熔断、不通知），无人
        值守时静默空转，用户永远等不到「该重新登录了」这句话（2026-09-23 修）。
        """
        b = _FakeWriteBrowser([], redirect=SIGNIN_URL)
        r = b.publish_draft()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "need_login")
        self.assertIn("登录", r["detail"])
        self.assertEqual(b.clicked, 0)            # 绝不点发布
        self.assertNotEqual(r.get("reason"), "empty")


class PublishReceiptTest(unittest.TestCase):
    """发布回执解析：服务端用 HTTP 200 + body.code 表达业务失败。

    2026-09-28 真机定位：草稿「点了发布 90s 未确认」的真因是服务端
    `POST /api/v4/content/publish` 回了
    `{"code":403,"message":"当前问题不支持开启送礼物"}` ——
    只看 HTTP 状态码会把「被拒绝」当成「已提交」，于是干等到超时。
    """

    def _b(self, body, status=200):
        b = WriteActionsMixin()
        b._publish_receipt = WriteActionsMixin._publish_receipt.__get__(b)
        return b

    def test_code_403_is_a_definite_failure(self):
        b = self._b(None)
        watch = {"_events": [{"status": 200, "body": json.dumps(
            {"code": 403, "message": "当前问题不支持开启送礼物",
             "toast_message": "当前问题不支持开启送礼物"})}]}
        r = b._publish_receipt(watch)
        self.assertIs(r["ok"], False)
        self.assertIn("送礼物", r["detail"])

    def test_code_0_is_success(self):
        b = self._b(None)
        r = b._publish_receipt({"_events": [{"status": 200,
                                             "body": '{"code":0}'}]})
        self.assertIs(r["ok"], True)

    def test_no_receipt_yet_is_unknown(self):
        b = self._b(None)
        self.assertIsNone(b._publish_receipt({"_events": []})["ok"])
        self.assertIsNone(b._publish_receipt(None)["ok"])

    def test_http_500_without_json_is_failure(self):
        b = self._b(None)
        r = b._publish_receipt({"_events": [{"status": 500, "body": "oops"}]})
        self.assertIs(r["ok"], False)


class _FakeResponse:
    """Playwright Response 的最小替身（监听器只读 url/status/text）。"""

    def __init__(self, url, status, body):
        self.url = url
        self.status = status
        self._body = body

    def text(self):
        return self._body


class PublishGiftRetryTest(unittest.TestCase):
    """服务端因「送礼物」拒绝时：关掉送礼物后自动重试一次（真机错误码 403）。

    真机证据（2026-09-28）：
      POST /api/v4/content/publish ->
      {"code":403,"message":"当前问题不支持开启送礼物"}
    """

    GIFT_REJECT = json.dumps({
        "code": 403, "message": "当前问题不支持开启送礼物",
        "toast_message": "当前问题不支持开启送礼物"})

    def setUp(self):
        self._sleep = mock.patch("time.sleep")
        self._sleep.start()
        self.addCleanup(self._sleep.stop)
        self._dump = mock.patch.object(WriteActionsMixin, "_dump_page_state",
                                       lambda *a, **k: "")
        self._dump.start()
        self.addCleanup(self._dump.stop)

    def _browser(self, draft_content="x" * 999, receipt="GIFT_REJECT"):
        """假浏览器：草稿还在（否则立刻走「草稿已清空 = 成功」那一支）。

        receipt="GIFT_REJECT" → 每次点发布都回一次「送礼物被拒」；
        传 None 表示点击不发任何回执（模拟 JS 点击那种「服务端根本没收到」）。
        """
        b = _FakeWriteBrowser([_card(0, 111)])
        b.get_draft_content = lambda question_id=None: draft_content
        b._open_publish_settings = lambda: True
        b._turn_gift_off = lambda: True
        if receipt == "GIFT_REJECT":
            def _click():
                b.emits(self.GIFT_REJECT)
                return b.native_click()
            b._click_publish_native = _click
        elif receipt is None:
            b._click_publish_native = lambda: b.native_click()
        return b

    def test_gift_rejection_triggers_one_retry_then_success(self):
        b = self._browser()
        calls = {"n": 0}

        def _click():
            calls["n"] += 1
            # 第一次：被「送礼物」拒绝；第二次（关掉之后）：成功
            b.emits(self.GIFT_REJECT if calls["n"] == 1 else '{"code":0}')
            return b.native_click()

        b._click_publish_native = _click
        r = b.publish_draft()
        self.assertEqual(b.native, 2)          # 关掉后重试了一次
        self.assertTrue(r["ok"])

    def test_gift_rejection_is_only_retried_once(self):
        """关掉送礼物后仍被同样理由拒绝 → 如实上报，不再无脑重试。"""
        b = self._browser()
        r = b.publish_draft()
        self.assertFalse(r["ok"])
        self.assertIn("送礼物", r["detail"])
        self.assertEqual(b.native, 2)          # 总共两次，不是无限重试

    def test_unknown_rejection_is_reported_verbatim(self):
        b = self._browser(receipt=None)

        def _click():
            b.emits(json.dumps({"code": 403, "message": "内容涉嫌违规"}))
            return b.native_click()

        b._click_publish_native = _click
        r = b.publish_draft()
        self.assertFalse(r["ok"])
        self.assertIn("内容涉嫌违规", r["detail"])
        self.assertEqual(b.native, 1)          # 非送礼物问题不重试

    def test_no_receipt_at_all_is_reported_as_request_never_sent(self):
        """点了但服务端没收到请求（JS 点击不被受理）→ 报告要能区分出来。

        真机对照（2026-09-28）：JS 的 hit.click() 那次发布**没有任何网络请求**，
        页面只是把按钮变灰；Playwright 真实鼠标点击才会发出
        POST /api/v4/content/publish。所以「一条回执都没有」必须单独说清楚，
        否则用户只会看到「90s 未确认」这种没法排查的提示。
        """
        b = self._browser(receipt=None)
        r = b.publish_draft(verify_timeout=0)
        self.assertFalse(r["ok"])
        self.assertIn("没有收到", r["detail"])

    def test_watch_is_disarmed_even_when_click_explodes(self):
        """点击抛异常时也要摘掉回执监听（长驻进程里监听器不能越积越多）。"""
        b = self._browser()

        def _boom():
            raise RuntimeError("Target page, context or browser has been closed")

        b._click_publish_native = _boom
        with self.assertRaises(RuntimeError):
            b.publish_draft()
        self.assertEqual(b.page.listeners, [])



class _ExecFakeBrowser:
    """执行器假浏览器：记录 qid/headless/close，返回预置结果。"""

    result = {"ok": True, "qid": "333", "title": "第333篇",
              "url": ANSWER_URL, "detail": "页面已跳到回答页"}
    raise_exc = None

    def __init__(self, headless=False):
        self.headless = headless
        self.page = object()
        self.started = False
        self.closed = False
        self.published = False
        self.kw = {}
        _ExecFakeBrowser.last = self

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def publish_draft(self, qid="", progress=None, dry_run=False, **kw):
        self.published = True
        self.kw = {"qid": qid, "progress": progress, "dry_run": dry_run}
        if _ExecFakeBrowser.raise_exc:
            raise _ExecFakeBrowser.raise_exc
        return dict(_ExecFakeBrowser.result)


class PublishDraftExecutorTest(unittest.TestCase):
    """草稿发布作业：结果归一 + 异常语义。"""

    def setUp(self):
        _ExecFakeBrowser.raise_exc = None
        _ExecFakeBrowser.result = {"ok": True, "qid": "333",
                                   "title": "第333篇", "url": ANSWER_URL,
                                   "detail": "页面已跳到回答页"}
        self._patches = [
            mock.patch("applications.zhihu_story.browser_adapter.ZhihuBrowser",
                       _ExecFakeBrowser),
            mock.patch("webui.browser_tasks.browser_busy", lambda: []),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_success_counts_one_unit_and_closes_browser(self):
        job = {"type": "publish_drafts", "params": {}}
        seen = []
        r = execute(job, progress=lambda st: seen.append(st))
        self.assertTrue(r["ok"])
        self.assertEqual(r["units"], 1)
        self.assertEqual(r["status"], planner.STATUS_DONE)
        self.assertIn("第333篇", r["message"])
        self.assertEqual(r["artifacts"], [ANSWER_URL])
        b = _ExecFakeBrowser.last
        self.assertTrue(b.headless)              # 排班任务只在无头下跑
        self.assertTrue(b.closed)                # 关掉，别占持久化 profile 锁
        self.assertEqual(b.kw["qid"], "")        # 空 = 由浏览器层选最旧
        b.kw["progress"]("已点「发布回答」")
        self.assertEqual(seen[0]["message"], "已点「发布回答」")

    def test_explicit_qid_is_forwarded(self):
        execute({"type": "publish_drafts", "params": {"qid": 333}})
        self.assertEqual(_ExecFakeBrowser.last.kw["qid"], "333")

    def test_failure_is_not_counted_as_success(self):
        _ExecFakeBrowser.result = {"ok": False, "qid": "333", "title": "第333篇",
                                   "url": "", "detail": "已点发布，但 90s 内未确认到结果"}
        r = execute({"type": "publish_drafts", "params": {}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["units"], 0)
        self.assertEqual(r["status"], planner.STATUS_FAILED)
        self.assertIn("未确认", r["message"])

    def test_dry_run_job_is_forwarded_and_recorded_as_skip(self):
        _ExecFakeBrowser.result = {
            "ok": False, "reason": "dry_run", "rehearsed": True, "qid": "333",
            "title": "第333篇", "url": "",
            "detail": "演练通过：已定位草稿与「发布回答」按钮，未点击发布"}
        r = execute({"type": "publish_drafts", "params": {}, "dry_run": True})
        self.assertTrue(_ExecFakeBrowser.last.kw["dry_run"])   # 真的传下去了
        self.assertEqual(r["units"], 0)                        # 演练不占配额
        self.assertEqual(r["status"], planner.STATUS_SKIPPED)
        self.assertIn("演练通过", r["message"])

    def test_dry_run_failure_is_a_real_failure(self):
        _ExecFakeBrowser.result = {
            "ok": False, "reason": "dry_run", "rehearsed": False, "qid": "",
            "title": "", "url": "", "detail": "演练未通过：编辑器里没找到「发布回答」按钮"}
        r = execute({"type": "publish_drafts", "params": {}, "dry_run": True})
        self.assertEqual(r["status"], planner.STATUS_FAILED)

    def test_empty_draft_box_records_skip_not_failure(self):
        """空草稿箱记「跳过」：不计失败、不触发熔断、不占当日配额。"""
        _ExecFakeBrowser.result = {"ok": False, "reason": "empty", "qid": "",
                                   "title": "", "url": "",
                                   "detail": "草稿箱里没有可发布的草稿"}
        r = execute({"type": "publish_drafts", "params": {}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["units"], 0)
        self.assertEqual(r["status"], planner.STATUS_SKIPPED)

    def test_need_login_result_maps_to_need_human(self):
        """发布链路报「登录失效」→ NeedHuman（暂停全部自动化 + 通知人工）。

        2026-09-23 修：识别点在 publish_draft（草稿箱页真被 302 到 /signin 才
        判，浏览器层测试守着「绝不点发布」），执行器只按 reason 归一。此前那处
        page_needs_login(b.page) 预检写在 b.start() 之后、任何导航之前（page
        还是 about:blank），恒为假 → 登录失效时会一路走到「草稿箱空 = 跳过」。
        """
        _ExecFakeBrowser.result = {
            "ok": False, "reason": "need_login", "qid": "", "title": "",
            "url": SIGNIN_URL,
            "detail": "知乎登录态已失效（草稿箱页被重定向到登录页）"}
        with self.assertRaises(NeedHuman):
            execute({"type": "publish_drafts", "params": {}})
        self.assertTrue(_ExecFakeBrowser.last.started)   # 必须先开页面才能判断
        self.assertTrue(_ExecFakeBrowser.last.closed)    # 异常路径也要关浏览器

    def test_publish_job_takes_the_shared_browser_lock(self):
        """独占 profile：发布作业必须与共享浏览器/登录引导串行。

        Chromium 单例锁禁止同 user-data-dir 并发——不持锁时撞上一次网页版
        登录检查就是一次莫名的启动失败（Target page ... has been closed）。
        """
        import inspect
        from automation import executor as _exec
        self.assertIn("_browser_lock", inspect.getsource(_exec._publish_drafts))

    def test_adapter_login_exception_maps_to_need_human(self):
        from applications.zhihu_story.browser_adapter import ZhihuLoginRequired
        _ExecFakeBrowser.raise_exc = ZhihuLoginRequired("登录已失效")
        with self.assertRaises(NeedHuman):
            execute({"type": "publish_drafts", "params": {}})

    def test_busy_browser_defers_instead_of_failing(self):
        with mock.patch("webui.browser_tasks.browser_busy", lambda: ["审批任务"]):
            with self.assertRaises(BrowserBusy):
                execute({"type": "publish_drafts", "params": {}})


class PublishDraftPlannerTest(unittest.TestCase):
    """M2 接入后，发布草稿必须真的能被排班。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_pub_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.now = datetime(2026, 9, 20, 8, 0, 0)

    def tearDown(self):
        self._p.stop()

    def test_publish_drafts_is_scheduled_when_enabled(self):
        plan = normalize_plan({"enabled": True, "tasks": {
            "full_chain": {"enabled": False, "daily_cap": 0},
            "publish_drafts": {"enabled": True, "daily_cap": 3}}})
        day = planner.materialize_day(self.now, plan, {}, {})
        jobs = [j for j in day["schedule"]
                if j["type"] == "publish_drafts"
                and j["status"] == planner.STATUS_PLANNED]
        self.assertEqual(len(jobs), 3)

    def test_default_plan_schedules_both_writing_and_publishing(self):
        plan = normalize_plan({})
        day = planner.materialize_day(self.now, plan, {}, {})
        planned = [j for j in day["schedule"]
                   if j["status"] == planner.STATUS_PLANNED]
        kinds = [j["type"] for j in planned]
        self.assertEqual(kinds.count("full_chain"), 3)
        self.assertEqual(kinds.count("publish_drafts"), 3)
        # 同一时刻撞车时发布优先：对外可见的动作尽量落在白天（TASK_PRIORITY）
        same = [j for j in planned if j["planned_at"] == planned[0]["planned_at"]]
        self.assertEqual(planned[0]["type"], "publish_drafts")
        self.assertTrue(all(j["type"] == "publish_drafts" for j in same))


if __name__ == "__main__":
    unittest.main()
