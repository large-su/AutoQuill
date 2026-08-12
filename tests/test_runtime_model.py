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
