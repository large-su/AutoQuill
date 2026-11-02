"""Regression: changing one quota must not reuse a completed job key."""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from automation import planner
from automation.model import normalize_plan
from automation import store
from automation.scheduler import AutomationScheduler
from core import paths


class QuotaReplanIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_quota_replan_"))
        self.data_root = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self.data_root.start()
        self.now = datetime(2026, 11, 2, 9, 10)

    def tearDown(self):
        self.data_root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self, *, publish_cap=9, full_cap=10):
        return normalize_plan({
            "enabled": True,
            "pause_when_user_busy": False,
            "window": {"start": "07:00", "end": "23:30"},
            "min_gap_minutes": 60,
            "tasks": {
                "publish_drafts": {"enabled": True, "daily_cap": publish_cap,
                                   "min_gap_minutes": 66},
                "full_chain": {"enabled": True, "daily_cap": full_cap,
                               "min_gap_minutes": 66},
            },
        })

    def test_replan_after_full_chain_quota_change_keeps_publish_job_keys_unique(self):
        original = self._plan()
        day = planner.materialize_day(self.now, original, {}, {})
        published = next(job for job in day["schedule"]
                         if job["type"] == "publish_drafts")
        published["status"] = planner.STATUS_DONE

        changed = normalize_plan({**original, "tasks": {
            **original["tasks"],
            "full_chain": {**original["tasks"]["full_chain"], "daily_cap": 2},
        }})
        replanned = planner.materialize_day(self.now, changed, day, {})
        keys = [job["key"] for job in replanned["schedule"]]

        self.assertEqual(len(keys), len(set(keys)),
                         "completed publish slot must not be reused after unrelated quota edit")

    def test_second_due_duplicate_key_does_not_publish_again_37_seconds_later(self):
        """The incident pattern: stale replan reused slot:0 and ran it again."""
        day = self.now.date().isoformat()
        plan = self._plan(publish_cap=9, full_cap=0)
        store.save_plan(plan)
        key = "auto:%s:publish_drafts:slot:0" % day
        first = {"key": key, "type": "publish_drafts", "status": planner.STATUS_PLANNED,
                 "planned_at": self.now.isoformat(), "units": 1, "params": {}, "note": ""}
        duplicate = {**first, "planned_at": (self.now + timedelta(seconds=37)).isoformat()}
        store.save_day(day, {"date": day, "plan_hash": planner.plan_fingerprint(plan),
                             "schedule": [first, duplicate], "counters": {}, "notes": []})
        calls = []
        scheduler = AutomationScheduler(
            executor=lambda job, **_kw: calls.append(dict(job)) or {
                "ok": True, "status": planner.STATUS_DONE, "units": 1, "message": "published"},
            now_fn=lambda: self.now)
        scheduler._tick()
        self.now += timedelta(seconds=37)
        scheduler._tick()
        self.assertEqual(len(calls), 1,
                         "same idempotency key must never issue a second public publish")

    def test_replan_uses_actual_completion_time_as_publish_gap_anchor(self):
        original = self._plan(publish_cap=1, full_cap=10)
        started = self.now.replace(hour=7, minute=30)
        day = planner.materialize_day(started, original, {}, {})
        job = next(j for j in day["schedule"] if j["type"] == "publish_drafts")
        job.update(status=planner.STATUS_DONE, planned_at=started.isoformat(),
                   finished_at=self.now.isoformat())
        changed = self._plan(publish_cap=2, full_cap=2)
        replanned = planner.materialize_day(self.now, changed, day, {})
        next_publish = min(datetime.fromisoformat(j["planned_at"])
                           for j in replanned["schedule"]
                           if j["type"] == "publish_drafts" and j["status"] == planner.STATUS_PLANNED)
        self.assertGreaterEqual(next_publish, self.now + timedelta(minutes=66))

    def test_stale_completed_public_job_cannot_be_reexecuted_directly(self):
        day = self.now.date().isoformat()
        plan = self._plan(publish_cap=1, full_cap=0)
        store.save_plan(plan)
        job = {"key": "auto:%s:publish_drafts:slot:0" % day,
               "type": "publish_drafts", "status": planner.STATUS_DONE,
               "planned_at": self.now.isoformat(), "units": 1, "params": {}, "note": ""}
        data = {"date": day, "plan_hash": planner.plan_fingerprint(plan),
                "schedule": [job], "counters": {"publish_drafts": 1}, "notes": []}
        store.save_day(day, data)
        calls = []
        scheduler = AutomationScheduler(
            executor=lambda *_a, **_kw: calls.append(True) or {"ok": True, "units": 1},
            now_fn=lambda: self.now)
        scheduler._run_job(dict(job), day, data, plan)
        self.assertEqual(calls, [], "completed public publish must be hard-blocked before executor")

    def test_new_publish_key_37_seconds_after_actual_success_is_delayed(self):
        day = self.now.date().isoformat()
        plan = self._plan(publish_cap=2, full_cap=0)
        store.save_plan(plan)
        earlier = {"key": "auto:%s:publish_drafts:slot:0" % day,
                   "type": "publish_drafts", "status": planner.STATUS_DONE,
                   "planned_at": (self.now - timedelta(hours=1)).isoformat(),
                   "finished_at": self.now.isoformat(), "units": 1, "params": {}, "note": ""}
        target = {"key": "auto:%s:publish_drafts:slot:1" % day,
                  "type": "publish_drafts", "status": planner.STATUS_PLANNED,
                  "planned_at": (self.now + timedelta(seconds=37)).isoformat(),
                  "units": 1, "params": {}, "note": ""}
        data = {"date": day, "plan_hash": planner.plan_fingerprint(plan),
                "schedule": [earlier, target], "counters": {"publish_drafts": 1}, "notes": []}
        store.save_day(day, data)
        calls = []
        scheduler = AutomationScheduler(
            executor=lambda *_a, **_kw: calls.append(True) or {"ok": True, "units": 1},
            now_fn=lambda: self.now + timedelta(seconds=37))
        scheduler._tick()
        self.assertEqual(calls, [])
        persisted = next(j for j in store.load_day(day)["schedule"] if j["key"] == target["key"])
        self.assertEqual(persisted["status"], planner.STATUS_PLANNED)
        self.assertGreaterEqual(datetime.fromisoformat(persisted["planned_at"]),
                                self.now + timedelta(minutes=66))

    def test_restart_recovers_success_and_quarantines_old_duplicate_slot(self):
        day = self.now.date().isoformat()
        key = "auto:%s:publish_drafts:slot:0" % day
        store.append_ledger({"day": day, "key": key, "type": "publish_drafts",
                             "status": "done", "units": 1,
                             "finished_at": self.now.isoformat(), "message": "published"})
        store.save_day(day, {"date": day, "schedule": [
            {"key": key, "type": "publish_drafts", "status": planner.STATUS_PLANNED,
             "planned_at": self.now.isoformat()},
            {"key": key, "type": "publish_drafts", "status": planner.STATUS_SKIPPED,
             "planned_at": (self.now + timedelta(minutes=1)).isoformat()},
        ], "counters": {}, "notes": []})
        recovered = store.load_day(day)
        self.assertEqual(len(recovered["schedule"]), 1)
        self.assertEqual(recovered["schedule"][0]["status"], planner.STATUS_DONE)
        self.assertEqual(len(recovered.get("recovered_duplicates") or []), 1)

    def test_manual_publish_respects_full_quota_but_rehearsal_still_has_no_side_effect(self):
        day = self.now.date().isoformat()
        plan = self._plan(publish_cap=1, full_cap=0)
        store.save_plan(plan)
        store.append_ledger({"day": day, "key": "already-published", "type": "publish_drafts",
                             "status": "done", "units": 1, "finished_at": self.now.isoformat()})
        scheduler = AutomationScheduler(executor=lambda *_a, **_kw: self.fail("must not execute"),
                                        now_fn=lambda: self.now)
        blocked = scheduler.run_now("publish_drafts")
        rehearsal = scheduler.run_now("publish_drafts", dry_run=True)
        self.assertFalse(blocked["ok"])
        self.assertTrue(rehearsal["ok"])
        self.assertTrue(rehearsal["job"]["dry_run"])

    def test_dry_run_receipt_does_not_consume_real_publish_quota(self):
        day = self.now.date().isoformat()
        store.append_ledger({"day": day, "key": "rehearsal", "type": "publish_drafts",
                             "status": "done", "units": 1, "dry_run": True,
                             "finished_at": self.now.isoformat()})
        self.assertEqual(store.done_counts(day).get("publish_drafts", 0), 0)

    def test_manual_dispatch_rechecks_quota_and_interval_after_queueing(self):
        day = self.now.date().isoformat()
        store.append_ledger({"day": day, "key": "earlier", "type": "publish_drafts",
                             "status": "done", "units": 1, "finished_at": self.now.isoformat()})
        for cap in (1, 2):
            with self.subTest(cap=cap):
                plan = self._plan(publish_cap=cap, full_cap=0)
                store.save_plan(plan)
                job = {"key": "manual-new", "type": "publish_drafts", "status": "planned",
                       "planned_at": self.now.isoformat(), "manual": True, "units": 1}
                data = {"date": day, "schedule": [job], "counters": {}, "notes": []}
                store.save_day(day, data)
                calls = []
                scheduler = AutomationScheduler(
                    executor=lambda *_a, **_kw: calls.append(True),
                    now_fn=lambda: self.now + timedelta(seconds=37))
                result = scheduler._run_job(dict(job), day, data, plan)
                self.assertEqual(calls, [])
                self.assertEqual(result["status"], "skipped" if cap == 1 else "planned")

    def test_stale_job_postponed_by_guard_cannot_execute_at_its_old_time(self):
        day = self.now.date().isoformat()
        plan = self._plan(publish_cap=2, full_cap=0)
        store.save_plan(plan)
        job = {"key": "auto:%s:publish_drafts:slot:1" % day,
               "type": "publish_drafts", "status": planner.STATUS_PLANNED,
               "planned_at": (self.now + timedelta(minutes=66)).isoformat(),
               "units": 1, "params": {}, "note": "距上次实际发布不足 66 分钟"}
        data = {"date": day, "plan_hash": planner.plan_fingerprint(plan),
                "schedule": [job], "counters": {"publish_drafts": 1}, "notes": []}
        store.save_day(day, data)
        calls = []
        scheduler = AutomationScheduler(
            executor=lambda *_a, **_kw: calls.append(True) or {"ok": True, "units": 1},
            now_fn=lambda: self.now)
        scheduler._run_job(dict(job), day, data, plan)
        self.assertEqual(calls, [], "stale dispatch snapshot must respect a later delay")

    def test_unpersisted_running_claim_pauses_without_executing(self):
        day = self.now.date().isoformat()
        plan = self._plan(publish_cap=1, full_cap=0)
        store.save_plan(plan)
        job = {"key": "auto:%s:publish_drafts:slot:0" % day,
               "type": "publish_drafts", "status": planner.STATUS_PLANNED,
               "planned_at": self.now.isoformat(), "units": 1, "params": {}, "note": ""}
        data = {"date": day, "plan_hash": planner.plan_fingerprint(plan),
                "schedule": [job], "counters": {}, "notes": []}
        store.save_day(day, data)
        calls = []
        scheduler = AutomationScheduler(
            executor=lambda *_a, **_kw: calls.append(True) or {"ok": True, "units": 1},
            now_fn=lambda: self.now)
        with mock.patch.object(scheduler, "_save_day_if_changed"):
            result = scheduler._run_job(dict(job), day, data, plan)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], planner.STATUS_NEEDS_HUMAN)
        self.assertTrue(scheduler.status()["paused"])


if __name__ == "__main__":
    unittest.main()
