# -*- coding: utf-8 -*-
"""进度校核（core/progress.py）回归：解析 / 快照 / 计数合并 / 差异记录。

不碰浏览器：本模块只测纯逻辑（接口原语在 browser_progress，编排在 webui）。
用户口径 → 断言映射：
  - 「数据必须是真的」→ 计数以线上为准，台账只做审计（差额要被补回来）；
  - 「读不到就不许猜」→ 缺快照/跨天/坏文件一律退回台账数字；
  - 「不能为对账抹掉历史」→ 差异只追加 reconcile.jsonl，不动台账。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import paths, progress


def _published_payload(totals, rows):
    """按真机确认的两层结构造接口返回：
    {"paging": {"totals": N}, "data": [{"type": "answer",
      "data": {"id", "created_time", "question_id", "title"}}]}"""
    return {
        "paging": {"totals": totals, "is_end": True},
        "data": [{"type": "answer", "status": [],
                  "data": {"id": str(r[0]), "created_time": int(r[1]),
                           "question_id": str(r[2]), "title": r[3]}}
                 for r in rows],
    }


class ProgressParseTest(unittest.TestCase):
    """接口 JSON → 归一化结构（纯函数）。"""

    def test_parses_two_layer_rows(self):
        payload = _published_payload(2, [
            (2088023614834611140, 1790603623, 550834495, "有没有女主是笨蛋美人…"),
            (2088018397955094230, 1790602379, 540135195, "有没有又虐又甜…"),
        ])
        items = progress.parse_published(payload)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["qid"], "550834495")
        self.assertEqual(items[0]["created_time"], 1790603623)

    def test_parses_flat_rows_as_fallback(self):
        """内层 data 缺失时按外层当数据用（接口若改版也不至于全空）。"""
        payload = {"data": [{"id": "1", "created_time": 100, "question_id": "9"}]}
        items = progress.parse_published(payload)
        self.assertEqual([(i["aid"], i["qid"]) for i in items], [("1", "9")])

    def test_garbage_input_returns_empty(self):
        for bad in (None, [], "x", {"data": "x"}, {"data": [None, 1, []]}):
            self.assertEqual(progress.parse_published(bad), [])

    def test_published_total_reads_paging_totals(self):
        self.assertEqual(progress.published_total(_published_payload(10, [])), 10)
        self.assertIsNone(progress.published_total({}))
        self.assertIsNone(progress.published_total(None))

    def test_draft_count(self):
        self.assertEqual(progress.parse_draft_count({"count": 1}), 1)
        self.assertIsNone(progress.parse_draft_count({}))
        self.assertIsNone(progress.parse_draft_count("x"))


class ProgressSnapshotTest(unittest.TestCase):
    """快照组装与跨天失效。"""

    def test_build_uses_totals_and_drafts(self):
        snap = progress.build_snapshot(
            day="2026-09-28",
            published_payload=_published_payload(10, [(1, 100, 2, "t")]),
            draft_payload={"count": 1})
        self.assertEqual(snap.published_today, 10)     # 用线上自己说的 totals
        self.assertEqual(snap.drafts_pending, 1)
        self.assertTrue(snap.valid_for("2026-09-28"))

    def test_snapshot_expires_across_days(self):
        """跨天必须失效：绝不拿昨天的数字算今天。"""
        snap = progress.build_snapshot(day="2026-09-27",
                                       published_payload=_published_payload(9, []),
                                       draft_payload={"count": 3})
        self.assertFalse(snap.valid_for("2026-09-28"))

    def test_totals_missing_falls_back_to_row_count(self):
        payload = _published_payload(0, [(1, 100, 2, "t"), (3, 101, 4, "u")])
        payload.pop("paging")
        snap = progress.build_snapshot(day="2026-09-28",
                                       published_payload=payload,
                                       draft_payload={"count": 0})
        self.assertEqual(snap.published_today, 2)


class ProgressRoundTripTest(unittest.TestCase):
    """落盘 / 读取：坏文件与跨天都退回 None（调用方据此退回本地计数）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_progress_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_save_then_load(self):
        snap = progress.build_snapshot(day="2026-09-28",
                                       published_payload=_published_payload(10, []),
                                       draft_payload={"count": 1})
        self.assertTrue(progress.save(snap))
        got = progress.load("2026-09-28")
        self.assertIsNotNone(got)
        self.assertEqual(got.published_today, 10)
        self.assertEqual(got.drafts_pending, 1)

    def test_load_missing_or_corrupt_returns_none(self):
        self.assertIsNone(progress.load("2026-09-28"))          # 还没校核过
        path = progress.progress_file("2026-09-28")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(progress.load("2026-09-28"))          # 坏文件不许抛

    def test_load_other_day_returns_none(self):
        snap = progress.build_snapshot(day="2026-09-27",
                                       published_payload=_published_payload(9, []),
                                       draft_payload={"count": 0})
        progress.save(snap)
        self.assertIsNone(progress.load("2026-09-28"))


