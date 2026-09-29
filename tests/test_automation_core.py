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

# 提前导入浏览器层：API 用例会 patch 它的 ZhihuBrowser 做「禁止开浏览器」护栏；
# 若等 setUp 里（DATA_ROOT 已指向临时目录）才首次导入，config 会因找不到
# llm_providers.json 直接报错（2026-09-27 踩过）。
import applications.zhihu_story.browser_adapter  # noqa: F401
import web_drivers.browser_pool  # noqa: F401

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

    def test_task_types_are_all_runnable(self):
        """契约层不登记「预留类型」：清单里有的就是能跑的。

        2026-09-24 曾砍到只剩「写故事 + 发布草稿」；2026-09-27 用户重新
        要求做打卡挑战（真机探针确认可行），于是加回 checkin（打卡互动
        巡检 + 写草稿顺带关注/赞同）。评论回复（reply_comment）等它落地
        再登记——这里只放已经在 executor 里有处理器的类型。
        """
        self.assertEqual(sorted(TASK_TYPES),
                         ["checkin", "full_chain", "publish_drafts",
                          "reply_comment"])
        from automation.executor import _HANDLERS
        self.assertEqual(sorted(_HANDLERS), sorted(TASK_TYPES),
                         "登记的每个类型都必须有执行器处理器")

    def test_removed_types_in_old_plan_are_ignored(self):
        """老 plan.json 里残留的已砍类型必须被安静丢弃（用户无需手工改文件）。

        可以确定不会再回来的：thank（感谢赞同/喜欢）、refresh_snapshot（M3 取消）。
        新增的打卡/回复这里显式关掉，隔离验证「已砍类型被丢弃」这件事本身。
        """
        legacy = {"enabled": True,
                  "tasks": {"thank": {"enabled": True, "daily_cap": 9},
                            "refresh_snapshot": {"enabled": True},
                            "checkin": {"enabled": False},
                            "reply_comment": {"enabled": False},
                            "full_chain": {"enabled": True, "daily_cap": 2}}}
        plan = normalize_plan(legacy)
        self.assertEqual(sorted(plan["tasks"]),
                         ["checkin", "full_chain", "publish_drafts",
                          "reply_comment"])
        self.assertEqual(plan["tasks"]["full_chain"]["daily_cap"], 2)
        day = planner.materialize_day(self.now, plan, {}, {})
        self.assertFalse([j for j in day["schedule"]
                          if j["type"] not in ("full_chain", "publish_drafts")])

    def test_new_tasks_enabled_by_default(self):
        """新装默认开启的任务：写 / 发 / 打卡。

        ★ 2026-09-29 稳定期调整：**reply_comment 移出默认**——它是唯一需要
          「共享浏览器 + 网页版大模型」双通道的一环，也是连续出错并被熔断的
          那一环（09-27/28 误判失败、09-29 跨线程崩溃还带崩了发布链路）。
          稳定期先不默认开；用户在面板上手动打开即可。
        """
        plan = normalize_plan({})
        for t in ("full_chain", "publish_drafts", "checkin"):
            self.assertTrue(plan["tasks"][t]["enabled"], "%s 应默认开启" % t)
        self.assertFalse(plan["tasks"]["reply_comment"]["enabled"],
                         "稳定期 reply_comment 不默认开启")

    def test_explicit_off_is_never_overridden(self):
        """默认开启**绝不覆盖**已有计划里的显式关闭（例如熔断自动停用后的状态）。"""
        plan = normalize_plan({"tasks": {"reply_comment": {"enabled": False},
                                          "checkin": {"enabled": False}}})
        self.assertFalse(plan["tasks"]["reply_comment"]["enabled"])
        self.assertFalse(plan["tasks"]["checkin"]["enabled"])

    def test_resume_uses_done_counts_from_ledger(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        done = {"full_chain": 2}          # 台账里今天已写完 2 篇
        day = planner.materialize_day(self.now, plan, {}, done)
        pending = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]
        self.assertEqual(len(pending), 1)

    def test_catch_up_same_day_shifts_missed_job(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        planned = datetime.fromisoformat(day["schedule"][0]["planned_at"])
        # 推进到「计划点 + 宽限期」之后：确实错过了，才该顺延
        later = planned + timedelta(minutes=planner.CATCH_UP_GRACE_MINUTES + 5)
        day = planner.apply_catch_up(later, plan, day)
        job = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED][0]
        self.assertGreaterEqual(datetime.fromisoformat(job["planned_at"]), later)
        self.assertIn("顺延", job["note"])

    def test_catch_up_none_skips_missed_job(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1})
        plan["catch_up"] = "none"
        day = planner.materialize_day(self.now, plan, {}, {})
        planned = datetime.fromisoformat(day["schedule"][0]["planned_at"])
        day = planner.apply_catch_up(
            planned + timedelta(minutes=planner.CATCH_UP_GRACE_MINUTES + 5),
            plan, day)
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

    def test_schedule_spreads_across_whole_window(self):
        """(a) 动作要「在时段内铺开」——每个动作落在自己那一段，不挤在一天前段。

        契约（2026-09-19 口径）：把时段等分 N 份、每份放一个动作，份内允许抖动。
        **不能断言「首尾跨度 ≥60% 时段」**——那不是算法保证的：实测不同随机种子下
        3 篇的跨度在 445~730 分钟之间浮动。旧断言只是碰巧命中一个好种子，
        2026-09-24 精简任务类型改变了排班种子后就翻了（改断言而不是改算法，
        因为「每份一个」才是要守住的性质）。
        """
        cap = 3
        span = 23 * 60 + 30 - 8 * 60                  # 08:00–23:30 = 930 分钟
        seg = span / float(cap)
        for off in range(6):                          # 多跑几个种子，防「碰巧通过」
            now = self.now + timedelta(days=off)
            plan = _plan(full_chain={"enabled": True, "daily_cap": cap})
            day = planner.materialize_day(now, plan, {}, {})
            times = sorted(datetime.fromisoformat(j["planned_at"])
                           for j in day["schedule"]
                           if j["status"] == planner.STATUS_PLANNED)
            shown = [t.strftime("%H:%M") for t in times]
            self.assertEqual(len(times), cap, shown)
            base = now.replace(hour=8, minute=0, second=0, microsecond=0)
            for i, t in enumerate(times):
                offset = (t - base).total_seconds() / 60
                self.assertGreaterEqual(offset, i * seg - 1, shown)
                self.assertLessEqual(offset, (i + 1) * seg + 1, shown)
            self.assertLessEqual(times[-1], now.replace(hour=23, minute=30))
            # 不能是「发一个等一小时」的随机游走：那会让最后一个点早早出现
            self.assertGreaterEqual(times[-1].hour, 18, shown)

    def test_gap_is_a_floor_not_a_step(self):
        """(b) 间隔是「下限」而不是「每次加一个随机间隔」：任何两个动作都 ≥ 间隔。"""
        for cap, gap in ((2, 60), (4, 90), (6, 60), (9, 45)):
            plan = _plan(full_chain={"enabled": True, "daily_cap": cap})
            plan["min_gap_minutes"] = gap
            day = planner.materialize_day(self.now, plan, {}, {})
            times = sorted(datetime.fromisoformat(j["planned_at"])
                           for j in day["schedule"]
                           if j["status"] == planner.STATUS_PLANNED)
            self.assertEqual(len(times), cap, (cap, gap))
            for a, b in zip(times, times[1:]):
                self.assertGreaterEqual((b - a).total_seconds() / 60, gap,
                                        (cap, gap, [t.strftime("%H:%M") for t in times]))

    def test_daily_cap_is_bounded_by_window_and_gap(self):
        """(c) 数学关系：上限 = floor(时段 / 最小间隔) + 1；设多了按上限排并说明原因。"""
        self.assertEqual(planner.feasible_count(930, 60), 16)    # 08:00–23:30 / 60 分
        self.assertEqual(planner.feasible_count(930, 120), 8)
        self.assertEqual(planner.feasible_count(120, 60), 3)
        self.assertEqual(planner.feasible_count(90, 60), 2)
        self.assertEqual(planner.feasible_count(60, 60), 2)
        self.assertEqual(planner.feasible_count(0, 60), 1)
        self.assertEqual(planner.feasible_count(30, 0), 30)      # 不限间隔 → 按分钟

        plan = _plan(full_chain={"enabled": True, "daily_cap": 20})   # 上限只有 16
        day = planner.materialize_day(self.now, plan, {}, {})
        planned = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]
        skipped = [j for j in day["schedule"] if j["status"] == planner.STATUS_SKIPPED]
        self.assertEqual(len(planned), 16)
        self.assertEqual(len(skipped), 1)                        # 只留一条说明，不刷屏
        self.assertIn("最多 16", skipped[0]["note"])
        times = sorted(datetime.fromisoformat(j["planned_at"]) for j in planned)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual((b - a).total_seconds() / 60, 60)
        self.assertLessEqual(times[-1], datetime(2026, 9, 20, 23, 30))

    def test_summary_exposes_daily_limit(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        day = planner.materialize_day(self.now, plan, {}, {})
        p = planner.summarize(self.now, plan, day)["per_type"]["full_chain"]
        self.assertEqual(p["window_minutes"], 930)
        self.assertEqual(p["min_gap_minutes"], 60)
        self.assertEqual(p["max_per_day"], 16)

    def test_resume_spreads_remaining_after_last_done(self):
        """续做：做完的部分不重排，剩下的在「剩余时段」里铺开且间隔 ≥ G。"""
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        done_jobs = [{"key": "k0", "type": "full_chain", "units": 1,
                      "status": planner.STATUS_DONE,
                      "planned_at": "2026-09-20T10:00:00", "note": ""}]
        day = planner.materialize_day(
            self.now, plan,
            {"date": "2026-09-20", "schedule": done_jobs},
            {"full_chain": 1})
        pend = sorted(datetime.fromisoformat(j["planned_at"])
                      for j in day["schedule"]
                      if j["status"] == planner.STATUS_PLANNED)
        self.assertEqual(len(pend), 2)
        self.assertGreaterEqual((pend[0] - datetime(2026, 9, 20, 10, 0)).total_seconds() / 60,
                                60)
        self.assertGreaterEqual((pend[1] - pend[0]).total_seconds() / 60, 60)
        self.assertLessEqual(pend[1], datetime(2026, 9, 20, 23, 30))

    def test_dense_schedule_never_leaves_the_window(self):
        """两张表都排满时，去碰撞只往后推——但不能把作业推出运行时段。"""
        plan = normalize_plan({"enabled": True, "min_gap_minutes": 30, "tasks": {
            "publish_drafts": {"enabled": True, "daily_cap": 16},
            "full_chain": {"enabled": True, "daily_cap": 16}}})
        day = planner.materialize_day(self.now, plan, {}, {})
        planned = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]
        self.assertTrue(planned)
        end = datetime(2026, 9, 20, 23, 30)
        for j in planned:
            self.assertLessEqual(datetime.fromisoformat(j["planned_at"]), end,
                                 j["planned_at"])

    def test_late_night_start_does_not_pile_up_past_jobs(self):
        """复现线上场景：22:40 才改计划/启动，不能再从早上铺出十几条「已经过去的点」。"""
        plan = normalize_plan({"enabled": True, "tasks": {
            "publish_drafts": {"enabled": True, "daily_cap": 6},
            "full_chain": {"enabled": True, "daily_cap": 8},
            # 隔离场景：新增的打卡/回复默认开启，这里显式关掉，
            # 只验证「深夜启动不会把发布/撰写铺出一堆过去的点」
            "checkin": {"enabled": False},
            "reply_comment": {"enabled": False}}})
        late = datetime(2026, 9, 19, 22, 40, 0)
        day = planner.materialize_day(late, plan, {}, {})
        planned = [j for j in day["schedule"] if j["status"] == planner.STATUS_PLANNED]
        past = [j for j in planned
                if datetime.fromisoformat(j["planned_at"]) < late]
        self.assertEqual(past, [], [j["planned_at"] for j in past])
        # 剩余 50 分钟 + 最小间隔 60 分钟 → 每类最多 1 项（数学上限，见 feasible_count）
        self.assertLessEqual(len(planned), 2)
        self.assertTrue(any(j["type"] == "publish_drafts" for j in planned))
        # 排不下的部分只留一条说明，不刷一屏跳过
        skipped = [j for j in day["schedule"] if j["status"] == planner.STATUS_SKIPPED]
        self.assertLessEqual(len(skipped), 2)
        self.assertTrue(any("排不下" in (j.get("note") or "") for j in skipped))

    def test_future_job_is_not_pushed_by_phantom_anchor(self):
        """还没到点的作业不该被「凭空 + 一个间隔」推走（线上把 22:50 的发布判死了）。"""
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        plan["min_gap_minutes"] = 60
        built = datetime(2026, 9, 20, 8, 0, 0)
        day = planner.materialize_day(built, plan, {}, {})
        times = [datetime.fromisoformat(j["planned_at"]) for j in day["schedule"]]
        # 从「第一个点之后 30 分钟」开始处理：第一项错过、后面的还在未来
        now = times[0] + timedelta(minutes=30)
        day = planner.apply_catch_up(now, plan, day)
        jobs = day["schedule"]
        self.assertTrue(all(j["status"] == planner.STATUS_PLANNED for j in jobs),
                        [(j["status"], j.get("note")) for j in jobs])
        planned = sorted(datetime.fromisoformat(j["planned_at"]) for j in jobs)
        self.assertEqual(planned[1:], times[1:])       # 未来作业保持原时间
        self.assertGreaterEqual((planned[1] - planned[0]).total_seconds() / 60, 60)

    def test_repeated_catch_up_eventually_executes(self):
        """顺延过的作业不能「一直被顺延」：tick 间隔被拖长时下一轮要直接执行。"""
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        first = datetime.fromisoformat(day["schedule"][0]["planned_at"])
        later = first + timedelta(minutes=30)          # 错过 → 顺延到 later+2
        day = planner.apply_catch_up(later, plan, day)
        job = day["schedule"][0]
        self.assertTrue(job.get("caught_up"))
        again = datetime.fromisoformat(job["planned_at"]) + timedelta(minutes=30)
        day = planner.apply_catch_up(again, plan, day)  # 又错过 → 直接现在执行
        job = day["schedule"][0]
        self.assertEqual(job["status"], planner.STATUS_PLANNED)
        self.assertLessEqual(datetime.fromisoformat(job["planned_at"]), again)

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
        """当前排班里的待执行时间点（用真实排班驱动，避免把「错过顺延」混进来）。

        落盘再返回：排班是「第一次生成时按当时的时间铺开」的（not_before=now），
        不落盘的话 tick 会重新铺一遍、时间点跟着变，测试就会去等一个不存在的点。
        """
        day = store.load_day("2026-09-20")
        plan = store.load_plan()
        day = planner.materialize_day(self.now, plan, day,
                                      store.done_counts("2026-09-20"))
        store.save_day("2026-09-20", day)
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
        # 排班是「铺满全天」的（3 篇约落在 10:0x / 15:2x / 21:4x），
        # 所以按 30 分钟步进要走完整个时段（留足补位时间），不能只走 6 小时
        for _ in range(40):
            self.sched._tick()
            self._advance(30)
        self.assertFalse(store.load_plan()["tasks"]["full_chain"]["enabled"])
        self.assertTrue(any("连续失败" in n["text"] for n in self.sched.status()["notices"]))

    def test_manual_run_works_outside_the_window(self):
        """「运行时段」只约束自动派活；用户手动点「立即执行」不受它限制。"""
        store.save_plan(_only("full_chain", daily_cap=2))
        self.now = datetime(2026, 9, 20, 3, 0, 0)        # 凌晨：时段外
        self.sched._tick()
        self.assertEqual(self.calls, [])                # 自动派活：不执行
        r = self.sched.run_now("full_chain")
        self.assertTrue(r["ok"])
        self.assertTrue(r["outside_window"])
        self._advance(1)
        self.sched._tick()
        self.assertEqual(len(self.calls), 1)            # 手动：照做

    def test_window_close_marks_leftovers_skipped_and_notifies(self):
        """时段结束：今天剩下的作业要明确收尾（标记跳过 + 通知一次），不能一直挂着。"""
        store.save_plan(_only("full_chain", daily_cap=2))
        self.now = datetime(2026, 9, 20, 23, 55, 0)      # 时段（08:00-23:30）已过
        self.sched._tick()
        self.assertEqual(self.calls, [])
        jobs = store.load_day("2026-09-20")["schedule"]
        self.assertTrue(jobs)
        self.assertTrue(all(j["status"] != planner.STATUS_PLANNED for j in jobs),
                        [j["status"] for j in jobs])
        # 作业被标跳过的原因可能是「错过时间点且当天时段已过」（catch_up 先处理），
        # 也可能是收尾时打的「运行时段已结束」——两种都算明确交代，不能有「无原因」的
        self.assertTrue(all((j.get("note") or "") for j in jobs),
                        [(j["status"], j.get("note")) for j in jobs])
        notices = self.sched.status()["notices"]
        self.assertTrue(any("运行时段已结束" in n["text"] for n in notices),
                        [n["text"] for n in notices])
        # 只通知一次：再 tick 不应再刷一条
        before = len(notices)
        self._advance(1)
        self.sched._tick()
        after = [n for n in self.sched.status()["notices"]
                 if "运行时段已结束" in n["text"]]
        self.assertEqual(len(after), 1, before)

    def test_next_day_rolls_over_without_restart(self):
        """跨零点：不用重新点开始，第二天照常排班（时段、配额自动进入新的一天）。"""
        store.save_plan(_only("full_chain", daily_cap=1))
        self.now = datetime(2026, 9, 21, 8, 5, 0)        # 第二天、时段内
        self.sched._tick()
        jobs = store.load_day("2026-09-21")["schedule"]   # 自动排出了新一天的时间轴
        self.assertTrue(jobs)
        self.assertEqual(len(self.calls), 0)              # 还没到点
        self.now = datetime.fromisoformat(jobs[0]["planned_at"])   # 到点
        self.sched._tick()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("2026-09-21", self.calls[0]["key"])

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
        # 程序在「第一篇计划点 + 宽限期」之后才启动 → same_day 顺延到当天剩余时段
        first = self._planned_times()[0]
        self.now = first + timedelta(
            minutes=planner.CATCH_UP_GRACE_MINUTES + 5)
        self.sched._tick()
        job = [j for j in store.load_day("2026-09-20")["schedule"]
               if j["status"] == planner.STATUS_PLANNED][0]
        self.assertGreaterEqual(datetime.fromisoformat(job["planned_at"]), self.now)
        self.assertIn("顺延", job["note"])
        self.assertEqual(self.calls, [])       # 顺延后不立刻执行（留出缓冲）

    def test_manual_run_works_while_automation_is_stopped(self):
        """停止状态下的「立即执行」也要真跑（线上只回一句通知，作业永远挂着）。

        2026-09-19 实录：未开启时点「立即执行（演练）」，接口回「已安排立即
        执行」，但 _tick 在函数开头就因 enabled=false 返回，作业一直停在
        「待执行」——用户以为在跑，其实什么也没发生（连点 4 次）。
        """
        times = self._planned_times()
        self.now = times[0]
        plan = store.load_plan()
        plan["enabled"] = False          # 用户点了「停止」，排班还在、作业未跑
        store.save_plan(plan)
        self.sched._tick()
        self.assertEqual(self.calls, [])          # 停止 = 自动派活不再执行
        r = self.sched.run_now("full_chain")
        self.assertTrue(r["ok"])
        self.assertTrue(self.sched._thread and self.sched._thread.is_alive(),
                        "未开启时也要有 tick 线程把这一次跑掉")
        self.assertFalse(store.load_plan()["enabled"],
                         "立即执行不能顺手把自动排班打开")
        self._advance(1)
        self.sched._tick()
        self.assertEqual(len(self.calls), 1)      # 手动：照做
        self.assertEqual(self.calls[0]["type"], "full_chain")
        self.sched.stop()                         # 收尾：别把 tick 线程留给下个用例

    def test_stopped_scheduler_still_ignores_automatic_jobs(self):
        """停止后即使有到点的自动作业也不执行（手动是唯一例外）。"""
        times = self._planned_times()
        self.now = times[0] + timedelta(minutes=1)
        plan = store.load_plan()
        plan["enabled"] = False
        store.save_plan(plan)
        for _ in range(3):
            self.sched._tick()
        self.assertEqual(self.calls, [])

    def test_ensure_running_resumes_only_when_plan_enabled(self):
        """重启 ≠ 停工：计划是「开启」就把 tick 线程补回来。

        plan.json 的 enabled 只代表「用户上次点过开始」，而 tick 线程是进程内
        的——重启后不恢复的话，界面显示「运行中」却什么都不执行。
        """
        plan = store.load_plan()
        plan["enabled"] = False
        store.save_plan(plan)
        self.assertFalse(self.sched.ensure_running())
        self.assertFalse(self.sched._thread and self.sched._thread.is_alive())
        plan["enabled"] = True
        store.save_plan(plan)
        self.assertTrue(self.sched.ensure_running())
        self.assertTrue(self.sched._thread and self.sched._thread.is_alive())
        self.sched.stop()

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
        # ★ 硬护栏（2026-09-27）：本类只测 API 边界，绝不允许真的拉起浏览器/起作业。
        #   起因：默认开关改成「四个任务全开」后，某些用例写的计划没有显式关掉新增任务，
        #   调度器就真的去跑了一班 → 开了真浏览器、占住 profile 租约没释放 →
        #   后面的 test_automation_publish 全部 BrowserBusy 报错（连环保育）。
        self._guards = [
            mock.patch("web_drivers.browser_pool.get_browser",
                       side_effect=RuntimeError("测试禁止启动浏览器")),
            mock.patch("applications.zhihu_story.browser_adapter.ZhihuBrowser",
                       side_effect=RuntimeError("测试禁止启动浏览器")),
            mock.patch("webui.run_manager.runner.start",
                       side_effect=RuntimeError("测试禁止启动作业")),
        ]
        for g in self._guards:
            g.start()

    def tearDown(self):
        for g in self._guards:
            g.stop()
        self._p.stop()

    def test_status_payload_shape(self):
        d = self.client.get("/api/automation").json()
        for key in ("plan", "summary", "schedule", "ledger", "notices", "now"):
            self.assertIn(key, d)
        self.assertIn("per_type", d["summary"])

    def test_types_catalog_lists_only_real_tasks(self):
        """目录里只有能跑的任务（UI 泳道/配置表据此渲染，不留空泳道）。"""
        d = self.client.get("/api/automation/types").json()
        ids = sorted(t["id"] for t in d["types"])
        self.assertEqual(ids, ["checkin", "full_chain", "publish_drafts",
                               "reply_comment"])
        self.assertTrue(all(t["implemented"] for t in d["types"]))

    def test_plan_saved_and_normalized(self):
        r = self.client.post("/api/automation/plan", json={"plan": {
            "enabled": False,
            "window": {"start": "09:00", "end": "22:00"},
            "tasks": {"full_chain": {"enabled": True, "daily_cap": 5}},
        }})
        plan = r.json()["plan"]
        self.assertEqual(plan["window"]["start"], "09:00")
        self.assertEqual(plan["tasks"]["full_chain"]["daily_cap"], 5)
        self.assertEqual(sorted(plan["tasks"]),
                         ["checkin", "full_chain", "publish_drafts",
                          "reply_comment"])

    def test_start_stop_toggles_enabled(self):
        # 全部任务关闭 → start 后调度线程起来也不会执行任何作业
        # 显式关掉全部四类（默认开启的新任务也要关，否则排班会真派活）
        self.client.post("/api/automation/plan", json={"plan": {
            "enabled": False,
            "tasks": {t: {"enabled": False, "daily_cap": 0}
                      for t in TASK_TYPES}}})
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


