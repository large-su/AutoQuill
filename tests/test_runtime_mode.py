# ============================================================
# tests/test_runtime_mode.py — 生成通道 + 浏览器模式运行时切换
#
# set_runtime_mode 把 LLM_MODE 切到 api/web，持久化到
# webui_model.json（mode 字段），启动时恢复。
# set_runtime_browser_headless 切 前台调试/无头工作（headless 字段）。
# ============================================================

import json
import os
import tempfile
import unittest


class TestRuntimeMode(unittest.TestCase):
    def setUp(self):
        import config
        self._orig_mode = config.LLM_MODE
        self._tmpfile = os.path.join(tempfile.mkdtemp(), "webui_model.json")
        # 持久化路径改到临时文件，不碰真实配置
        config._WEBUI_MODEL_FILE = self._tmpfile

    def tearDown(self):
        import config
        config.LLM_MODE = self._orig_mode
        try:
            os.remove(self._tmpfile)
        except OSError:
            pass

    def test_set_web_mode_effective(self):
        import config
        eff = config.set_runtime_mode("web", persist=False)
        self.assertEqual(eff["mode"], "web")
        self.assertEqual(config.LLM_MODE, "web")

    def test_set_api_mode_effective(self):
        import config
        eff = config.set_runtime_mode("api", persist=False)
        self.assertEqual(eff["mode"], "api")
        self.assertEqual(config.LLM_MODE, "api")

    def test_invalid_mode_raises(self):
        import config
        with self.assertRaises(ValueError):
            config.set_runtime_mode("gpu")
        # 非法值不得改动当前模式
        self.assertIn(config.LLM_MODE, ("api", "web"))

    def test_persist_roundtrip(self):
        import config
        config.set_runtime_mode("web", persist=True)
        with open(self._tmpfile, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["mode"], "web")

    def test_persist_keeps_provider_and_model(self):
        # 切通道不得清掉模型选择（同文件共存）
        import config
        # 用注册表里真实存在的 provider（假名会被 _save_webui_state guard 拦下）
        with open(config._PROVIDERS_FILE, encoding="utf-8") as f:
            providers = json.load(f)
        real = (providers[0]["name"], providers[0]["models"][0]["id"])
        config.set_runtime_model(*real, persist=True)
        config.set_runtime_mode("web", persist=True)
        with open(self._tmpfile, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["provider"], real[0])
        self.assertEqual(data["model_id"], real[1])
        self.assertEqual(data["mode"], "web")

    def test_apply_override_restores_mode(self):
        import config
        config.set_runtime_mode("web", persist=True)
        config.LLM_MODE = "api"  # 模拟重启后默认
        config._apply_webui_model_override()
        self.assertEqual(config.LLM_MODE, "web")

    def test_apply_override_ignores_missing_mode_field(self):
        # 旧版 webui_model.json 无 mode 字段 → 保持默认 api，不报错
        import config
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump({"provider": "A", "model_id": "B"}, f)
        config.LLM_MODE = "api"
        config._apply_webui_model_override()
        self.assertEqual(config.LLM_MODE, "api")

    def test_apply_override_ignores_invalid_mode(self):
        import config
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump({"mode": "gpu"}, f)
        config.LLM_MODE = "api"
        config._apply_webui_model_override()
        self.assertEqual(config.LLM_MODE, "api")


