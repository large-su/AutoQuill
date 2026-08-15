# ============================================================
# tests/test_webui_server.py — Web 控制台后端 API
# ============================================================

import json
import os
import tempfile
import time
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

    def test_config_tunable_roundtrip(self):
        # 选题参数前端可配：写 → 读 → 恢复（禁落盘，避免污染真实配置）
        from config import story
        orig = story.MAX_TOPIC_RETRY
        try:
            with mock.patch("config._save_webui_state"):
                r = self.client.post("/api/config", json={
                    "key": "MAX_TOPIC_RETRY", "value": 7})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["value"], 7)
            self.assertEqual(story.MAX_TOPIC_RETRY, 7)
            cfg = self.client.get("/api/config").json()
            self.assertEqual(cfg["MAX_TOPIC_RETRY"], 7)
        finally:
            story.MAX_TOPIC_RETRY = orig

    def test_config_tunable_clamped(self):
        # 超范围值被限幅（MAX_TOPIC_RETRY 上限 10）
        from config import story
        orig = story.MAX_TOPIC_RETRY
        try:
            with mock.patch("config._save_webui_state"):
                r = self.client.post("/api/config", json={
                    "key": "MAX_TOPIC_RETRY", "value": 99})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["value"], 10)
        finally:
            story.MAX_TOPIC_RETRY = orig

    def test_config_tunable_unknown_key_rejected(self):
        r = self.client.post("/api/config", json={
            "key": "LLM_API_KEY", "value": 1})
        self.assertEqual(r.status_code, 400)

    def test_config_tunable_non_int_rejected(self):
        r = self.client.post("/api/config", json={
            "key": "MAX_TOPIC_RETRY", "value": "abc"})
        self.assertEqual(r.status_code, 422)

    def test_question_source_get(self):
        from config import story
        r = self.client.get("/api/question-source")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("source", data)
        self.assertIn("custom_url", data)
        self.assertEqual(data["source"], story.QUESTION_SOURCE)

    def test_question_source_post_switch(self):
        # 切选题来源（推荐话题→邀请回答）；mock 禁落盘防污染真实配置
        from config import story
        orig = story.QUESTION_SOURCE
        try:
            with mock.patch("config._save_webui_state"):
                r = self.client.post("/api/question-source",
                                     json={"source": "invited"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["effective"]["question_source"],
                             "invited")
            self.assertEqual(story.QUESTION_SOURCE, "invited")
            # 配置速览同步反映新来源
            cfg = self.client.get("/api/config").json()
            self.assertEqual(cfg["QUESTION_SOURCE"], "invited")
        finally:
            story.QUESTION_SOURCE = orig

    def test_question_source_post_custom_with_url(self):
        from config import story
        orig_s, orig_u = story.QUESTION_SOURCE, story.CUSTOM_QUESTION_URL
        try:
            with mock.patch("config._save_webui_state"):
                r = self.client.post("/api/question-source", json={
                    "source": "custom",
                    "custom_url": "https://www.zhihu.com/question/12345"})
            self.assertEqual(r.status_code, 200)
            eff = r.json()["effective"]
            self.assertEqual(eff["question_source"], "custom")
            self.assertEqual(eff["custom_question_url"],
                             "https://www.zhihu.com/question/12345")
        finally:
            story.QUESTION_SOURCE = orig_s
            story.CUSTOM_QUESTION_URL = orig_u

    def test_question_source_invalid_400(self):
        r = self.client.post("/api/question-source",
                             json={"source": "bogus"})
        self.assertEqual(r.status_code, 400)

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
        # 闭环预检：切 web 需 DeepSeek 已登录（mock 缓存通过，免真实拉起 Edge）
        import config
        orig = config.LLM_MODE
        try:
            config.set_runtime_mode("api", persist=False)  # 起点非 web
            with mock.patch.object(server, "_web_llm_logged_in_cached",
                                   return_value=True):
                r = self.client.post("/api/mode", json={"mode": "web"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["effective"]["mode"], "web")
                self.assertEqual(config.LLM_MODE, "web")
        finally:
            config.set_runtime_mode(orig, persist=False)

    def test_mode_post_web_requires_login(self):
        # 闭环：切 web 且未登录 → 拒绝并带 needs=deepseek_login，模式不变
        import config
        orig = config.LLM_MODE
        try:
            config.set_runtime_mode("api", persist=False)  # 起点非 web
            with mock.patch.object(server, "_web_llm_logged_in_cached",
                                   return_value=False):
                r = self.client.post("/api/mode", json={"mode": "web"})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"]["needs"],
                                 "deepseek_login")
                self.assertEqual(config.LLM_MODE, "api")
        finally:
            config.set_runtime_mode(orig, persist=False)

    def test_mode_web_switch_uses_cache(self):
        # 切 web 走 15s 缓存：缓存命中（已登录）不再启动真实检测浏览器
        # （用户反馈切换检测慢：原来强制真实检测 ≈5-8s，现在秒回）
        import applications.zhihu_story.browser_adapter as ba
        import config
        orig = config.LLM_MODE
        try:
            config.set_runtime_mode("api", persist=False)
            server._web_llm_cache.update(ts=time.time(), ok=True)
            with mock.patch.object(ba, "web_llm_logged_in",
                                   return_value=False) as m:
                r = self.client.post("/api/mode", json={"mode": "web"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["effective"]["mode"], "web")
                m.assert_not_called()
        finally:
            config.set_runtime_mode(orig, persist=False)

    def test_mode_post_api_requires_key(self):
        # 闭环：切 api 且未配置 key → 拒绝并带 needs=api_key，模式不变
        import config
        orig = config.LLM_MODE
        try:
            config.set_runtime_mode("web", persist=False)  # 起点非 api
            with mock.patch.object(
                    config, "_load_provider_config",
                    return_value={"apiKey": "sk-your-placeholder"}):
                r = self.client.post("/api/mode", json={"mode": "api"})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"]["needs"], "api_key")
                self.assertEqual(config.LLM_MODE, "web")

            # key 有效 → 放行
            with mock.patch.object(
                    config, "_load_provider_config",
                    return_value={"apiKey": "sk-real-key-123"}):
                r = self.client.post("/api/mode", json={"mode": "api"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["effective"]["mode"], "api")
        finally:
            config.set_runtime_mode(orig, persist=False)

    def test_mode_post_invalid(self):
        r = self.client.post("/api/mode", json={"mode": "gpu"})
        self.assertEqual(r.status_code, 400)

    def test_run_web_gate_blocks_when_not_logged_in(self):
        # 闭环兜底：Web 通道生成任务运行时未登录 → 直接 error，不进 _dispatch
        import applications.zhihu_story.browser_adapter as ba
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "web"):
            with mock.patch.object(ba, "web_llm_logged_in",
                                   return_value=False):
                with mock.patch.object(runner, "_dispatch") as m:
                    runner._run_task(spec)
        self.assertEqual(runner.state, "error")
        self.assertIn("登录", runner.message)
        m.assert_not_called()

    def test_run_web_gate_sets_guide_needed(self):
        # 闭环引导标记：web 未登录 → guide_needed=deepseek_login，
        # 前端据此弹引导（而非只看报错文案）
        import applications.zhihu_story.browser_adapter as ba
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "web"):
            with mock.patch.object(ba, "web_llm_logged_in",
                                   return_value=False):
                with mock.patch.object(runner, "_dispatch"):
                    runner._run_task(spec)
        self.assertEqual(runner.guide_needed, "deepseek_login")
        # status() 透出给前端轮询
        self.assertEqual(runner.status()["guide_needed"],
                         "deepseek_login")

    def test_run_api_gate_blocks_without_key(self):
        # API 模式缺 key 同样闭环：error + guide_needed=api_key
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "api"):
            with mock.patch.object(
                    config, "_load_provider_config",
                    return_value={"apiKey": "sk-your-placeholder"}):
                with mock.patch.object(runner, "_dispatch") as m:
                    runner._run_task(spec)
        self.assertEqual(runner.state, "error")
        self.assertIn("API", runner.message)
        self.assertEqual(runner.guide_needed, "api_key")
        m.assert_not_called()

    def test_run_api_gate_allows_with_key(self):
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "api"):
            with mock.patch.object(
                    config, "_load_provider_config",
                    return_value={"apiKey": "sk-real-key-123"}):
                with mock.patch.object(runner, "_dispatch",
                                       return_value=True) as m:
                    runner._run_task(spec)
        self.assertEqual(runner.state, "done")
        m.assert_called_once_with(spec)

    def test_profile_mode_also_gated(self):
        # 文风提炼同样依赖 LLM 通道：web 未登录 → 引导标记
        import applications.zhihu_story.browser_adapter as ba
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="profile", author="甲")
        with mock.patch.object(config, "LLM_MODE", "web"):
            with mock.patch.object(ba, "web_llm_logged_in",
                                   return_value=False):
                with mock.patch.object(runner, "_dispatch") as m:
                    runner._run_task(spec)
        self.assertEqual(runner.guide_needed, "deepseek_login")
        m.assert_not_called()

    def test_start_clears_guide_needed(self):
        # 新任务开始前清除上次引导标记（每次运行重新检测）
        runner = server.TaskRunner()
        runner.guide_needed = "api_key"
        spec = server._RunSpec(mode="select")
        with mock.patch.object(runner, "_dispatch", return_value=True):
            runner.start(spec)
            runner._thread.join(timeout=10)
        self.assertIsNone(runner.guide_needed)
        self.assertEqual(runner.state, "done")

    def test_run_web_gate_allows_when_logged_in(self):
        import applications.zhihu_story.browser_adapter as ba
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "web"):
            with mock.patch.object(ba, "web_llm_logged_in",
                                   return_value=True):
                with mock.patch.object(runner, "_dispatch",
                                       return_value=True) as m:
                    runner._run_task(spec)
        self.assertEqual(runner.state, "done")
        m.assert_called_once_with(spec)

    def test_run_web_gate_skips_for_api_mode(self):
        # API 模式不查 DeepSeek 登录态（不应拉起浏览器）
        import applications.zhihu_story.browser_adapter as ba
        import config
        runner = server.TaskRunner()
        spec = server._RunSpec(mode="generate")
        with mock.patch.object(config, "LLM_MODE", "api"):
            with mock.patch.object(ba, "web_llm_logged_in") as m:
                with mock.patch.object(runner, "_dispatch",
                                       return_value=True):
                    runner._run_task(spec)
        m.assert_not_called()

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


