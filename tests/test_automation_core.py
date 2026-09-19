# -*- coding: utf-8 -*-
"""自动化模块核心逻辑回归（纯逻辑：可注入时钟与执行器，不碰浏览器）。

用户口径 → 断言映射：
  - 配额 3/3 可调、间隔 ≥1 小时且随机化、当日完成即可 → 排班测试；
  - 随时可停、再开始时先看今天已做多少 → 续做测试（按台账扣减）；
  - 冲突排队（手动优先）→ BrowserBusy 顺延测试；
  - 无人化不能黑箱 → 台账/熔断/暂停通知测试。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from core import paths
from automation import planner, store
from automation.executor import BrowserBusy, NeedHuman
from automation.model import TASK_TYPES, normalize_plan
from automation.scheduler import AutomationScheduler


def _only(task_type, **cfg):
    """只启用一个任务类型（其余全关）。

    默认计划里 full_chain / publish_drafts 都是开启的（用户口径：撰写 3 + 发布 3），
    单类型断言必须显式关掉其它类型，否则排班数量会被默认值污染。
    """
    tasks = {t: {"enabled": False, "daily_cap": 0} for t in TASK_TYPES}
    tasks[task_type] = dict({"enabled": True}, **cfg)
    return {"enabled": True, "tasks": tasks}


def _plan(**tasks):
    raw = {"enabled": True,
           "tasks": dict({t: {"enabled": False, "daily_cap": 0}
                          for t in TASK_TYPES}, **tasks)}
    return normalize_plan(raw)


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_p_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.now = datetime(2026, 9, 20, 8, 0, 0)

    def tearDown(self):
        self._p.stop()

    def test_schedule_respects_cap_window_and_gap(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        day = planner.materialize_day(self.now, plan, {}, {})
        jobs = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]
        self.assertEqual(len(jobs), 3)
        times = [datetime.fromisoformat(j["planned_at"]) for j in jobs]
        start = datetime(2026, 9, 20, 8, 0)
        end = datetime(2026, 9, 20, 23, 30)
        for t in times:
            self.assertGreaterEqual(t, start)
            self.assertLessEqual(t, end)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual((b - a).total_seconds() / 60, 60)

    def test_schedule_is_deterministic_for_same_day_and_plan(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        a = planner.materialize_day(self.now, plan, {}, {})
        b = planner.materialize_day(self.now, plan, {}, {})
        self.assertEqual([j.get("planned_at") for j in a["schedule"]],
                         [j.get("planned_at") for j in b["schedule"]])

    def test_unimplemented_types_are_never_scheduled(self):
        plan = _plan(checkin={"enabled": True, "daily_cap": 5},
                     thank={"enabled": True, "daily_cap": 9},
                     reply_comment={"enabled": True, "daily_cap": 9})
        day = planner.materialize_day(self.now, plan, {}, {})
        self.assertFalse([j for j in day["schedule"]
                          if j["type"] in ("checkin", "thank", "reply_comment")])

    def test_resume_uses_done_counts_from_ledger(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        done = {"full_chain": 2}          # 台账里今天已写完 2 篇
        day = planner.materialize_day(self.now, plan, {}, done)
        pending = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]
        self.assertEqual(len(pending), 1)

    def test_catch_up_same_day_shifts_missed_job(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        later = datetime(2026, 9, 20, 12, 0, 0)      # 计划点已过
        day = planner.apply_catch_up(later, plan, day)
        job = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED][0]
        self.assertGreaterEqual(datetime.fromisoformat(job["planned_at"]), later)
        self.assertIn("顺延", job["note"])

    def test_catch_up_none_skips_missed_job(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1})
        plan["catch_up"] = "none"
        day = planner.materialize_day(self.now, plan, {}, {})
        day = planner.apply_catch_up(datetime(2026, 9, 20, 12, 0, 0), plan, day)
        self.assertEqual(day["schedule"][0]["status"], planner.STATUS_SKIPPED)
        self.assertIn("不补做", day["schedule"][0]["note"])

    def test_window_out_of_range_is_marked_skipped(self):
        # 窗口只有 1 小时、要排 3 篇（间隔 60 分钟）→ 排不下的部分标记跳过并给出原因
        plan = normalize_plan({"enabled": True, "window": {"start": "09:00", "end": "10:00"},
                               "tasks": {"full_chain": {"enabled": True, "daily_cap": 3}}})
        day = planner.materialize_day(self.now, plan, {}, {})
        skipped = [j for j in day["schedule"] if j["status"] == planner.STATUS_SKIPPED]
        self.assertTrue(skipped)
        self.assertIn("排不下", skipped[0]["note"])

    def test_summary_reports_progress(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        day = planner.materialize_day(self.now, plan, {}, {})
        s = planner.summarize(self.now, plan, day)
        self.assertEqual(s["per_type"]["full_chain"]["cap"], 3)
        self.assertEqual(s["plan_total"], 3)
        self.assertTrue(s["in_window"])           # 08:00 整点在窗口内
        self.assertIsNotNone(s["next_job"])


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_s_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        store.save_plan(_only("full_chain", daily_cap=2))
        self.now = datetime(2026, 9, 20, 9, 0, 0)
        self.calls = []
        self.result = {"ok": True, "units": 1, "status": planner.STATUS_DONE,
                       "message": "完成", "artifacts": []}

        def fake_exec(job, should_stop=None, progress=None):
            self.calls.append(job)
            if isinstance(self.result, Exception):
                raise self.result
            return dict(self.result)

        self.sched = AutomationScheduler(executor=fake_exec,
                                         now_fn=lambda: self.now)

    def tearDown(self):
        self._p.stop()

    def _planned_times(self):
        """当前排班里的待执行时间点（用真实排班驱动，避免把「错过顺延」混进来）。"""
        day = store.load_day("2026-09-20")
        plan = store.load_plan()
        day = planner.materialize_day(self.now, plan, day,
                                      store.done_counts("2026-09-20"))
        return [datetime.fromisoformat(j["planned_at"])
                for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]

    def _advance(self, minutes):
        self.now = self.now + timedelta(minutes=minutes)

    def test_tick_executes_due_jobs_and_records_ledger(self):
        times = self._planned_times()
        self.now = times[0]                    # 到点（不算错过）→ 立即执行
        self.sched._tick()
        self.assertEqual(len(self.calls), 1)
        self.now = times[1] + timedelta(minutes=1)
        self.sched._tick()
        self.assertEqual(len(self.calls), 2)
        self._advance(200)                     # 配额 2 已用完 → 不再派活
        self.sched._tick()
        self.assertEqual(len(self.calls), 2)
        done = store.done_counts("2026-09-20")
        self.assertEqual(done.get("full_chain"), 2)

    def test_busy_is_postponed_not_counted_as_failure(self):
        self.result = BrowserBusy("浏览器被占用：看板刷新")
        self.now = self._planned_times()[0]
        self.sched._tick()
        self.assertEqual(self.sched.status()["fails"].get("full_chain", 0), 0)
        rows = store.load_ledger("2026-09-20")
        self.assertFalse([r for r in rows if r.get("status") == planner.STATUS_FAILED])
        job = [j for j in store.load_day("2026-09-20")["schedule"]
               if j["status"] == planner.STATUS_PLANNED][0]
        self.assertIn("顺延", job["note"])

    def test_needs_human_pauses_automation(self):
        self.result = NeedHuman("运行前检测未通过：deepseek_login")
        self.now = self._planned_times()[0]
        self.sched._tick()
        st = self.sched.status()
        self.assertTrue(st["paused"])
        self.assertIn("deepseek_login", st["pause_reason"])
        self.assertTrue(any(n["level"] != "info" for n in st["notices"]))

    def test_circuit_breaker_disables_type_after_three_failures(self):
        store.save_plan(_only("full_chain", daily_cap=3))
        self.result = RuntimeError("模型无输出")
        # 失败会「当日补位」再试，最多补 2 次 → 第 3 次失败触发熔断；
        # 这里按 30 分钟步进反复 tick，直到熔断（或步数用尽）
        for _ in range(12):
            self.sched._tick()
            self._advance(30)
        self.assertFalse(store.load_plan()["tasks"]["full_chain"]["enabled"])
        self.assertTrue(any("连续失败" in n["text"] for n in self.sched.status()["notices"]))

    def test_stop_prevents_further_execution(self):
        self.sched.start()
        self.sched.stop()
        self.sched._tick()
        self.assertEqual(self.calls, [])

    def test_run_now_pulls_next_job_forward(self):
        self.now = datetime(2026, 9, 20, 8, 30, 0)
        r = self.sched.run_now("full_chain")
        self.assertTrue(r["ok"])
        self._advance(1)
        self.sched._tick()
        self.assertEqual(len(self.calls), 1)

    def test_run_now_dry_run_is_marked_on_the_job(self):
        """演练：作业上打 dry_run 标记（执行器据此只探测不点发布）。"""
        store.save_plan(_only("publish_drafts", daily_cap=1))
        r = self.sched.run_now("publish_drafts", dry_run=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["job"]["dry_run"])
        self.assertIn("演练", r["job"]["note"])

    def test_dry_run_works_even_when_publish_is_disabled(self):
        """演练的意义是「先验证再启用」：发布没启用/配额用完也要能跑一次。"""
        store.save_plan(_only("full_chain", daily_cap=1))    # 发布草稿未启用
        r = self.sched.run_now("publish_drafts", dry_run=True)
        self.assertTrue(r["ok"])
        job = r["job"]
        self.assertEqual(job["type"], "publish_drafts")
        self.assertEqual(job["units"], 0)                   # 不占配额
        self.assertTrue(job["dry_run"])
        self.assertIn(":rehearsal:", job["key"])

    def test_dry_run_rejects_types_without_rehearsal_support(self):
        store.save_plan(_only("full_chain", daily_cap=1))
        r = self.sched.run_now("checkin", dry_run=True)
        self.assertFalse(r["ok"])
        self.assertIn("不支持演练", r["message"])

    def test_paused_scheduler_does_not_execute(self):
        times = self._planned_times()
        self.now = times[0]
        self.sched.pause("测试暂停")
        self.sched._tick()
        self.assertEqual(self.calls, [])
        self.sched.resume()
        self.sched._tick()
        self.assertEqual(len(self.calls), 1)

    def test_missed_job_is_caught_up_within_window(self):
        # 程序 11:00 才启动，08:0x 的作业已明显错过 → same_day 顺延到当天剩余时段
        self._advance(120)
        self.sched._tick()
        job = [j for j in store.load_day("2026-09-20")["schedule"]
               if j["status"] == planner.STATUS_PLANNED][0]
        self.assertGreaterEqual(datetime.fromisoformat(job["planned_at"]), self.now)
        self.assertIn("顺延", job["note"])
        self.assertEqual(self.calls, [])       # 顺延后不立刻执行（留出缓冲）

    def test_resume_continues_from_ledger(self):
        # 今天已写完 2 篇（配额 2）→ 再次开始时不应再排活（用户要求「先看已做多少」）
        store.append_ledger({"day": "2026-09-20", "type": "full_chain",
                             "status": planner.STATUS_DONE, "units": 1})
        store.append_ledger({"day": "2026-09-20", "type": "full_chain",
                             "status": planner.STATUS_DONE, "units": 1})
        self.sched.start()
        self.sched._tick()
        self.assertEqual(self.calls, [])




class AutomationApiTest(unittest.TestCase):
    """API 边界回归（TestClient；计划里不启用任何任务，避免测试里真跑作业）。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from webui import server
        cls.client = TestClient(server.app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_api_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_status_payload_shape(self):
        d = self.client.get("/api/automation").json()
        for key in ("plan", "summary", "schedule", "ledger", "notices", "now"):
            self.assertIn(key, d)
        self.assertIn("per_type", d["summary"])

    def test_types_catalog_includes_reserved(self):
        d = self.client.get("/api/automation/types").json()
        ids = [t["id"] for t in d["types"]]
        self.assertIn("full_chain", ids)
        self.assertIn("publish_drafts", ids)
        todo = {t["id"]: t["implemented"] for t in d["types"]}
        self.assertTrue(todo["full_chain"])
        self.assertFalse(todo["checkin"])          # 预留接口，未实现

    def test_plan_saved_and_normalized(self):
        r = self.client.post("/api/automation/plan", json={"plan": {
            "enabled": False,
            "window": {"start": "09:00", "end": "22:00"},
            "tasks": {"full_chain": {"enabled": True, "daily_cap": 5}},
        }})
        plan = r.json()["plan"]
        self.assertEqual(plan["window"]["start"], "09:00")
        self.assertEqual(plan["tasks"]["full_chain"]["daily_cap"], 5)
        self.assertFalse(plan["tasks"]["checkin"]["enabled"])

    def test_start_stop_toggles_enabled(self):
        # 全部任务关闭 → start 后调度线程起来也不会执行任何作业
        self.client.post("/api/automation/plan", json={"plan": {
            "enabled": False,
            "tasks": {"full_chain": {"enabled": False, "daily_cap": 0},
                      "publish_drafts": {"enabled": False, "daily_cap": 0}}}})
        d = self.client.post("/api/automation/start").json()
        self.assertTrue(d["status"]["enabled"])
        d2 = self.client.post("/api/automation/stop").json()
        self.assertFalse(d2["status"]["enabled"])

    def test_run_now_without_pending_job_is_graceful(self):
        self.client.post("/api/automation/plan", json={"plan": {
            "tasks": {t: {"enabled": False, "daily_cap": 0}
                      for t in TASK_TYPES}}})
        d = self.client.post("/api/automation/run-now", json={}).json()
        self.assertFalse(d["ok"])
        self.assertIn("没有可提前执行", d["message"])

    def test_unknown_type_rejected(self):
        r = self.client.post("/api/automation/run-now", json={"type": "nope"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()