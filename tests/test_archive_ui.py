"""隔离验证：本地发表快照状态与重建按钮不触发知乎采集。"""
import json
import os
import unittest
from urllib.parse import urlsplit

@unittest.skipUnless(os.environ.get("AQ_UI_TESTS") == "1", "opt-in Edge UI test")
class ArchiveUITest(unittest.TestCase):
    def test_archive_status_rebuild_and_failure_are_local(self):
        from fastapi.testclient import TestClient
        from webui.server import app
        from playwright.sync_api import sync_playwright

        actions = []
        failed = {"value": False}
        archive = {
            "snapshot_count": 12, "day_count": 8, "first_date": "2026-10-01",
            "last_success_date": "2026-10-27", "consecutive_days": 3,
            "missing_dates": ["2026-10-23"], "today_status": "recorded",
            "invalid_count": 1, "legacy_count": 2, "state_import_count": 686,
            "warnings": ["隔离测试校验提示"],
            "refreshes": {
                "latest": {"status": "error", "started_at": "2026-10-27T23:00:00+08:00", "message": "上次失败测试"},
                "last_success": {"status": "done", "finished_at": "2026-10-27T22:58:00+08:00", "count": 686},
            },
        }
        feedback = {"current_version": "5.7.1", "metric": "likes", "horizon": 7,
                    "releases": [{"tag": "v5.7.1", "evaluation_date": "2026-10-27", "release_count": 1,
                                  "time_article_ids": [], "time_metrics": {}}],
                    "nodes": [], "articles": [], "quality": {}, "warnings": [],
                    "refreshes": {"latest": archive["refreshes"]["latest"], "history": []},
                    "archive": archive}

        def route_request(route):
            parsed = urlsplit(route.request.url)
            path = parsed.path
            if path == "/api/evolution/archive":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(archive))
                return
            if path == "/api/evolution/rebuild":
                actions.append("local-rebuild")
                if failed["value"]:
                    route.fulfill(status=500, content_type="application/json", body=json.dumps({"ok": False, "message": "本地重建失败测试"}))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": "本地更新完成", "archive": archive}))
                return
            if path == "/api/dashboard/refresh":
                actions.append("zhihu-refresh")
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            if path == "/api/evolution/feedback":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(feedback))
                return
            if path.startswith("/api/"):
                if path == "/api/automation":
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"enabled": True, "paused": False, "running": None, "summary": {}, "tasks": [], "history": [], "site_progress": {}}))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"state": "idle", "setup_needed": False, "edge_ok": True, "version": "5.7.1", "current": "5.7.1", "latest": "5.7.1"}))
                return
            response = TestClient(app).request(route.request.method, path, content=route.request.post_data)
            route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/html"), body=response.content)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page(viewport={"width": 620, "height": 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route("**/*", route_request)
                page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
                page.select_option("#leftModeSel", "automation")
                page.wait_for_function("document.getElementById('autoArchiveStatus').textContent.includes('今日已记录')")
                page.wait_for_function("document.getElementById('autoArchiveWarnings').textContent.includes('上次失败测试')")
                self.assertIn("上次失败测试", page.locator("#autoArchiveWarnings").text_content())
                self.assertIn("仅知日期，完整性未知", page.locator("#autoArchiveWarnings").text_content())
                page.click("#autoArchiveRebuild")
                page.wait_for_function("document.getElementById('autoArchiveStatus').textContent.includes('本地快照更新完成')")
                self.assertEqual(actions, ["local-rebuild"])
                failed["value"] = True
                page.click("#autoArchiveRebuild")
                page.wait_for_function("document.getElementById('autoArchiveStatus').textContent.includes('本地快照更新未完成')")
                page.select_option("#leftModeSel", "evolution")
                page.wait_for_function("!document.getElementById('evoArchive').hidden")
                self.assertIn("旧台账归档", page.inner_text("#evoArchiveSummary"))
                self.assertIn("最近失败", page.locator("#evoArchiveWarnings").text_content())
                page.click("#evoRefresh")
                page.wait_for_function("document.getElementById('evoStatus').textContent.includes('本地快照更新未完成')")
                self.assertEqual(actions, ["local-rebuild", "local-rebuild", "local-rebuild"])
                failed['value'] = False
                page.click('#evoRefresh')
                page.wait_for_function("document.getElementById('evoStatus').textContent.includes('本地快照更新完成')")
                self.assertEqual(len(actions), 4)
                self.assertNotIn("zhihu-refresh", actions)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), page.evaluate("document.documentElement.clientWidth") + 2)
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