class TestRuntimeBrowserHeadless(unittest.TestCase):
    """浏览器模式运行时切换（调试 False=前台 / 工作 True=无头）。"""

    def setUp(self):
        import config
        self._orig = config.BROWSER_HEADLESS
        self._tmpfile = os.path.join(tempfile.mkdtemp(), "webui_model.json")
        config._WEBUI_MODEL_FILE = self._tmpfile

    def tearDown(self):
        import config
        config.BROWSER_HEADLESS = self._orig
        try:
            os.remove(self._tmpfile)
        except OSError:
            pass

    def test_set_headless_effective(self):
        import config
        eff = config.set_runtime_browser_headless(True, persist=False)
        self.assertTrue(eff["headless"])
        self.assertTrue(config.BROWSER_HEADLESS)

    def test_set_foreground_effective(self):
        import config
        eff = config.set_runtime_browser_headless(False, persist=False)
        self.assertFalse(eff["headless"])
        self.assertFalse(config.BROWSER_HEADLESS)

    def test_persist_roundtrip(self):
        import config
        config.set_runtime_browser_headless(True, persist=True)
        with open(self._tmpfile, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIs(data["headless"], True)

    def test_apply_override_restores_headless(self):
        import config
        config.set_runtime_browser_headless(True, persist=True)
        config.BROWSER_HEADLESS = False  # 模拟重启后默认
        config._apply_webui_model_override()
        self.assertTrue(config.BROWSER_HEADLESS)

    def test_apply_override_ignores_missing_field(self):
        import config
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump({"mode": "api"}, f)
        config.BROWSER_HEADLESS = False
        config._apply_webui_model_override()
        self.assertFalse(config.BROWSER_HEADLESS)


class TestRuntimeAuthorProfile(unittest.TestCase):
    """作者文风运行时切换（空串 = 不注入，持久化 + 启动恢复）。"""

    def setUp(self):
        import config
        from config import story
        self._orig = story.AUTHOR_PROFILE
        self._tmpfile = os.path.join(tempfile.mkdtemp(), "webui_model.json")
        config._WEBUI_MODEL_FILE = self._tmpfile

    def tearDown(self):
        import config
        from config import story
        story.AUTHOR_PROFILE = self._orig
        try:
            os.remove(self._tmpfile)
        except OSError:
            pass

    def test_set_author_effective(self):
        import config
        from config import story
        eff = config.set_runtime_author_profile("张三", persist=False)
        self.assertEqual(eff["author_profile"], "张三")
        self.assertEqual(story.AUTHOR_PROFILE, "张三")

    def test_set_empty_clears(self):
        import config
        from config import story
        config.set_runtime_author_profile("张三", persist=False)
        eff = config.set_runtime_author_profile("", persist=False)
        self.assertEqual(eff["author_profile"], "")
        self.assertEqual(story.AUTHOR_PROFILE, "")

    def test_persist_roundtrip(self):
        import config
        config.set_runtime_author_profile("李四", persist=True)
        with open(self._tmpfile, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["author_profile"], "李四")

    def test_persist_keeps_other_fields(self):
        # 切文风不得清掉模型选择（同文件共存）
        import config
        with open(config._PROVIDERS_FILE, encoding="utf-8") as f:
            providers = json.load(f)
        real = (providers[0]["name"], providers[0]["models"][0]["id"])
        config.set_runtime_model(*real, persist=True)
        config.set_runtime_author_profile("李四", persist=True)
        with open(self._tmpfile, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["provider"], real[0])
        self.assertEqual(data["model_id"], real[1])
        self.assertEqual(data["author_profile"], "李四")

    def test_persist_keeps_mode_and_headless(self):
        # ★ 回归：切文风不得覆盖已持久化的生成通道/浏览器模式
        #   （_save_webui_state 曾重建文件丢字段，重启后静默恢复默认）
        import config
        config.set_runtime_mode("web", persist=True)
        config.set_runtime_browser_headless(True, persist=True)
        config.set_runtime_author_profile("李四", persist=True)
        with open(self._tmpfile, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["mode"], "web")
        self.assertIs(data["headless"], True)
        self.assertEqual(data["author_profile"], "李四")

    def test_apply_override_restores_author(self):
        import config
        from config import story
        config.set_runtime_author_profile("王五", persist=True)
        story.AUTHOR_PROFILE = ""  # 模拟重启后默认
        config._apply_webui_model_override()
        self.assertEqual(story.AUTHOR_PROFILE, "王五")

    def test_apply_override_ignores_missing_field(self):
        # 旧版 webui_model.json 无 author_profile 字段 → 保持默认，不报错
        import config
        from config import story
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump({"mode": "api"}, f)
        story.AUTHOR_PROFILE = "旧值"
        config._apply_webui_model_override()
        self.assertEqual(story.AUTHOR_PROFILE, "旧值")


if __name__ == "__main__":
    unittest.main()
