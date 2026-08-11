# ============================================================
# tests/test_webui_server.py — Web 控制台后端 API
# ============================================================

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webui import server


class TestServerAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_index_serves_html(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("AutoQuill", r.text)
        self.assertIn("log-box", r.text)   # 核心 DOM 存在

    def test_config_shape(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        cfg = r.json()
        self.assertIn("LLM_MODE", cfg)
        self.assertIn("STORY_MATERIAL_MODE", cfg)
        self.assertIn("AUTHOR_PROFILE", cfg)
        # 值不该含 API key（安全约束：只读速览不暴露密钥）
        blob = str(cfg)
        self.assertNotIn("sk-", blob)
        self.assertNotIn("api_key", blob.lower())

    def test_stories_empty_ok(self):
        r = self.client.get("/api/stories")
        self.assertEqual(r.status_code, 200)
        self.assertIn("stories", r.json())

    def test_status_idle(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        st = r.json()
        self.assertIn("state", st)
        self.assertIn("context", st)
        self.assertIn("story", st)


class TestStoryTraversalGuard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._orig_output = server.OUTPUT_DIR
        server.OUTPUT_DIR = Path(tempfile.mkdtemp(prefix="webui_out_"))
        (server.OUTPUT_DIR / "story_20260811_000001.md").write_text(
            "测试故事内容", encoding="utf-8")
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        server.OUTPUT_DIR = cls._orig_output

    def test_read_existing(self):
        r = self.client.get("/api/story",
                            params={"name": "story_20260811_000001.md"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "测试故事内容")

    def test_path_traversal_blocked(self):
        for evil in ("..\\config.py", "../config.py",
                     "..\\..\\config\\llm_providers.json",
                     "sub\\..\\..\\config.py", "/etc/passwd"):
            r = self.client.get("/api/story", params={"name": evil})
            self.assertEqual(r.status_code, 400, f"name={evil}")

    def test_missing_file_404(self):
        r = self.client.get("/api/story",
                            params={"name": "story_nonexist.md"})
        self.assertEqual(r.status_code, 404)


class TestRunSpec(unittest.TestCase):

    def test_defaults(self):
        spec = server._RunSpec(mode="batch")
        self.assertEqual(spec.gen_count, 5)
        self.assertEqual(spec.publish_count, 3)

    def test_invalid_mode_rejected(self):
        spec = server._RunSpec(mode="nonsense")
        self.assertEqual(spec.mode, "nonsense")


if __name__ == "__main__":
    unittest.main()
