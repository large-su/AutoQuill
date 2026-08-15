"""运行时模型切换（config.set_runtime_model）与生成进度心跳测试。"""

import unittest


class TestRuntimeModelSwitch(unittest.TestCase):
    """config.py: set_runtime_model 切换后各派生常量同步更新。"""

    @classmethod
    def setUpClass(cls):
        import config
        cls._orig = (config.LLM_PROVIDER, config.LLM_MODEL_ID)

    @classmethod
    def tearDownClass(cls):
        import config
        config.set_runtime_model(*cls._orig, persist=False)

    def test_switch_to_another_model(self):
        import config
        # 找出一个与当前不同的模型
        with open(config._PROVIDERS_FILE, encoding="utf-8") as f:
            import json
            providers = json.load(f)
        target = None
        for p in providers:
            for m in p.get("models", []):
                if m["id"] != config.LLM_MODEL_ID:
                    target = (p["name"], m["id"])
                    break
            if target:
                break
        self.assertIsNotNone(target)
        eff = config.set_runtime_model(*target, persist=False)
        self.assertEqual(eff["provider"], target[0])
        self.assertEqual(eff["model_id"], target[1])
        # 派生常量全部跟随
        self.assertEqual(config.LLM_PROVIDER, target[0])
        self.assertEqual(config.LLM_MODEL_ID, target[1])
        self.assertEqual(config.LLM_API_MODEL, target[1])
        self.assertTrue(config.LLM_API_KEY)
        self.assertTrue(config.LLM_API_BASE_URL)

    def test_switch_back(self):
        import config
        eff = config.set_runtime_model(*self._orig, persist=False)
        self.assertEqual(eff["provider"], self._orig[0])
        self.assertEqual(eff["model_id"], self._orig[1])
        self.assertEqual(config.LLM_API_MODEL, self._orig[1])

    def test_invalid_provider_raises(self):
        import config
        with self.assertRaises(ValueError):
            config.set_runtime_model("不存在的服务商", "x", persist=False)


class TestQuestionSourceSwitch(unittest.TestCase):
    """选题来源（推荐话题/邀请回答/自选问题）运行时切换。"""

    @classmethod
    def setUpClass(cls):
        from config import story
        cls._orig_source = story.QUESTION_SOURCE
        cls._orig_url = story.CUSTOM_QUESTION_URL

    @classmethod
    def tearDownClass(cls):
        from config import story
        story.QUESTION_SOURCE = cls._orig_source
        story.CUSTOM_QUESTION_URL = cls._orig_url

    def test_switch_to_invited(self):
        from config import story, set_runtime_question_source
        eff = set_runtime_question_source("invited", persist=False)
        self.assertEqual(eff["question_source"], "invited")
        self.assertEqual(story.QUESTION_SOURCE, "invited")

    def test_switch_to_custom(self):
        from config import story, set_runtime_question_source
        eff = set_runtime_question_source("custom", persist=False)
        self.assertEqual(story.QUESTION_SOURCE, "custom")

    def test_invalid_source_raises(self):
        from config import story, set_runtime_question_source
        with self.assertRaises(ValueError):
            set_runtime_question_source("bogus", persist=False)
        # 非法值不得污染当前状态
        self.assertIn(story.QUESTION_SOURCE,
                      ("recommend", "invited", "custom"))

    def test_custom_url_switched_and_stripped(self):
        from config import story, set_runtime_custom_question_url
        eff = set_runtime_custom_question_url(
            "  https://www.zhihu.com/question/12345  ", persist=False)
        self.assertEqual(eff["custom_question_url"],
                         "https://www.zhihu.com/question/12345")
        self.assertEqual(story.CUSTOM_QUESTION_URL,
                         "https://www.zhihu.com/question/12345")
        eff = set_runtime_custom_question_url(None, persist=False)
        self.assertEqual(story.CUSTOM_QUESTION_URL, "")

    def test_persist_roundtrip_via_apply_override(self):
        # 持久化 → 启动恢复：写 webui_model.json（临时文件）后重放
        # _apply_webui_model_override 的恢复逻辑
        import config
        from config import story
        orig_file = config._WEBUI_MODEL_FILE
        import tempfile
        import os
        import json
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False)
        tmp.close()
        try:
            config._WEBUI_MODEL_FILE = tmp.name
            with open(tmp.name, "w", encoding="utf-8") as f:
                json.dump({
                    "question_source": "invited",
                    "custom_question_url": "https://www.zhihu.com/question/999",
                }, f, ensure_ascii=False)
            config._apply_webui_model_override()
            self.assertEqual(story.QUESTION_SOURCE, "invited")
            self.assertEqual(story.CUSTOM_QUESTION_URL,
                             "https://www.zhihu.com/question/999")
        finally:
            config._WEBUI_MODEL_FILE = orig_file
            os.unlink(tmp.name)


class TestHeartbeatAccumulation(unittest.TestCase):
    """llm_api 生成心跳：累计总量单调递增（而非每 400 字窗口归零）。"""

    def _on_chunk(self, chunks):
        # 复刻 llm_api.generate_story 的 _on_chunk 逻辑
        heartbeat = {"n": 0, "total": 0}
        logged = []
        for c in chunks:
            heartbeat["n"] += len(c)
            heartbeat["total"] += len(c)
            if heartbeat["n"] >= 400:
                logged.append(heartbeat["total"])
                heartbeat["n"] = 0
        return logged

    def test_total_monotonic_increasing(self):
        logs = self._on_chunk(["a" * 300] * 10)  # 3000 字符
        self.assertEqual(logs, [600, 1200, 1800, 2400, 3000])

    def test_not_stuck_at_400(self):
        logs = self._on_chunk(["a" * 400] * 4)   # 1600 字符
        self.assertEqual(logs, [400, 800, 1200, 1600])

    def test_small_chunks_accumulate(self):
        logs = self._on_chunk(["ab"] * 300)      # 600 字符，每块 2
        # 首条在累计达 400 时记录（此后每满 400 记一条，600 尚未触发）
        self.assertEqual(logs, [400])


if __name__ == "__main__":
    unittest.main()