class CheckinScheduleTest(unittest.TestCase):
    """打卡任务（2026-09-27 新增）的排班与上下文契约。

    用户口径：打卡互动/巡检这类任务「时段内随机取一个时间点，一次把该做的
    都做完，不分散在多个时间点」；而「翻转兜底」只允许发生在当天最后一班
    写草稿上。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_ck_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.now = datetime(2026, 9, 27, 8, 0, 0)

    def tearDown(self):
        self._p.stop()

    def test_checkin_is_single_job_per_day(self):
        plan = _plan(checkin={"enabled": True, "daily_cap": 3})
        day = planner.materialize_day(self.now, plan, {}, {})
        jobs = [j for j in day["schedule"] if j["type"] == "checkin"]
        self.assertEqual(len(jobs), 1, "一天只排一次班，绝不摊成好几波")
        self.assertEqual(jobs[0]["units"], 3, "一个作业扛全部配额")
        self.assertEqual(jobs[0]["params"]["count"], 3)

    def test_checkin_lands_in_its_own_evening_window(self):
        plan = _plan(checkin={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        job = [j for j in day["schedule"] if j["type"] == "checkin"][0]
        when = datetime.fromisoformat(job["planned_at"])
        self.assertGreaterEqual(when.hour * 60 + when.minute, 20 * 60 + 30)
        self.assertLessEqual(when.hour * 60 + when.minute, 22 * 60 + 30)

    def test_single_mode_not_rescheduled_after_done(self):
        plan = _plan(checkin={"enabled": True, "daily_cap": 3})
        day = planner.materialize_day(self.now, plan, {}, {})
        # 第一个作业只完成了 1 项（配额 3）——按口径也不该再排第二班
        for j in day["schedule"]:
            if j["type"] == "checkin":
                j["status"] = planner.STATUS_DONE
                j["units"] = 1
        again = planner.materialize_day(self.now, plan, day, {"checkin": 1})
        jobs = [j for j in again["schedule"] if j["type"] == "checkin"]
        self.assertEqual(len(jobs), 1)

    def test_last_full_chain_job_is_stamped(self):
        # 打卡启用时才打「当天最后一班」标记（翻转兜底只在那一班做）
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3},
                     checkin={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        chains = [j for j in day["schedule"] if j["type"] == "full_chain"]
        stamped = [j for j in chains if (j.get("params") or {}).get("is_last_of_day")]
        self.assertEqual(len(stamped), 1, "翻转兜底只能有一个落点")
        latest = max(chains, key=lambda j: j["planned_at"])
        self.assertIs(stamped[0], latest, "必须是当天最后一班")
        self.assertIn("打卡可翻转", stamped[0]["note"])


    def test_reply_comment_single_job_and_dry_run_default(self):
        """评论回复：一天一班、一个作业扛全部配额；默认演练（不发出去）。"""
        plan = _plan(reply_comment={"enabled": True, "daily_cap": 2})
        self.assertTrue(plan["tasks"]["reply_comment"]["params"]["dry_run"],
                        "默认必须是演练：用户要求先看两天语气")
        day = planner.materialize_day(self.now, plan, {}, {})
        jobs = [j for j in day["schedule"] if j["type"] == "reply_comment"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["units"], 2)
        self.assertEqual(jobs[0]["params"]["count"], 2)
        when = datetime.fromisoformat(jobs[0]["planned_at"])
        self.assertGreaterEqual(when.hour, 10, "默认落在白天 10:00–22:00")
        self.assertLessEqual(when.hour, 22)


    def test_default_windows_keep_reply_before_check(self):
        """默认时段必须保证「先回复、后检查」——这是给用户看的顺序承诺。"""
        plan = _plan(reply_comment={"enabled": True, "daily_cap": 3},
                     checkin={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        rep = [j for j in day["schedule"] if j["type"] == "reply_comment"][0]
        chk = [j for j in day["schedule"] if j["type"] == "checkin"][0]
        self.assertLess(rep["planned_at"], chk["planned_at"],
                        "回复评论的落点必须早于打卡检查")
        self.assertLessEqual(rep["planned_at"][11:16], "20:00")
        self.assertGreaterEqual(chk["planned_at"][11:16], "20:30")

    def test_reply_comment_auto_mode_kept_when_user_turns_it_off(self):
        plan = _plan(reply_comment={"enabled": True, "daily_cap": 1,
                                    "params": {"dry_run": False}})
        self.assertFalse(plan["tasks"]["reply_comment"]["params"]["dry_run"])


    def test_last_of_day_stamp_only_when_checkin_enabled(self):
        """打卡没启用时，写草稿的作业上不该出现「打卡可翻转」这种噪音说明。"""
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3})
        day = planner.materialize_day(self.now, plan, {}, {})
        chains = [j for j in day["schedule"] if j["type"] == "full_chain"]
        self.assertFalse([j for j in chains
                          if (j.get("params") or {}).get("is_last_of_day")])
        self.assertFalse([j for j in chains if "打卡可翻转" in (j.get("note") or "")])

    def test_last_of_day_stamp_added_when_checkin_enabled(self):
        plan = _plan(full_chain={"enabled": True, "daily_cap": 3},
                     checkin={"enabled": True, "daily_cap": 1})
        day = planner.materialize_day(self.now, plan, {}, {})
        stamped = [j for j in day["schedule"]
                   if (j.get("params") or {}).get("is_last_of_day")]
        self.assertEqual(len(stamped), 1)
        self.assertEqual(stamped[0]["type"], "full_chain")

    def test_context_empty_when_checkin_disabled(self):
        from automation.executor import _checkin_context
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1})
        store.save_plan(plan)
        ctx = _checkin_context({"params": {"is_last_of_day": True}})
        self.assertEqual(ctx, {}, "没启用打卡 → 写草稿完全不碰账号互动")

    def test_context_when_enabled_and_pending(self):
        from automation.executor import _checkin_context
        plan = _plan(full_chain={"enabled": True, "daily_cap": 1},
                     checkin={"enabled": True, "daily_cap": 1})
        store.save_plan(plan)
        from core import checkin as ck
        state = ck.load_state()
        ck.update_tasks(state, {"follow": {"title": "关注 1 位知友", "done": False},
                                "vote": {"title": "送出 1 个赞同", "done": True}})
        ck.save_state(state)
        ctx = _checkin_context({"params": {"is_last_of_day": False}})
        self.assertEqual(ctx["follow"], "do")
        self.assertEqual(ctx["vote"], "done", "打卡页已完成 → 不再重复点赞")
        self.assertFalse(ctx["is_last"])
        ctx_last = _checkin_context({"params": {"is_last_of_day": True}})
        self.assertEqual(ctx_last["follow"], "toggle", "最后一班允许翻转兜底")


class SingleStageGateTest(unittest.TestCase):
    """单次任务的「阶段闸门」：回复评论（阶段 1）没结束，打卡检查（阶段 2）不许跑。

    用户 2026-09-27 口径：「回复肯定要提前完成，打卡最终在晚上检查是否完成，
    只有提前回复好了，检查时才能确保全部完成」。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_gate_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.now = datetime(2026, 9, 27, 21, 0, 0)
        self.plan = _plan(reply_comment={"enabled": True, "daily_cap": 1},
                          checkin={"enabled": True, "daily_cap": 1})

    def tearDown(self):
        self._p.stop()

    def _job(self, task_type, status, when):
        return {"key": "%s-%s" % (task_type, status), "type": task_type,
                "planned_at": when, "status": status, "units": 1, "params": {}}

    def _day(self, reply_status):
        sched = [self._job("reply_comment", reply_status, "2026-09-27T14:00:00"),
                 self._job("checkin", planner.STATUS_PLANNED, "2026-09-27T20:30:00")]
        return {"date": "2026-09-27", "schedule": sched}

    def _overlapping_plan(self):
        """把检查时段提前到与回复时段重叠（18:00 起），用于隔离验证闸门本身。"""
        return _plan(reply_comment={"enabled": True, "daily_cap": 1},
                     checkin={"enabled": True, "daily_cap": 1,
                              "window": {"start": "18:00", "end": "23:00"}})

    def test_stage_metadata_declared(self):
        self.assertEqual(planner.single_stage_of("reply_comment"), 1)
        self.assertEqual(planner.single_stage_of("checkin"), 2)
        self.assertIsNone(planner.single_stage_of("full_chain"))

    def test_pending_reply_blocks_check(self):
        # 回复还挂在时段内（10:00–20:00）没跑，检查到点了也不许抢跑
        plan = self._overlapping_plan()
        now = datetime(2026, 9, 27, 19, 30, 0)
        day = {"date": "2026-09-27", "schedule": [
            self._job("reply_comment", planner.STATUS_PLANNED, "2026-09-27T18:00:00"),
            self._job("checkin", planner.STATUS_PLANNED, "2026-09-27T19:00:00")]}
        due = planner.due_jobs(now, day, plan)
        self.assertEqual([j["type"] for j in due], ["reply_comment"],
                         "检查必须等回复先结束，绝不抢跑")

    def test_terminal_reply_opens_gate(self):
        for status in (planner.STATUS_DONE, planner.STATUS_SKIPPED,
                       planner.STATUS_FAILED):
            due = planner.due_jobs(self.now, self._day(status), self.plan)
            self.assertIn("checkin", [j["type"] for j in due],
                          "回复%s → 检查照跑（依赖的是「已结束」，不是「成功」）" % status)

    def test_running_reply_blocks_check(self):
        due = planner.due_jobs(self.now, self._day(planner.STATUS_RUNNING),
                               self.plan)
        self.assertEqual([j["type"] for j in due], [])

    def test_window_passed_opens_gate(self):
        # 安全阀：回复还挂着 planned，但它的时段（默认 10:00–20:00）已经过去，
        # 今天它不可能再跑了 → 不能把检查一路卡死（否则当天没人确认打卡）
        self.assertGreater(self.now.hour, 20)
        due = planner.due_jobs(self.now, self._day(planner.STATUS_PLANNED),
                               self.plan)
        self.assertIn("checkin", [j["type"] for j in due])

    def test_non_single_tasks_never_blocked(self):
        day = {"date": "2026-09-27", "schedule": [
            self._job("full_chain", planner.STATUS_PLANNED, "2026-09-27T14:00:00"),
            self._job("checkin", planner.STATUS_PLANNED, "2026-09-27T20:30:00")]}
        due = planner.due_jobs(self.now, day, self.plan)
        self.assertEqual(sorted(j["type"] for j in due),
                         ["checkin", "full_chain"])

    def test_due_jobs_without_plan_keeps_old_behaviour(self):
        # 不传 plan 时只按作业状态判断（老调用方/老单测不受影响）
        due = planner.due_jobs(self.now, self._day(planner.STATUS_PLANNED))
        self.assertEqual([j["type"] for j in due], ["reply_comment"])