class MergeCountsTest(unittest.TestCase):
    """计数合并（唯一出口）：线上补回被误报漏掉的已发布量。"""

    def _snap(self, published, day="2026-09-28"):
        return progress.build_snapshot(
            day=day, published_payload=_published_payload(published, []),
            draft_payload={"count": 0})

    def test_site_overrides_undercounted_ledger(self):
        """今天的真实事故：台账 7 篇、线上 10 篇 → 必须取 10。"""
        merged = progress.merge_counts("publish_drafts", 7, self._snap(10),
                                       day="2026-09-28")
        self.assertEqual(merged, 10)

    def test_ledger_wins_when_site_lags(self):
        """线上接口滞后（读数更小）时不把已完成的算没。"""
        merged = progress.merge_counts("publish_drafts", 10, self._snap(8),
                                       day="2026-09-28")
        self.assertEqual(merged, 10)

    def test_no_snapshot_falls_back_to_ledger(self):
        self.assertEqual(progress.merge_counts("publish_drafts", 5, None), 5)

    def test_stale_snapshot_is_ignored(self):
        snap = self._snap(10, day="2026-09-27")
        self.assertEqual(
            progress.merge_counts("publish_drafts", 3, snap, day="2026-09-28"), 3)

    def test_other_task_types_keep_ledger_semantics(self):
        """草稿类任务（full_chain）线上没有等价物，仍按台账计。"""
        snap = self._snap(10)
        self.assertEqual(progress.merge_counts("full_chain", 9, snap,
                                               day="2026-09-28"), 9)


class ReconcileLogTest(unittest.TestCase):
    """差异层：只追加、不改台账；同日同偏差不重复记。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_reconcile_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_logs_delta_then_dedupes(self):
        snap = progress.build_snapshot(
            day="2026-09-28",
            published_payload=_published_payload(10, []),
            draft_payload={"count": 1})
        self.assertTrue(progress.log_reconcile(day="2026-09-28", ledger_units=7,
                                               snapshot=snap, note="发布前校核"))
        # 同一偏差再记一次 → 不重复（避免日志噪音）
        self.assertFalse(progress.log_reconcile(day="2026-09-28", ledger_units=7,
                                                snapshot=snap))
        rows = progress.load_reconcile("2026-09-28")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delta"], 3)          # 线上 10 - 台账 7
        self.assertEqual(rows[0]["site_published"], 10)

    def test_different_delta_is_logged_again(self):
        snap7 = progress.build_snapshot(
            day="2026-09-28", published_payload=_published_payload(7, []),
            draft_payload={"count": 2})
        snap9 = progress.build_snapshot(
            day="2026-09-28", published_payload=_published_payload(9, []),
            draft_payload={"count": 1})
        progress.log_reconcile(day="2026-09-28", ledger_units=7, snapshot=snap7)
        progress.log_reconcile(day="2026-09-28", ledger_units=7, snapshot=snap9)
        self.assertEqual(len(progress.load_reconcile("2026-09-28")), 2)

    def test_no_snapshot_logs_nothing(self):
        self.assertFalse(progress.log_reconcile(day="2026-09-28", ledger_units=1,
                                                snapshot=None))
        self.assertEqual(progress.load_reconcile("2026-09-28"), [])


class DoneCountsIntegrationTest(unittest.TestCase):
    """store.done_counts 是「已完成数量」的唯一出口：接上线上快照。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_done_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.addCleanup(self._p.stop)

    def _ledger(self, rows):
        from automation import store
        for r in rows:
            store.append_ledger(r)

    def test_ledger_seven_site_ten_yields_ten(self):
        """今天的真实数字：本地 7 篇失败里漏了 3 篇，线上 10 篇说了算。"""
        from automation import store
        self._ledger([{"day": "2026-09-28", "type": "publish_drafts",
                       "status": "done", "units": 1}] * 7
                     + [{"day": "2026-09-28", "type": "publish_drafts",
                         "status": "failed", "units": 0}] * 5)
        progress.save(progress.build_snapshot(
            day="2026-09-28", published_payload=_published_payload(10, []),
            draft_payload={"count": 1}))
        counts = store.done_counts("2026-09-28")
        self.assertEqual(counts.get("publish_drafts"), 10)

    def test_without_snapshot_uses_ledger(self):
        from automation import store
        self._ledger([{"day": "2026-09-28", "type": "publish_drafts",
                       "status": "done", "units": 3}])
        self.assertEqual(store.done_counts("2026-09-28").get("publish_drafts"), 3)


