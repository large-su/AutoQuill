# ============================================================
# tests/test_webui_server.py — Web 控制台后端 API
# ============================================================

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_mode_get(self):
        r = self.client.get("/api/mode")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("mode", data)
        self.assertIn("allowed", data)
        self.assertIn("api", data["allowed"])
        self.assertIn("web", data["allowed"])

    def test_mode_post_switch(self):
        import config
        orig = config.LLM_MODE
        try:
            r = self.client.post("/api/mode", json={"mode": "web"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["effective"]["mode"], "web")
            self.assertEqual(config.LLM_MODE, "web")
        finally:
            config.set_runtime_mode(orig, persist=False)

    def test_mode_post_invalid(self):
        r = self.client.post("/api/mode", json={"mode": "gpu"})
        self.assertEqual(r.status_code, 400)

    def test_browser_get(self):
        r = self.client.get("/api/browser")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("headless", data)
        self.assertIsInstance(data["headless"], bool)

    def test_browser_post_switch(self):
        import config
        orig = config.BROWSER_HEADLESS
        try:
            r = self.client.post("/api/browser", json={"headless": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["effective"]["headless"])
            self.assertTrue(config.BROWSER_HEADLESS)
        finally:
            config.set_runtime_browser_headless(orig, persist=False)


class TestAuthorEndpoints(unittest.TestCase):
    """文风列表 / 切换 / 提炼任务分发。"""

    @classmethod
    def setUpClass(cls):
        import applications.zhihu_story.author_profiler as ap
        from applications.zhihu_story import config as sconfig
        cls._orig_dir = ap.AUTHORS_DIR
        ap.AUTHORS_DIR = tempfile.mkdtemp(prefix="authors_")
        cls._orig_current = sconfig.AUTHOR_PROFILE
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        import applications.zhihu_story.author_profiler as ap
        ap.AUTHORS_DIR = cls._orig_dir
        from applications.zhihu_story import config as sconfig
        sconfig.AUTHOR_PROFILE = cls._orig_current

    def _make_profile(self, name):
        import json
        from pathlib import Path
        import applications.zhihu_story.author_profiler as ap
        profile = {
            "author": name, "profiled_at": "2026-08-12 10:00:00",
            "source_stories": [{"title": "t", "likes": 1, "chars": 100}] * 3,
            "signature": {"style": f"{name}的文风", "tone": "冷峻",
                          "opening_patterns": ["开局直入"]},
        }
        Path(ap.AUTHORS_DIR, f"{name}.json").write_text(
            json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        return profile

    def test_authors_list_and_current(self):
        self._make_profile("测试作者")
        r = self.client.get("/api/authors")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("authors", data)
        self.assertIn("current", data)
        names = [a["name"] for a in data["authors"]]
        self.assertIn("测试作者", names)
        self.assertIn("stories_count", data["authors"][0])

    def test_author_post_switch(self):
        from applications.zhihu_story import config as sconfig
        orig = sconfig.AUTHOR_PROFILE
        try:
            r = self.client.post("/api/author", json={"name": "测试作者"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["effective"]["author_profile"], "测试作者")
            self.assertEqual(sconfig.AUTHOR_PROFILE, "测试作者")
        finally:
            sconfig.AUTHOR_PROFILE = orig

    def test_author_post_clear(self):
        from applications.zhihu_story import config as sconfig
        orig = sconfig.AUTHOR_PROFILE
        try:
            r = self.client.post("/api/author", json={"name": ""})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["effective"]["author_profile"], "")
        finally:
            sconfig.AUTHOR_PROFILE = orig

    def test_profile_sources_empty_ok(self):
        r = self.client.get("/api/profile-sources")
        self.assertEqual(r.status_code, 200)
        self.assertIn("authors", r.json())

    def test_dispatch_profile_missing_author_400(self):
        with self.assertRaises(Exception):
            server.runner._dispatch(server._RunSpec(mode="profile", author=""))

    def test_dispatch_profile_success(self):
        # mock LLM 剖析，验证提炼 → 落盘 → 自动切换文风 全链路
        import applications.zhihu_story.author_profiler as ap
        from pathlib import Path
        from applications.zhihu_story import config as sconfig
        orig = sconfig.AUTHOR_PROFILE
        src_file = ap.STORY_LIB
        try:
            lib = Path(tempfile.mkdtemp(prefix="lib_")) / "collected.jsonl"
            with open(lib, "w", encoding="utf-8") as f:
                for i in range(3):
                    f.write('{"author": "提炼作者", "title": "t%d",'
                            ' "answer": "%s", "footer": {"likes": 300}}\n'
                            % (i, "故事正文" * 40))
            ap.STORY_LIB = str(lib)
            # _call_profile_llm 真实返回的是签名本体（含 style 的 dict）
            fake = {"style": "测试文风", "tone": "冷"}
            with mock.patch.object(ap, "_call_profile_llm",
                                   return_value=fake):
                ok = server.runner._dispatch(
                    server._RunSpec(mode="profile", author="提炼作者"))
            self.assertTrue(ok)
            self.assertEqual(sconfig.AUTHOR_PROFILE, "提炼作者")
            saved = Path(ap.AUTHORS_DIR, "提炼作者.json")
            self.assertTrue(saved.exists(), "签名必须落盘")
            self.assertIn("测试文风",
                          server.runner.last_context["profile"]["summary"])
        finally:
            sconfig.AUTHOR_PROFILE = orig
            ap.STORY_LIB = src_file

    def test_dispatch_general_profile_success(self):
        import applications.zhihu_story.author_profiler as ap
        from pathlib import Path
        from applications.zhihu_story import config as sconfig
        orig = sconfig.AUTHOR_PROFILE
        src_file = ap.STORY_LIB
        try:
            lib = Path(tempfile.mkdtemp(prefix="lib_")) / "collected.jsonl"
            with open(lib, "w", encoding="utf-8") as f:
                for i in range(4):
                    f.write('{"author": "甲", "title": "t%d",'
                            ' "answer": "%s", "footer": {"likes": 300}}\n'
                            % (i, "通用正文" * 40))
            ap.STORY_LIB = str(lib)
            fake = {"style": "通用风格", "tone": "暖"}
            with mock.patch.object(ap, "_call_profile_llm",
                                   return_value=fake):
                ok = server.runner._dispatch(
                    server._RunSpec(mode="general_profile"))
            self.assertTrue(ok)
            self.assertEqual(sconfig.AUTHOR_PROFILE, "通用")
            saved = Path(ap.AUTHORS_DIR, "_general.json")
            self.assertTrue(saved.exists(), "通用签名必须落盘")
        finally:
            sconfig.AUTHOR_PROFILE = orig
            ap.STORY_LIB = src_file


class TestCollectDispatch(unittest.TestCase):
    """故事采集任务分发（collect 模式）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_dispatch_collect_missing_url_400(self):
        with self.assertRaises(Exception) as ctx:
            server.runner._dispatch(server._RunSpec(mode="collect"))
        self.assertIn("URL", str(ctx.exception))

    def test_dispatch_collect_invalid_count_400(self):
        with self.assertRaises(Exception) as ctx:
            server.runner._dispatch(server._RunSpec(
                mode="collect", url="https://www.zhihu.com/people/x/answers",
                count=0))
        self.assertIn("1-500", str(ctx.exception))

    def test_dispatch_collect_success(self):
        from applications.zhihu_story import browser_adapter as ba
        fake_browser = mock.MagicMock()
        fake_result = {"collected": [{"title": "新故事", "answer": "x" * 200,
                                      "footer": {"answer_url": "https://x/1"}}],
                       "author": "自动识别作者", "existing": 5}
        with mock.patch("applications.zhihu_story.browser_adapter.get_browser",
                        return_value=fake_browser):
            with mock.patch("applications.zhihu_story.collector.collect_author_stories",
                            return_value=fake_result) as m:
                ok = server.runner._dispatch(server._RunSpec(
                    mode="collect",
                    url="https://www.zhihu.com/people/x/answers",
                    count=7))
        self.assertTrue(ok)
        # 参数原样传递（URL/数量；作者名由采集器自动识别，不传）
        args = m.call_args
        self.assertEqual(args.args[0],
                         "https://www.zhihu.com/people/x/answers")
        self.assertEqual(args.kwargs["count"], 7)
        self.assertNotIn("author", args.kwargs,
                         "作者名必须全自动识别，不允许手动指定")
        self.assertIs(args.kwargs["browser"], fake_browser)
        # 结果上下文可展示（含库中总数 = 已有 + 新增）
        ctx = server.runner.last_context["collect"]
        self.assertIn("自动识别作者", ctx["title"])
        self.assertIn("新增 1 篇", ctx["summary"])
        self.assertIn("该作者库中共 6 篇", ctx["summary"])

    def test_dispatch_collect_nothing_new(self):
        # 全部重复/失败 → 返回 False（任务未成功，日志说明）
        with mock.patch("applications.zhihu_story.browser_adapter.get_browser",
                        return_value=mock.MagicMock()):
            with mock.patch("applications.zhihu_story.collector.collect_author_stories",
                            return_value={"collected": [], "author": "空作者"}):
                ok = server.runner._dispatch(server._RunSpec(
                    mode="collect",
                    url="https://www.zhihu.com/people/x/answers"))
        self.assertFalse(ok)


class TestModelEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        import config
        cls._orig = (config.LLM_PROVIDER, config.LLM_MODEL_ID)

    @classmethod
    def tearDownClass(cls):
        import config
        config.set_runtime_model(*cls._orig, persist=False)

    def test_models_lists_providers_without_keys(self):
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("current", data)
        self.assertTrue(data["providers"])
        # 绝不能泄露密钥
        blob = str(data)
        self.assertNotIn("sk-", blob)
        self.assertNotIn("apiKey", blob)

    def test_config_model_not_null(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        cfg = r.json()
        self.assertTrue(cfg.get("LLM_MODEL"), "LLM_MODEL 不应为 null/空")

    def test_set_model_roundtrip(self):
        import config
        # 选一个与默认不同的模型，切过去再切回来
        r = self.client.get("/api/models")
        provs = r.json()["providers"]
        target = None
        for p in provs:
            for m in p["models"]:
                if m["id"] != config.LLM_MODEL_ID:
                    target = (p["name"], m["id"])
                    break
            if target:
                break
        self.assertIsNotNone(target, "应有至少一个可切换模型")
        r = self.client.post("/api/model", json={
            "provider": target[0], "model_id": target[1]})
        self.assertEqual(r.status_code, 200)
        eff = r.json()["effective"]
        self.assertEqual(eff["provider"], target[0])
        self.assertEqual(eff["model_id"], target[1])
        # 生效后 config 模块属性同步变化
        self.assertEqual(config.LLM_PROVIDER, target[0])
        self.assertEqual(config.LLM_MODEL_ID, target[1])
        self.assertEqual(config.LLM_API_MODEL, target[1])

    def test_set_model_invalid_rejected(self):
        r = self.client.post("/api/model", json={
            "provider": "不存在的服务商", "model_id": "x"})
        self.assertEqual(r.status_code, 400)


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


class TestStoryLibApi(unittest.TestCase):
    """采集库管理：聚合查询 + 按作者/单条删除 + 运行中拒绝。"""

    RECS = [
        {"author": "甲", "title": "甲1", "answer": "x" * 200,
         "footer": {"answer_url": "https://zhihu.com/question/1/answer/11"},
         "collected_at": "2026-08-01 10:00:00"},
        {"author": "甲", "title": "甲2", "answer": "y" * 200,
         "footer": {"answer_url":
                    "https://zhihu.com/question/1/answer/12?utm=1"},
         "collected_at": "2026-08-02 10:00:00"},
        {"author": "乙", "title": "乙1", "answer": "z" * 200,
         "footer": {"answer_url":
                    "https://zhihu.com/question/2/answer/21#x"},
         "collected_at": "2026-08-03 10:00:00"},
    ]

    @classmethod
    def setUpClass(cls):
        import applications.zhihu_story.author_profiler as ap
        cls._orig_lib = ap.STORY_LIB
        cls._orig_dir = ap.AUTHORS_DIR
        cls.tmp = tempfile.mkdtemp(prefix="storylib_")
        ap.STORY_LIB = str(Path(cls.tmp) / "lib.jsonl")
        ap.AUTHORS_DIR = str(Path(cls.tmp) / "authors")
        os.makedirs(ap.AUTHORS_DIR, exist_ok=True)
        Path(ap.AUTHORS_DIR, "甲.json").write_text("{}", encoding="utf-8")
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        import applications.zhihu_story.author_profiler as ap
        ap.STORY_LIB = cls._orig_lib
        ap.AUTHORS_DIR = cls._orig_dir
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 每个测试重置库（删除测试会改库，避免顺序依赖）
        import applications.zhihu_story.author_profiler as ap
        with open(ap.STORY_LIB, "w", encoding="utf-8") as f:
            for rec in self.RECS:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_list_aggregates_with_profile_flag(self):
        r = self.client.get("/api/storylib")
        self.assertEqual(r.status_code, 200)
        by_name = {a["name"]: a for a in r.json()["authors"]}
        self.assertEqual(by_name["甲"]["records"], 2)
        self.assertTrue(by_name["甲"]["has_profile"], "甲有签名文件")
        self.assertEqual(by_name["乙"]["records"], 1)
        self.assertFalse(by_name["乙"]["has_profile"])

    def test_detail_by_author(self):
        r = self.client.get("/api/storylib", params={"author": "甲"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["author"], "甲")
        self.assertEqual(len(data["records"]), 2)
        self.assertTrue(any("answer/12?utm=1" in rec["answer_url"]
                            for rec in data["records"]),
                        "单条删除用原始 answer_url 定位")

    def test_delete_by_author(self):
        r = self.client.request(
            "DELETE", "/api/storylib", json={"author": "甲"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["removed"], 2)
        self.assertEqual([a["name"] for a in data["authors"]], ["乙"])

    def test_delete_single_by_url(self):
        r = self.client.request(
            "DELETE", "/api/storylib",
            json={"url": "https://zhihu.com/question/2/answer/21#x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["removed"], 1)
        r2 = self.client.get("/api/storylib", params={"author": "甲"})
        self.assertEqual(len(r2.json()["records"]), 2, "甲的不受影响")

    def test_delete_missing_params_400(self):
        r = self.client.request("DELETE", "/api/storylib", json={})
        self.assertEqual(r.status_code, 400)

    def test_delete_not_found_404(self):
        r = self.client.request(
            "DELETE", "/api/storylib", json={"author": "不存在"})
        self.assertEqual(r.status_code, 404)

    def test_delete_blocked_while_running(self):
        orig = server.runner.state
        server.runner.state = "running"
        try:
            r = self.client.request(
                "DELETE", "/api/storylib", json={"author": "甲"})
            self.assertEqual(r.status_code, 409)
        finally:
            server.runner.state = orig


if __name__ == "__main__":
    unittest.main()