class SingleAxisPayloadTest(unittest.TestCase):
    """单次任务轴 + 两张详情卡的数据组装（UI 只渲染，不再拼业务）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_auto_axis_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_axis_sorted_by_stage_and_has_details(self):
        from automation.scheduler import _single_axis_payload
        day = {"date": "2026-09-27", "schedule": [
            {"key": "c", "type": "checkin", "status": "planned",
             "planned_at": "2026-09-27T20:40:00", "units": 1},
            {"key": "r", "type": "reply_comment", "status": "done",
             "planned_at": "2026-09-27T14:00:00", "units": 2,
             "dry_run": True},
            {"key": "f", "type": "full_chain", "status": "planned",
             "planned_at": "2026-09-27T15:00:00", "units": 1}]}
        payload = _single_axis_payload("2026-09-27", day)
        self.assertEqual([x["type"] for x in payload["axis"]],
                         ["reply_comment", "checkin"],
                         "轴上只有单次任务，且按阶段排序（先回复后检查）")
        first = payload["axis"][0]
        self.assertEqual(first["stage"], 1)
        self.assertEqual(first["label"], "回复评论")
        self.assertTrue(first["dry_run"])
        self.assertIn("checkin", payload)
        self.assertIn("replies", payload)
        self.assertEqual(payload["checkin"]["pending"], ["follow", "vote", "comment"])

    def test_reply_runs_roundtrip(self):
        """回复运行统计归 core.checkin 管（automation 只编排，不做业务存储）。"""
        from core import checkin as _ck
        _ck.append_reply_run({"collected": 20, "candidates": 10,
                             "dropped": {"hostile": 3, "replied": 2},
                             "units": 1, "dry_run": True})
        rows = _ck.load_reply_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["collected"], 20)
        self.assertEqual(rows[0]["dropped"]["hostile"], 3)
        from datetime import date as _date
        self.assertEqual(len(_ck.load_reply_runs(_date.today().isoformat())), 1)
        self.assertEqual(len(_ck.load_reply_runs("1999-01-01")), 0)


class CheckinInteractWiringTest(unittest.TestCase):
    """互动落地模块的纯逻辑（不碰浏览器）：目标挑选与翻转动作编排。"""

    def test_pick_target_prefers_reference_answer(self):
        from applications.zhihu_story.checkin_task import pick_target
        items = [{"index": 0, "author": "甲", "has_follow": False, "has_vote": False},
                 {"index": 1, "author": "乙", "has_follow": True, "has_vote": True}]
        self.assertEqual(pick_target(items)["author"], "乙")
        items[0]["has_follow"] = True
        self.assertEqual(pick_target(items)["author"], "甲", "优先参考回答（第一条）")
        self.assertIsNone(pick_target([]))

    def test_apply_action_toggle_does_cancel_then_redo(self):
        from applications.zhihu_story.checkin_task import apply_action
        calls = []

        class FakeBrowser:
            def set_follow(self, want, index, **kw):
                calls.append(("follow", want, index))
                return {"ok": True, "detail": "ok"}

            def set_vote(self, want, index, **kw):
                calls.append(("vote", want, index))
                return {"ok": True, "detail": "ok"}

        r = apply_action(FakeBrowser(), "follow", 0, "toggle", pause=0)
        self.assertTrue(r["ok"])
        self.assertEqual(calls, [("follow", False, 0), ("follow", True, 0)])
        calls.clear()
        apply_action(FakeBrowser(), "vote", 2, "do", pause=0)
        self.assertEqual(calls, [("vote", True, 2)])

    def test_apply_action_toggle_aborts_when_cancel_fails(self):
        from applications.zhihu_story.checkin_task import apply_action
        calls = []

        class FakeBrowser:
            def set_follow(self, want, index, **kw):
                calls.append(want)
                return {"ok": False, "detail": "点了但状态没变"}

        r = apply_action(FakeBrowser(), "follow", 0, "toggle", pause=0)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [False], "取消没成功就不该硬点第二次")


class BrowserThreadingGuardTest(unittest.TestCase):
    """跨线程创建浏览器 = 整个作业崩（2026-09-29 事故的护栏）。

    事故：进度校核为了「顺便读两个只读接口」，在 full_chain 里调了
    get_browser()——而 full_chain 的浏览器是 TaskRunner 在另一个线程里建的。
    共享单例被创建在调度线程后，Playwright 的 sync API 跨线程使用直接抛
    「It looks like you are using Playwright Sync API inside the asyncio loop」，
    把 publish_drafts 与 full_chain 一起打挂（当天 2 次发布失败 + 自动化暂停）。
    """

    @staticmethod
    def _called_names(fn):
        """函数体里**真正被调用**的名字（走 AST，不看注释/docstring）。"""
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        return names

    def test_full_chain_never_syncs_or_creates_a_browser(self):
        """作业入口里的校核必须走 shared=True（只取不建）；full_chain 干脆不做。"""
        from automation import executor as _exec
        called = self._called_names(_exec._full_chain)
        self.assertNotIn("get_browser", called)
        self.assertNotIn("_sync_progress", called)

    def test_sync_progress_never_creates_shared_browser(self):
        """校核只能用 get_shared_browser（返回 None 就跳过），绝不 get_browser。"""
        from automation import executor as _exec
        called = self._called_names(_exec._sync_progress)
        self.assertIn("get_shared_browser", called)
        self.assertNotIn("get_browser", called)

    def test_get_shared_browser_returns_none_without_creating(self):
        """没有实例时必须返回 None，而不是顺手建一个（否则又回到事故里）。"""
        from web_drivers import browser_pool
        saved = browser_pool._shared_browser
        browser_pool._shared_browser = None
        try:
            with mock.patch.object(browser_pool, "create_browser") as fake:
                fake.side_effect = AssertionError("不该创建浏览器")
                self.assertIsNone(browser_pool.get_shared_browser())
        finally:
            browser_pool._shared_browser = saved


if __name__ == "__main__":
    unittest.main()
