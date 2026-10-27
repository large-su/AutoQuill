"""Opt-in Edge test of the real evolution view and local assignment endpoint.

AQ_UI_TESTS=1 python -m unittest tests.test_evolution_ui
All browser requests are intercepted; no service, login, or public actions run.
"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from core import evolution


@unittest.skipUnless(os.environ.get("AQ_UI_TESTS") == "1", "opt-in Edge UI test")
class EvolutionUITest(unittest.TestCase):
    def test_release_time_window_views_horizons_and_assignment(self):
        from fastapi.testclient import TestClient
        from playwright.sync_api import sync_playwright
        from webui.server import app

        today = date.today()
        first_release = (today - timedelta(days=40)).isoformat()
        second_release = (today - timedelta(days=10)).isoformat()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            evolution.paths, "data", side_effect=lambda *parts: str(Path(temporary).joinpath(*parts))
        ), patch.object(
            evolution, "_history", return_value=[
                {"version": "0.1.0", "tag": "v0.1.0", "date": first_release,
                 "released_at": first_release + "T09:00:00+08:00",
                 "release_time_source": "synthetic-ui", "writing_code_id": "synthetic-writing-a",
                 "change_kind": "writing", "summary": "合成同日动作 A"},
                {"version": "0.1.1", "tag": "v0.1.1", "date": first_release,
                 "released_at": first_release + "T18:00:00+08:00",
                 "release_time_source": "synthetic-ui", "writing_code_id": "synthetic-writing-a",
                 "change_kind": "mixed", "summary": "合成同日动作 B（最后版本）"},
                {"version": "0.2.0", "tag": "v0.2.0", "date": second_release,
                 "released_at": second_release + "T00:00:00+08:00",
                 "release_time_source": "synthetic-ui", "writing_code_id": "synthetic-writing-b",
                 "change_kind": "writing", "summary": "合成版本窗口 B"},
            ]
        ):
            today = date.today()
            publish_date = (today - timedelta(days=30)).isoformat()
            rows = [{"aid": str(index), "url": "https://www.zhihu.com/answer/" + str(index),
                     "title": "隔离测试作品 " + str(index), "publish_date": publish_date,
                     "metrics": {"赞同": str(likes), "阅读": "1000", "收藏": str(index)}}
                    for index, likes in enumerate((30, 35, 40, 45, 50, 1000), 1)]
            evolution.ingest_snapshot(rows, datetime.now().astimezone().isoformat())
            refresh = evolution.start_refresh('automation', 'synthetic-ui')
            evolution.finish_refresh(refresh['id'], status='done', count=6,
                                     observed_at=datetime.now().astimezone().isoformat(),
                                     source_file='published_answers_ui.json')
            client = TestClient(app)
            scheme_id = client.get("/api/evolution").json()["nodes"][0]["id"]
            for aid in ("2", "3", "4", "5", "6"):
                self.assertEqual(client.post("/api/evolution/assign", json={"aid": aid, "node_id": scheme_id}).status_code, 200)
            errors = []
            actions = []
            fail = {'sync': False, 'feedback': False}

            def route_request(route):
                parsed = urlsplit(route.request.url)
                target = parsed.path + ("?" + parsed.query if parsed.query else "")
                if parsed.path == "/api/evolution/releases/refresh":
                    actions.append("release-refresh")
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": not fail['sync'], "message": "离线测试：保留缓存", "count": 2}))
                    return
                if parsed.path == "/api/dashboard/refresh":
                    actions.append("feedback-refresh")
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                    return
                if parsed.path == "/api/dashboard/refresh/status":
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"status": "error" if fail['feedback'] else "done", "error": "采集失败测试"}))
                    return
                if parsed.path.startswith("/api/evolution") or not parsed.path.startswith("/api/"):
                    response = client.request(route.request.method, target, content=route.request.post_data,
                                              headers={"Content-Type": "application/json"})
                    route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/plain"),
                                  body=response.content)
                    return
                response = {"state": "idle", "setup_needed": False, "edge_ok": True,
                            "llm_configured": True, "zhihu_logged_in": True, "version": "5.0.8",
                            "models": [], "authors": [], "sources": [], "stories": [], "lines": [],
                            "providers": [], "drivers": [], "config": {}, "mode": "web", "stage": "idle",
                            "has_update": False, "current": "5.0.8", "latest": "5.0.8"}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1400, "height": 960})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("**/*", route_request)
                    page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
                    page.select_option("#leftModeSel", "evolution")
                    page.wait_for_selector("#evoArticles tbody tr")
                    self.assertEqual(page.locator("#evoArticles tbody tr").count(), 6)
                    self.assertGreater(page.locator("#evoNodeList .evo-time-item").count(), 0)
                    self.assertIn("最小评价间隔为自然日", page.inner_text("#evoListHint"))
                    self.assertIn("从知乎更新反馈", page.inner_text("#pane-evolution"))
                    self.assertIn("同步版本时间", page.inner_text("#pane-evolution"))
                    self.assertEqual(page.get_attribute('[data-evo-view="release"]', 'aria-selected'), 'true')
                    self.assertEqual(page.locator("#evoNodeList .evo-time-item").count(), 2)
                    page.click('[data-evo-node="v0.1.1"]')
                    self.assertIn('42.5', page.inner_text('#evoDetail'))
                    self.assertIn('当天合并的 2 次发布', page.inner_text('#evoDetail'))
                    page.locator('#evoDetail summary', has_text='当天合并的 2 次发布').click()
                    self.assertIn('v0.1.0', page.inner_text('#evoDetail'))
                    self.assertIn('v0.1.1', page.inner_text('#evoDetail'))
                    self.assertIn('合成同日动作 A', page.inner_text('#evoDetail'))
                    self.assertIn('合成同日动作 B', page.inner_text('#evoDetail'))
                    page.check('#evoOnlySelected')
                    self.assertEqual(page.locator('#evoArticles tbody tr').count(), 6)
                    page.click('[data-evo-node="v0.2.0"]')
                    self.assertEqual(page.locator('#evoArticles tbody tr').count(), 0)
                    page.uncheck('#evoOnlySelected')
                    page.click("#evoReleaseRefresh")
                    page.wait_for_function("document.getElementById('evoReleaseRefresh').disabled === false")
                    page.click("#evoFeedbackRefresh")
                    page.wait_for_function("document.getElementById('evoFeedbackRefresh').disabled === false")
                    self.assertEqual(actions, ["release-refresh", "feedback-refresh"])
                    fail['sync'] = True
                    page.click('#evoReleaseRefresh')
                    page.wait_for_function("!document.getElementById('evoReleaseRefresh').disabled")
                    self.assertIn('离线测试', page.inner_text('#evoSyncStatus'))
                    fail['feedback'] = True
                    page.click('#evoFeedbackRefresh')
                    page.wait_for_function("!document.getElementById('evoFeedbackRefresh').disabled")
                    self.assertIn('采集失败测试', page.inner_text('#evoFeedbackStatus'))
                    self.assertEqual(page.locator('#evoArticles tbody tr').count(), 6)
                    page.select_option("#evoHorizon", "30")
                    page.wait_for_function("document.getElementById('evoWindowNote').textContent.includes('第 30～33')")
                    page.click('[data-evo-view="scheme"]')
                    page.select_option('select[data-evo-aid="1"]', scheme_id)
                    page.click('button[data-evo-save="1"]')
                    page.wait_for_function("[...document.querySelectorAll('#evoArticles select[data-evo-aid]')].every(x => x.value)")
                    page.locator('#evoNodeList button[data-evo-node="' + scheme_id + '"]').click()
                    self.assertIn("42.5", page.inner_text("#evoDetail"))
                    self.assertGreater(page.locator("#evoDistribution canvas").count(), 0)
                    self.assertIn("6 篇", page.inner_text("#evoFeedbackStatus"))
                    self.assertIn("账号关注数不可用", page.inner_text("#evoWarnings"))
                    self.assertIn("人工确认", page.inner_text("#evoArticles"))
                    page.select_option("#evoMetric", "collects")
                    page.wait_for_function("document.getElementById('evoDetail').textContent.includes('收藏中位数')")
                    self.assertIn("3.5", page.inner_text("#evoDetail"))
                    page.select_option("#evoMetric", "likes")
                    page.select_option("#evoHorizon", "7")
                    page.wait_for_function("document.getElementById('evoWindowNote').textContent.includes('第 7～10')")
                    self.assertIn("缺少窗口内有效观测", page.inner_text("#evoArticles"))
                    page.click('[data-evo-view="release"]')
                    release_count = len(client.get('/api/evolution').json()['releases'])
                    self.assertEqual(page.locator("#evoNodeList button").count(), release_count)
                    page.click("#evoNodeList button:first-child")
                    self.assertIn("按发布日推定", page.inner_text("#evoDetail"))
                    page.click('[data-evo-view="scheme"]')
                    page.select_option('select[data-evo-aid="1"]', "")
                    page.click('button[data-evo-save="1"]')
                    page.wait_for_function("[...document.querySelectorAll('#evoArticles select[data-evo-aid]')].filter(x => x.value).length === 5")
                    screenshot = os.environ.get("AQ_UI_SCREENSHOT")
                    if screenshot:
                        page.select_option("#evoHorizon", "30")
                        page.wait_for_function("document.getElementById('evoWindowNote').textContent.includes('第 30～33')")
                        page.click('[data-evo-view="release"]')
                        page.click('[data-evo-node="v0.1.1"]')
                        page.locator(".left-col").evaluate("element => element.scrollTop = 0")
                        page.locator("#evoStatus").evaluate("element => element.textContent = '界面测试示例：版本、文章与互动均为合成数据。 ' + element.textContent")
                        page.screenshot(path=screenshot, full_page=True)
                    page.set_viewport_size({"width": 620, "height": 900})
                    page.wait_for_timeout(100)
                    self.assertLessEqual(page.locator("#pane-evolution .evo-workspace").evaluate("e => e.scrollWidth - e.clientWidth"), 1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth - window.innerWidth'), 1)
                    if screenshot:
                        page.screenshot(path=str(Path(screenshot).with_name(Path(screenshot).stem + "-narrow.png")), full_page=True)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