class QuotaTrimTest(unittest.TestCase):
    """配额被线上事实填满时，之前排下的待执行作业要退场。

    用户反馈原话（2026-09-28）：「后面还规划着有 3 篇，甚至有一篇都规划到
    时间轴之外去了」——根因就是台账少算，规划器以为还差 3 篇。
    """

    def setUp(self):
        from datetime import datetime
        self.now = datetime(2026, 9, 28, 22, 0, 0)

    def _plan(self, cap=10):
        return {"enabled": True, "tasks": {
            "publish_drafts": {"enabled": True, "daily_cap": cap},
            "full_chain": {"enabled": True, "daily_cap": 10}}}

    def _day(self, planned=3, done_jobs=7):
        from automation import planner
        schedule = []
        for i in range(done_jobs):
            schedule.append({"key": "done:%d" % i, "type": "publish_drafts",
                             "status": planner.STATUS_DONE, "units": 1,
                             "planned_at": "2026-09-28T0%d:00:00" % (i % 9)})
        for i in range(planned):
            schedule.append({"key": "plan:%d" % i, "type": "publish_drafts",
                             "status": planner.STATUS_PLANNED, "units": 1,
                             "planned_at": "2026-09-28T2%d:00:00" % i})
        # plan_hash 必须与当前计划一致：否则 materialize_day 会按新计划重排，
        # 把这里的测试夹具整个换掉（那测的就不是「超额退场」了）
        return {"date": "2026-09-28",
                "plan_hash": planner.plan_fingerprint(self._plan()),
                "schedule": schedule, "counters": {}, "rescheduled": 0,
                "notes": []}

    def test_surplus_planned_jobs_are_retired(self):
        """done == cap 时（今天就是），3 个待执行也必须全部退场。"""
        from automation import planner
        plan = self._plan(cap=10)
        day = self._day(planned=3)
        out = planner.materialize_day(self.now, plan, day,
                                      {"publish_drafts": 10})
        left = [j for j in out["schedule"]
                if j["status"] == planner.STATUS_PLANNED
                and j["type"] == "publish_drafts"]
        self.assertEqual(left, [])
        skipped = [j for j in out["schedule"]
                   if j["status"] == planner.STATUS_SKIPPED]
        self.assertEqual(len(skipped), 3)
        self.assertIn("配额已满", skipped[0]["note"])
        # 已完成的一条都不能被改
        self.assertEqual(len([j for j in out["schedule"]
                              if j["status"] == planner.STATUS_DONE]), 7)

    def test_partial_trim_when_over_delivered(self):
        """线上校核把计数补到超发（11/10）→ 待执行的一个也不留。"""
        from automation import planner
        out = planner.materialize_day(self.now, self._plan(cap=10),
                                      self._day(planned=3),
                                      {"publish_drafts": 11})
        left = [j for j in out["schedule"]
                if j["status"] == planner.STATUS_PLANNED
                and j["type"] == "publish_drafts"]
        self.assertEqual(left, [])

    def test_manual_and_dry_run_jobs_are_never_trimmed(self):
        from automation import planner
        day = self._day(planned=0)
        day["schedule"].append({"key": "manual:1", "type": "publish_drafts",
                                "status": planner.STATUS_PLANNED, "units": 1,
                                "manual": True, "planned_at": "2026-09-28T23:00:00"})
        out = planner.materialize_day(self.now, self._plan(cap=10), day,
                                      {"publish_drafts": 10})
        self.assertTrue(any(j["key"] == "manual:1" for j in out["schedule"]
                            if j["status"] == planner.STATUS_PLANNED))

    def test_no_trim_when_quota_not_reached(self):
        from automation import planner
        out = planner.materialize_day(self.now, self._plan(cap=10),
                                      self._day(planned=3),
                                      {"publish_drafts": 7})
        left = [j for j in out["schedule"]
                if j["status"] == planner.STATUS_PLANNED
                and j["type"] == "publish_drafts"]
        self.assertEqual(len(left), 3)


if __name__ == "__main__":
    unittest.main()
