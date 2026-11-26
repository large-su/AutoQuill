import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class StoryModelProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = lambda *parts: str(self.root.joinpath(*parts))

    def tearDown(self):
        self.temp.cleanup()

    def test_save_draft_publish_keeps_generation_chain_and_index(self):
        from core import paths, evolution
        from workflows.base import WorkflowBase

        with patch.object(paths, "data", self.data):
            workflow = WorkflowBase()
            workflow._last_story_model_chain = {
                "channel": "web", "provider": "Doubao", "model": "网页模型",
                "options": {"mode": "fast"},
            }
            story_path = workflow.save_story_file("故事正文")
            draft = evolution.record_draft(
                "https://www.zhihu.com/question/123", story_path, "测试题目",
                story_text="故事正文", rendered_html="<p>故事正文</p>")
            self.assertIsNotNone(draft)
            self.assertEqual(evolution._load()[0]["generations"][-1]["model_chain"]["provider"], "Doubao")
            receipt = {"ok": True, "qid": "123", "title": "测试题目",
                       "url": "https://www.zhihu.com/answer/456",
                       "draft_content_fingerprint": draft["content_fingerprint"]}
            import config
            with patch.object(config, "LLM_PROVIDER", "Changed"), \
                 patch.object(config, "LLM_MODEL_ID", "changed-model"):
                evolution.record_publication(receipt)
            evolution.record_publication(receipt)
            evolution.record_publication(receipt)

            state = evolution._load()[0]
            self.assertEqual(state["articles"]["456"].get("generation_id"), state["generations"][-1]["id"])

            index_file = Path(self.data("data", "state", "story_models.jsonl"))
            rows = [json.loads(line) for line in index_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["chain"].get("provider"), "Doubao", rows)

            # Ordinary retained artifacts may be pruned without touching the index.
            shutil.rmtree(self.root / "output")
            (self.root / "logs").mkdir()
            shutil.rmtree(self.root / "logs")
            from webui import _snapshot
            dashboard = self.root / "data" / "published_answers_2026-01-01.json"
            dashboard.write_text(json.dumps([{"aid": "456"}]), encoding="utf-8")
            before = index_file.read_bytes()
            self.assertEqual(_snapshot.prune_rows(dashboard.parent, "published_answers_*.json", "aid", ["456"]), 1)
            self.assertTrue(index_file.exists())
            self.assertEqual(index_file.read_bytes(), before)

    def test_retry_keeps_chain_for_highest_scoring_old_story(self):
        from config import story as story_config
        from workflows.base import WorkflowBase
        from core import story_text, originality
        import config

        old_mode = config.LLM_MODE
        config.LLM_MODE = "api"
        try:
            workflow = WorkflowBase()
            stories = ["旧稿" * 400, "新稿" * 400]
            chains = [
                {"channel": "api", "provider": "P", "model": "old"},
                {"channel": "api", "provider": "P", "model": "new"},
            ]
            calls = []

            def fake_generate(*args, **kwargs):
                index = len(calls)
                calls.append(index)
                workflow._last_story_model_chain = chains[index]
                return stories[index]

            def fake_validate(text):
                return (8 if text == stories[0] else 4, False, {"格式": "测试失败"})

            with patch.object(workflow, "generate_story", side_effect=fake_generate), \
                 patch.object(story_text, "validate_story_format", side_effect=fake_validate), \
                 patch.object(originality, "opening_copy_signals", return_value={"risky": False}), \
                 patch.object(story_config, "STORY_GENERATE_MAX_ATTEMPTS", 2):
                story, passed = workflow.generate_story_with_retry(
                    "题目", "参考", max_attempts=2, min_length=1)
            self.assertFalse(passed)
            self.assertEqual(story, stories[0])
            self.assertEqual(workflow._last_story_model_chain, chains[0])
        finally:
            config.LLM_MODE = old_mode

    def test_web_preferences_capture_detached_actual_snapshot(self):
        from web_drivers.base import WebLLMDriver
        actual = {"model": "Model-A", "options": {"mode": "fast"}}
        driver = WebLLMDriver({})
        with patch("web_drivers.capabilities.apply_selection", return_value=actual):
            self.assertIs(driver.apply_preferences(), actual)
        actual["model"] = "Model-B"
        actual["options"]["mode"] = "slow"
        self.assertEqual(driver.observed_model_selection,
                         {"model": "Model-A", "options": {"mode": "fast"}})

    def test_parallel_api_progress_records_api_chain_in_web_runtime_mode(self):
        import config
        import story_generation
        import llm_client

        old = (config.LLM_MODE, config.LLM_API_KEY, config.LLM_API_MAX_TOKENS,
               config.LLM_PROVIDER, config.LLM_API_MODEL)
        config.LLM_MODE = "web"
        config.LLM_API_KEY = "test-key"
        config.LLM_API_MAX_TOKENS = 100
        config.LLM_PROVIDER = "P"
        config.LLM_API_MODEL = "actual-api"
        progress = {}

        def fake_stream(_prompt, max_tokens=None, on_chunk=None, **kwargs):
            on_chunk("完整文本")
            return "完整文本", 0.01, None

        try:
            with patch.object(story_generation, "build_story_prompt", return_value=("prompt", "test")), \
                 patch.object(story_generation, "_load_author_profile_or_none", return_value=None), \
                 patch.object(llm_client, "_call_llm_streaming", side_effect=fake_stream):
                result = story_generation.generate_story_parallel(
                    "问题", "参考", 1, progress)
            self.assertEqual(result, "完整文本")
            self.assertEqual(progress[1]["model_chain"], {
                "channel": "api", "provider": "P", "model": "actual-api"})
        finally:
            (config.LLM_MODE, config.LLM_API_KEY, config.LLM_API_MAX_TOKENS,
             config.LLM_PROVIDER, config.LLM_API_MODEL) = old


if __name__ == "__main__":
    unittest.main()