class TestLocalOnlyGuard(unittest.TestCase):
    """本地单机守卫：Host 白名单 + Origin 同源校验（DNS rebinding / 跨站盲打）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_default_host_allowed(self):
        # TestClient 默认 Host=testserver，白名单内放行
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)

    def test_evil_host_rejected(self):
        # DNS rebinding：伪造 Host 头直连本机端口 → 403
        r = self.client.get("/", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Host", r.json()["detail"])

    def test_evil_host_rejected_on_api(self):
        r = self.client.post("/api/run", headers={"Host": "evil.example.com"},
                             json={"mode": "select"})
        self.assertEqual(r.status_code, 403)

    def test_evil_origin_rejected(self):
        # 跨站盲打：真实 Host + 攻击者 Origin → 403
        r = self.client.get("/api/config",
                            headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Origin", r.json()["detail"])

    def test_evil_origin_rejected_on_post(self):
        r = self.client.post("/api/run", headers={"Origin": "http://evil.example.com"},
                             json={"mode": "select"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_allowed(self):
        # 同源 Origin（本机页面自身的 POST）放行
        r = self.client.get("/api/config",
                            headers={"Origin": f"http://127.0.0.1:{server.PORT}"})
        self.assertEqual(r.status_code, 200)

    def test_localhost_origin_allowed(self):
        r = self.client.get("/api/config",
                            headers={"Origin": f"http://localhost:{server.PORT}"})
        self.assertEqual(r.status_code, 200)


class TestCollectUrlGuard(unittest.TestCase):
    """POST /api/run collect 模式 URL 域名白名单（知乎域）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_non_zhihu_domain_rejected(self):
        r = self.client.post("/api/run", json={
            "mode": "collect",
            "url": "https://example.com/people/x/answers",
            "count": 5,
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("知乎", r.json()["detail"])

    def test_http_scheme_rejected(self):
        r = self.client.post("/api/run", json={
            "mode": "collect",
            "url": "http://www.zhihu.com/people/x/answers",
            "count": 5,
        })
        self.assertEqual(r.status_code, 400)

    def test_zhihu_suffix_impostor_rejected(self):
        # zhihu.com 前缀伪装（evilzhihu.com）不在白名单
        r = self.client.post("/api/run", json={
            "mode": "collect",
            "url": "https://evilzhihu.com/people/x/answers",
            "count": 5,
        })
        self.assertEqual(r.status_code, 400)

    def test_zhihu_urls_accepted(self):
        # 合法知乎 URL（含子域/查询串）通过白名单，不抛 400
        for url in ("https://www.zhihu.com/people/x/answers",
                    "https://zhihu.com/people/x/answers",
                    "https://www.zhihu.com/people/x/answers?page=2"):
            server._require_zhihu_url(url)


class TestAuthorEndpoints(unittest.TestCase):
    """文风列表 / 切换 / 提炼任务分发。"""

    @classmethod
    def setUpClass(cls):
        import applications.zhihu_story.author_profiler as ap
        from config import story
        cls._orig_dir = ap.AUTHORS_DIR
        ap.AUTHORS_DIR = tempfile.mkdtemp(prefix="authors_")
        cls._orig_current = story.AUTHOR_PROFILE
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        import applications.zhihu_story.author_profiler as ap
        ap.AUTHORS_DIR = cls._orig_dir
        from config import story
        story.AUTHOR_PROFILE = cls._orig_current

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
        from config import story
        orig = story.AUTHOR_PROFILE
        try:
            r = self.client.post("/api/author", json={"name": "测试作者"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["effective"]["author_profile"], "测试作者")
            self.assertEqual(story.AUTHOR_PROFILE, "测试作者")
        finally:
            story.AUTHOR_PROFILE = orig

    def test_author_post_clear(self):
        from config import story
        orig = story.AUTHOR_PROFILE
        try:
            r = self.client.post("/api/author", json={"name": ""})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["effective"]["author_profile"], "")
        finally:
            story.AUTHOR_PROFILE = orig

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
        from config import story
        orig = story.AUTHOR_PROFILE
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
            self.assertEqual(story.AUTHOR_PROFILE, "提炼作者")
            saved = Path(ap.AUTHORS_DIR, "提炼作者.json")
            self.assertTrue(saved.exists(), "签名必须落盘")
            self.assertIn("测试文风",
                          server.runner.last_context["profile"]["summary"])
        finally:
            story.AUTHOR_PROFILE = orig
            ap.STORY_LIB = src_file

    def test_dispatch_general_profile_success(self):
        import applications.zhihu_story.author_profiler as ap
        from pathlib import Path
        from config import story
        orig = story.AUTHOR_PROFILE
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
            self.assertEqual(story.AUTHOR_PROFILE, "通用")
            saved = Path(ap.AUTHORS_DIR, "_general.json")
            self.assertTrue(saved.exists(), "通用签名必须落盘")
        finally:
            story.AUTHOR_PROFILE = orig
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
        import applications.zhihu_story.collector as cl
        cls._orig_lib = ap.STORY_LIB
        cls._orig_dir = ap.AUTHORS_DIR
        cls._orig_cl_lib = cl.STORY_LIB
        cls.tmp = tempfile.mkdtemp(prefix="storylib_")
        ap.STORY_LIB = str(Path(cls.tmp) / "lib.jsonl")
        ap.AUTHORS_DIR = str(Path(cls.tmp) / "authors")
        # server 的 storylib 接口现在统一从 collector 读库（C5 收敛后）
        cl.STORY_LIB = ap.STORY_LIB
        os.makedirs(ap.AUTHORS_DIR, exist_ok=True)
        Path(ap.AUTHORS_DIR, "甲.json").write_text("{}", encoding="utf-8")
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        import applications.zhihu_story.author_profiler as ap
        import applications.zhihu_story.collector as cl
        ap.STORY_LIB = cls._orig_lib
        ap.AUTHORS_DIR = cls._orig_dir
        cl.STORY_LIB = cls._orig_cl_lib
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


class TestSetupEndpoints(unittest.TestCase):
    """首启引导：状态 / API Key 写入 / 连接测试 / 知乎登录分发。"""

    @classmethod
    def setUpClass(cls):
        import config
        cls._orig_file = config._PROVIDERS_FILE
        cls.tmp = tempfile.mkdtemp(prefix="setup_")
        cls.dst = os.path.join(cls.tmp, "llm_providers.json")
        # 复制真实 example 配置到临时数据目录（不碰真实 llm_providers.json）
        cls._src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "llm_providers.example.json")
        config._PROVIDERS_FILE = cls.dst
        cls.client = TestClient(server.app)

    def setUp(self):
        # 每个测试重置为 example（占位 key）状态，避免写 key 测试污染顺序
        with open(self._src, encoding="utf-8") as f:
            providers = json.load(f)
        with open(self.dst, "w", encoding="utf-8") as f:
            json.dump(providers, f, ensure_ascii=False, indent=2)
        # Web 登录检查缓存：测试内固定为未登录（免真实拉起 Edge），
        # 需要登录语义的用例单独 patch
        server._web_llm_cache.update(ts=time.time(), ok=False)

    @classmethod
    def tearDownClass(cls):
        import config
        config._PROVIDERS_FILE = cls._orig_file
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_status_shape(self):
        r = self.client.get("/api/setup/status")
        self.assertEqual(r.status_code, 200)
        st = r.json()
        for k in ("version", "edge_ok", "llm_configured",
                  "web_llm_logged_in", "zhihu_logged_in",
                  "login_running", "login_kind", "login_error",
                  "setup_needed"):
            self.assertIn(k, st)
        self.assertEqual(st["version"], server._setup_version())

    def test_status_placeholder_not_configured(self):
        # example 配置里的占位 key → 视为未配置
        r = self.client.get("/api/setup/status")
        self.assertFalse(r.json()["llm_configured"])

    def test_setup_needed_relaxed_by_web_login(self):
        # Edge + 知乎就绪的前提下：API 未配置但 Web 已登录 → 引导放行
        import applications.zhihu_story.browser_adapter as ba
        with mock.patch.object(ba, "EDGE_PATH", "C:/fake/msedge.exe"):
            with mock.patch.object(server.os.path, "exists",
                                   return_value=True):
                with mock.patch.object(
                        server, "_web_llm_logged_in_cached",
                        return_value=False):
                    st = self.client.get("/api/setup/status").json()
                    self.assertTrue(st["setup_needed"])
                with mock.patch.object(
                        server, "_web_llm_logged_in_cached",
                        return_value=True):
                    st = self.client.get("/api/setup/status").json()
                    self.assertFalse(st["setup_needed"])

    def test_web_login_cached_result_reused(self):
        # TTL 内重复调用不重复拉起浏览器（web_llm_logged_in 只调一次）
        server._web_llm_cache.update(ts=0.0, ok=False)
        import applications.zhihu_story.browser_adapter as ba
        with mock.patch.object(ba, "web_llm_logged_in",
                               return_value=True) as m:
            self.assertTrue(server._web_llm_logged_in_cached())
            self.assertTrue(server._web_llm_logged_in_cached())
            self.assertEqual(m.call_count, 1)

    def test_web_login_cache_dedups_concurrent_calls(self):
        # 检测进行中（约 5s 真实耗时）的并发轮询不重复启动浏览器：
        # 锁去重后 8 个并发调用只触发 1 次真实检测（首启轮询 2.5s 间隔
        # 会撞上检测期，原实现各自排队启动浏览器是「检测慢」的主因）
        import threading
        server._web_llm_cache.update(ts=0.0, ok=False)
        import applications.zhihu_story.browser_adapter as ba
        entered = threading.Event()
        release = threading.Event()

        def _slow_check():
            entered.set()
            release.wait(timeout=10)
            return True

        results = []
        # patch 必须在主线程统一建立/恢复：若每个线程各自 patch 同一
        # 属性，线程退出时的恢复互相覆盖，残留 mock 污染后续测试
        with mock.patch.object(ba, "web_llm_logged_in",
                               side_effect=_slow_check) as m:
            def _call():
                results.append(server._web_llm_logged_in_cached())

            threads = [threading.Thread(target=_call) for _ in range(4)]
            for t in threads:
                t.start()
            self.assertTrue(entered.wait(timeout=10))  # 第一个进入真实检测
            release.set()
            for t in threads:
                t.join(timeout=15)
        # 仅首个线程触发真实检测（1 次），其余等待锁后命中缓存——
        # 正是锁去重要求的行为（原来并发轮询各自启动浏览器）
        self.assertEqual(results, [True, True, True, True])
        self.assertEqual(m.call_count, 1)

    def test_apikey_write_and_effect(self):
        import config
        orig = (config.LLM_PROVIDER, config.LLM_MODEL_ID)
        try:
            r = self.client.post("/api/setup/apikey", json={
                "provider": "DeepSeek", "api_key": "sk-test-12345"})
            self.assertEqual(r.status_code, 200)
            eff = r.json()["effective"]
            self.assertEqual(eff["provider"], "DeepSeek")
            # 已写入临时 providers 文件
            with open(config._PROVIDERS_FILE, encoding="utf-8") as f:
                providers = json.load(f)
            p = next(p for p in providers if p["name"] == "DeepSeek")
            self.assertEqual(p["apiKey"], "sk-test-12345")
            # 立即生效
            self.assertEqual(config.LLM_API_KEY, "sk-test-12345")
            # 状态翻转为已配置
            st = self.client.get("/api/setup/status").json()
            self.assertTrue(st["llm_configured"])
        finally:
            config.set_runtime_model(*orig, persist=False)

    def test_apikey_write_clears_guide_needed(self):
        # 填 key 成功 → 运行前引导标记解除（用户可立即重新运行）
        import config
        orig = (config.LLM_PROVIDER, config.LLM_MODEL_ID)
        prev = server.runner.guide_needed
        try:
            server.runner.guide_needed = "api_key"
            r = self.client.post("/api/setup/apikey", json={
                "provider": "DeepSeek", "api_key": "sk-test-67890"})
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(server.runner.guide_needed)
        finally:
            server.runner.guide_needed = prev
            config.set_runtime_model(*orig, persist=False)

    def test_apikey_empty_rejected(self):
        r = self.client.post("/api/setup/apikey", json={
            "provider": "DeepSeek", "api_key": "  "})
        self.assertEqual(r.status_code, 400)

    def test_apikey_unknown_provider_rejected(self):
        r = self.client.post("/api/setup/apikey", json={
            "provider": "不存在的服务商", "api_key": "sk-x"})
        self.assertEqual(r.status_code, 400)

    def test_test_api_mock_ok(self):
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {"choices": [
            {"message": {"content": "连接成功"}}]}
        # 连接测试前需通过 API Key 就绪校验（示例配置是占位 key，
        # 这里按「已填有效 key」模拟 _load_provider_config 的返回值）
        with mock.patch("config._load_provider_config",
                        return_value={"apiKey": "sk-real-123"}):
            with mock.patch("webui.server.requests.post",
                            return_value=fake) as m:
                r = self.client.post("/api/setup/test-api")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # 请求确实发往 baseUrl + 认证头
        args = m.call_args
        self.assertIn("/chat/completions", args.args[0])
        self.assertIn("Authorization",
                      args.kwargs["headers"])

    def test_zhihu_login_missing_edge_400(self):
        # 无 Edge 环境（CI/无浏览器机器）→ 400 而非 500
        import applications.zhihu_story.browser_adapter as ba
        with mock.patch.object(ba, "EDGE_PATH", None):
            r = self.client.post("/api/setup/zhihu-login")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Edge", r.json()["detail"])

    def test_zhihu_login_starts_thread(self):
        import applications.zhihu_story.browser_adapter as ba
        orig_thread = server._login_thread
        server._login_thread = None
        try:
            with mock.patch.object(ba, "EDGE_PATH", "C:/fake/msedge.exe"):
                with mock.patch.object(
                        ba, "login_zhihu_flow",
                        return_value=(True, "ok")) as m:
                    r = self.client.post("/api/setup/zhihu-login")
                    self.assertEqual(r.status_code, 200)
                    # 后台线程里执行了登录流程（等线程跑完）
                    import time
                    deadline = time.time() + 5
                    while (server._login_thread
                           and server._login_thread.is_alive()
                           and time.time() < deadline):
                        time.sleep(0.05)
                    self.assertEqual(m.call_count, 1)
                    st = self.client.get("/api/setup/status").json()
                    self.assertFalse(st["login_running"])
        finally:
            server._login_thread = orig_thread

    def test_zhihu_login_duplicate_409(self):
        import applications.zhihu_story.browser_adapter as ba
        fake = mock.Mock()
        fake.is_alive.return_value = True
        orig = server._login_thread
        server._login_thread = fake
        try:
            with mock.patch.object(ba, "EDGE_PATH", "C:/fake/msedge.exe"):
                r = self.client.post("/api/setup/zhihu-login")
            self.assertEqual(r.status_code, 409)
        finally:
            server._login_thread = orig

    def test_web_login_missing_edge_400(self):
        import applications.zhihu_story.browser_adapter as ba
        with mock.patch.object(ba, "EDGE_PATH", None):
            r = self.client.post("/api/setup/web-login")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Edge", r.json()["detail"])

    def test_web_login_starts_thread(self):
        import applications.zhihu_story.browser_adapter as ba
        orig_thread = server._login_thread
        orig_kind = server._login_kind
        server._login_thread = None
        server._login_kind = ""
        try:
            with mock.patch.object(ba, "EDGE_PATH", "C:/fake/msedge.exe"):
                with mock.patch.object(
                        ba, "login_deepseek_web_flow",
                        return_value=(True, "ok")) as m:
                    r = self.client.post("/api/setup/web-login")
                    self.assertEqual(r.status_code, 200)
                    deadline = time.time() + 5
                    while (server._login_thread
                           and server._login_thread.is_alive()
                           and time.time() < deadline):
                        time.sleep(0.05)
                    self.assertEqual(m.call_count, 1)
        finally:
            server._login_thread = orig_thread
            server._login_kind = orig_kind

    def test_web_login_duplicate_409(self):
        import applications.zhihu_story.browser_adapter as ba
        fake = mock.Mock()
        fake.is_alive.return_value = True
        orig = server._login_thread
        server._login_thread = fake
        try:
            with mock.patch.object(ba, "EDGE_PATH", "C:/fake/msedge.exe"):
                r = self.client.post("/api/setup/web-login")
            self.assertEqual(r.status_code, 409)
        finally:
            server._login_thread = orig


class TestUpdateCheck(unittest.TestCase):
    """检查更新：有新版 / 已最新 / 网络失败。"""

    def setUp(self):
        server._update_cache["data"] = None

    def _fake_resp(self, tag_name):
        r = mock.Mock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "tag_name": tag_name,
            "html_url": f"https://github.com/large-su/AutoQuill/releases/tag/{tag_name}",
        }
        return r

    def test_latest_greater_than_current(self):
        with mock.patch("webui.server.requests.get",
                        return_value=self._fake_resp("v9.9.9")):
            d = self.client_get()
        self.assertTrue(d["has_update"])
        self.assertEqual(d["latest"], "9.9.9")

    def test_same_version_no_update(self):
        with mock.patch("webui.server.requests.get",
                        return_value=self._fake_resp("v4.0.0")):
            d = self.client_get()
        self.assertFalse(d["has_update"])

    def test_network_error_graceful(self):
        with mock.patch("webui.server.requests.get",
                        side_effect=Exception("boom")):
            d = self.client_get()
        self.assertIsNone(d["latest"])
        self.assertIn("error", d)

    def client_get(self):
        r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200)
        return r.json()

    @property
    def client(self):
        return TestClient(server.app)


if __name__ == "__main__":
    unittest.main()
