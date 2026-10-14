"""Opt-in Edge smoke test for feedback automation UI and dashboard history."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit


@unittest.skipUnless(os.environ.get("AQ_UI_TESTS") == "1", "opt-in Edge UI test")
class FeedbackUITest(unittest.TestCase):
    def test_fixed_feedback_time_and_dashboard_refresh_nodes(self):
        from fastapi.testclient import TestClient
        from playwright.sync_api import sync_playwright
        from webui.server import app

        with tempfile.TemporaryDirectory() as temporary, patch(
            "core.paths.DATA_ROOT", temporary
        ):
            plan = {
                "enabled": False,
                "tasks": {
                    "full_chain": {"enabled": False, "daily_cap": 4,
                                    "params": {"mode": "clean"}},
                    "publish_drafts": {"enabled": False, "daily_cap": 2},
                    "reply_comment": {"enabled": False, "daily_cap": 3},
                    "checkin": {"enabled": False},
                    "feedback_refresh": {"enabled": False, "at": "23:00",
                                          "params": {"keep": "user"}},
                },
            }
            feedback_meta = {
                "history": [
                    {"id": "refresh:done", "source": "automation", "status": "done",
                     "count": 3, "observed_at": "2026-10-14T23:00:00+08:00"},
                    {"id": "refresh:error", "source": "manual", "status": "error",
                     "message": "页面未登录", "finished_at": "2026-10-14T22:00:00+08:00"},
                ],
                "latest": None, "last_success": None, "total": 2, "warnings": [],
            }
            auto_state = {
                "enabled": False, "paused": False, "running": None,
                "plan": plan, "now": "2026-10-14T20:00:00+08:00",
                "summary": {"in_window": False, "window_label": "08:00–23:30",
                            "done_total": 0, "plan_total": 0, "rescheduled": 0,
                            "per_type": {
                                "full_chain": {"label": "全链路撰写", "lane": 1,
                                                "implemented": True, "enabled": False,
                                                "cap": 4, "done": 0, "unit": "篇",
                                                "job_mode": "normal"},
                                "feedback_refresh": {"label": "反馈刷新", "lane": 4,
                                                     "implemented": True, "enabled": False,
                                                     "cap": 1, "done": 0, "unit": "次",
                                                     "job_mode": "fixed_daily", "at": "23:00"},
                            }},
                "schedule": [], "single": {"axis": []}, "ledger": [], "notices": [],
            }
            dashboard = {
                "rows": [], "total": 0, "all_total": 0, "stats": {},
                "generated_at": "2026-10-14T23:00:00+08:00",
                "source_file": "published_answers_2026-10-14.json",
                "refresh": {"status": "idle"}, "feedback_refresh": feedback_meta,
            }
            client = TestClient(app)
            saved_plans = []
            errors = []

            def route_request(route):
                parsed = urlsplit(route.request.url)
                path = parsed.path
                if path == "/api/automation":
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(auto_state))
                    return
                if path == "/api/automation/plan" and route.request.method == "POST":
                    body = json.loads(route.request.post_data or "{}")
                    saved_plans.append(body.get("plan") or {})
                    auto_state["plan"] = body.get("plan") or auto_state["plan"]
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "status": auto_state}))
                    return
                if path == "/api/dashboard":
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(dashboard))
                    return
                if path.startswith("/api/"):
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "state": "idle", "status": "idle"}))
                    return
                # Static files are served by the local TestClient; no external request is used.
                response = client.request(route.request.method, path,
                                          content=route.request.post_data,
                                          headers={"Content-Type": "application/json"})
                route.fulfill(status=response.status_code,
                              content_type=response.headers.get("content-type", "text/plain"),
                              body=response.content)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1400, "height": 960})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("**/*", route_request)
                    page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
                    page.select_option("#leftModeSel", "automation")
                    page.wait_for_selector('.auto-task[data-type="feedback_refresh"]')
                    card = page.locator('.auto-task[data-type="feedback_refresh"]')
                    self.assertEqual(card.locator('[data-role="cap"]').count(), 0)
                    self.assertEqual(card.locator('[data-role="gap"]').count(), 0)
                    self.assertEqual(card.locator('[data-role="at"]').input_value(), "23:00")
                    full_chain = page.locator('.auto-task[data-type="full_chain"]')
                    page.locator('.auto-task[data-type="feedback_refresh"] [data-role="at"]').fill("22:45")
                    page.wait_for_timeout(700)
                    self.assertTrue(saved_plans)
                    saved = saved_plans[-1]
                    self.assertEqual(saved["tasks"]["feedback_refresh"]["at"], "22:45")
                    self.assertFalse(saved["enabled"])
                    self.assertEqual(saved["tasks"]["full_chain"]["daily_cap"], 4)
                    self.assertEqual(saved["tasks"]["full_chain"]["params"]["mode"], "clean")
                    page.locator('.auto-task[data-type="feedback_refresh"] [data-role="at"]').fill("")
                    page.wait_for_timeout(700)
                    self.assertEqual(saved_plans[-1]["tasks"]["feedback_refresh"]["at"], "22:45")

                    page.select_option("#leftModeSel", "dashboard")
                    page.wait_for_selector("#dashSrcLine")
                    page.wait_for_function("document.getElementById('dashSrcLine').innerText.includes('最近反馈更新节点')")
                    page.locator("#dashSrcLine details summary").click()
                    source = page.inner_text("#dashSrcLine")
                    self.assertIn("完成", source)
                    self.assertIn("失败", source)
                    self.assertIn("2026-10-14 23:00", source)
                    self.assertEqual(errors, [])
                    screenshot = os.environ.get("AQ_UI_SCREENSHOT")
                    if screenshot:
                        page.screenshot(path=screenshot, full_page=True)
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
