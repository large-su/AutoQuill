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
    def test_graph_views_horizons_and_assignment(self):
        from fastapi.testclient import TestClient
        from playwright.sync_api import sync_playwright
        from webui.server import app

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            evolution.paths, "data", side_effect=lambda *parts: str(Path(temporary).joinpath(*parts))
        ):
            today = date.today()
            publish_date = (today - timedelta(days=30)).isoformat()
            rows = [{"aid": str(index), "url": "https://www.zhihu.com/answer/" + str(index),
                     "title": "隔离测试作品 " + str(index), "publish_date": publish_date,
                     "metrics": {"赞同": str(likes), "阅读": "1000"}}
                    for index, likes in enumerate((30, 35, 40, 45, 50, 1000), 1)]
            evolution.ingest_snapshot(rows, datetime.now().astimezone().isoformat())
            client = TestClient(app)
            scheme_id = client.get("/api/evolution").json()["nodes"][0]["id"]
            for aid in ("2", "3", "4", "5", "6"):
                self.assertEqual(client.post("/api/evolution/assign", json={"aid": aid, "node_id": scheme_id}).status_code, 200)
            errors = []

            def route_request(route):
                parsed = urlsplit(route.request.url)
                target = parsed.path + ("?" + parsed.query if parsed.query else "")
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
                    self.assertGreater(page.locator("#evoGraph canvas").count(), 0)
                    page.select_option('select[data-evo-aid="1"]', scheme_id)
                    page.click('button[data-evo-save="1"]')
                    page.wait_for_function("document.querySelectorAll('#evoArticles .evo-state.confirmed').length === 6")
                    self.assertIn("42.5", page.inner_text("#evoDetail"))
                    self.assertGreater(page.locator("#evoDistribution canvas").count(), 0)
                    page.select_option("#evoHorizon", "7")
                    page.wait_for_function("document.getElementById('evoWindowNote').textContent.includes('第 7～10')")
                    self.assertIn("缺少窗口内有效观测", page.inner_text("#evoArticles"))
                    page.click('[data-evo-view="release"]')
                    self.assertEqual(page.locator("#evoNodeList button").count(), 16)
                    self.assertEqual(page.evaluate("echarts.getInstanceByDom(document.getElementById('evoGraph')).getOption().series[0].links.length"), 15)
                    page.click("#evoNodeList button:first-child")
                    self.assertIn("属于", page.inner_text("#evoDetail"))
                    page.click('[data-evo-view="scheme"]')
                    page.select_option('select[data-evo-aid="1"]', "")
                    page.click('button[data-evo-save="1"]')
                    page.wait_for_function("document.querySelectorAll('#evoArticles .evo-state.confirmed').length === 5")
                    screenshot = os.environ.get("AQ_UI_SCREENSHOT")
                    if screenshot:
                        page.select_option("#evoHorizon", "30")
                        page.wait_for_function("document.getElementById('evoWindowNote').textContent.includes('第 30～33')")
                        page.click('[data-evo-view="release"]')
                        page.locator(".left-col").evaluate("element => element.scrollTop = 0")
                        page.locator("#evoStatus").evaluate("element => element.textContent = '界面测试示例：版本历史真实，文章与互动为合成数据。 ' + element.textContent")
                        page.screenshot(path=screenshot, full_page=True)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
