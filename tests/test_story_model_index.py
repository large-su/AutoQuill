import json
import tempfile
import threading
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core import story_model_index as index


class _Driver:
    provider_name = "Doubao"

    def __init__(self, selection):
        self.observed_model_selection = selection


class StoryModelIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = lambda *parts: str(Path(self.temp.name, *parts))
        self.paths_patch = patch.object(index.paths, "data", self.data)
        self.paths_patch.start()

    def tearDown(self):
        self.paths_patch.stop()
        self.temp.cleanup()

    def test_web_uses_observed_selection_and_not_api_config(self):
        driver = _Driver({"model": "网页模型", "options": {"mode": "fast", "prompt": "drop"}})
        fake_config = types.SimpleNamespace(LLM_API_MODEL="残留API")
        with patch.dict(sys.modules, {"config": fake_config}):
            chain = index.web_chain(driver)
        self.assertEqual(chain, {"channel": "web", "provider": "Doubao", "model": "网页模型", "options": {"mode": "fast"}})

    def test_unknown_web_selection_is_null(self):
        chain = index.web_chain(_Driver(None))
        self.assertIsNone(chain["model"])
        self.assertIsNone(chain["options"])

    def test_api_chain_uses_actual_api_model(self):
        fake_config = types.SimpleNamespace(LLM_PROVIDER="P", LLM_API_MODEL="actual")
        with patch.dict(sys.modules, {"config": fake_config}):
            self.assertEqual(index.api_chain(), {"channel": "api", "provider": "P", "model": "actual"})

    def test_unknown_settings_mode_does_not_become_api(self):
        settings = {"LLM_MODE": "future", "LLM_PROVIDER": "P", "LLM_MODEL_ID": "M"}
        self.assertEqual(index.chain_from_settings(settings), {"channel": "unknown", "provider": None, "model": None})

    def test_restart_idempotence_and_changed_chain_history(self):
        chain = {"channel": "api", "provider": "P", "model": "M"}
        self.assertTrue(index.append_publication("题目", "https://x", chain))
        self.assertFalse(index.append_publication("题目", "https://x", chain))
        self.assertTrue(index.append_publication("题目", "https://x", {**chain, "model": "N"}))
        self.assertTrue(index.append_publication("题目", "https://x", chain))
        self.assertFalse(index.append_publication("题目", "https://x", chain))
        records = [json.loads(line) for line in index.index_path().read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["title"], "题目")

    def test_whitelist_unicode_and_thread_safe_lines(self):
        chain = {"channel": "web", "provider": "P", "model": None, "options": {"x": "中文", "secret": "no", "nested": {}} , "body": "no"}
        def write():
            index.append_publication("标题", "https://x", chain)
        threads = [threading.Thread(target=write) for _ in range(10)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        lines = index.index_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["chain"], {"channel": "web", "provider": "P", "model": None, "options": {"x": "中文"}})


if __name__ == "__main__":
    unittest.main()
