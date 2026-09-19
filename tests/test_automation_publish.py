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


class _FakePage:
    """只记录导航：publish_draft 的跳转顺序靠它断言。"""

    def __init__(self):
        self.url = ""
        self.gotos = []

    def goto(self, url, **kw):
        self.url = url
        self.gotos.append(url)


class _FakeWriteBrowser(WriteActionsMixin):
    """真实跑 WriteActionsMixin.publish_draft，只把页面交互换成脚本。

    这样「选哪张卡」「什么时候算发布成功」走的是生产代码，
    而不是测试里另写一遍逻辑。
    """

    def __init__(self, cards, has_button=True):
        self.page = _FakePage()
        self.cards = cards
        self.has_button = has_button
        self.clicked = 0
        self.probed = 0

    def _safe_evaluate(self, js, *args, **kw):
        if js is WriteActionsMixin._DRAFT_CARDS_JS:
            return self.cards
        if js is WriteActionsMixin._PUBLISH_BTN_JS:
            self.probed += 1
            click = bool(args and args[0])
            if not self.has_button:
                return False
            if click:
                self.clicked += 1
                self.page.url = ANSWER_URL      # 点发布后跳到回答页
            return True
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
        self.assertEqual(r["url"], ANSWER_URL)
        self.assertEqual(b.page.gotos[-1],
                         "https://www.zhihu.com/question/333#write")
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
            mock.patch("applications.zhihu_story.browser_adapter"
                       ".page_needs_login", lambda page: False),
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

    def test_login_expired_needs_human_and_skips_publish(self):
        self._patches[1].stop()
        with mock.patch("applications.zhihu_story.browser_adapter"
                        ".page_needs_login", lambda page: True):
            with self.assertRaises(NeedHuman):
                execute({"type": "publish_drafts", "params": {}})
        self.assertTrue(_ExecFakeBrowser.last.started)   # 必须先开页面才能判断
        self.assertFalse(_ExecFakeBrowser.last.published)  # 但绝不点发布
        self.assertTrue(_ExecFakeBrowser.last.closed)

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
