# ============================================================
# tests/test_web_session_lifecycle.py — 网页会话生命周期回归
#
# 2026-09 新增：单条完整链路只开一个会话 + 完成后删除会话
# （用户实测生成多了被 DeepSeek 警告/封号，需减少会话堆积）。
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import unittest
from unittest import mock

from fastapi.testclient import TestClient

import web_drivers
from web_drivers.base import WebLLMDriver


class FakePage:
    """假页面：只实现会话生命周期用到的接口。"""

    def __init__(self, url="https://chat.deepseek.com/"):
        self.url = url
        self.open = True

    def is_closed(self):
        return not self.open

    def close(self):
        self.open = False


class RecordDriver(WebLLMDriver):
    """记录调用序列的测试驱动（不碰浏览器）。"""

    def __init__(self, config=None):
        super().__init__(config or {"max_wait": 60, "stable_count": 2})
        self.calls = []
        self._page = FakePage()
        self._delete_result = False
        self._wait_result = True

    # ---- 生命周期桩 ----
    def open_session(self):
        self.calls.append("open_session")
        return self

    def setup(self):
        self.calls.append("setup")
        return self

    def input(self, prompt):
        self.calls.append(f"input:{prompt}")
        return self

    def send(self):
        self.calls.append("send")
        return self

    def wait_complete(self, max_wait=None):
        self.calls.append("wait")
        return self._wait_result

    def read_result(self):
        self.calls.append("read")
        return "故事正文"

    def _delete_current_session_impl(self):
        return self._delete_result


class TestBaseSessionLifecycle(unittest.TestCase):
    """base.generate 会话复用 / 损坏自动新开 / 归属删除。"""

    def _driver(self):
        return RecordDriver()

    def test_single_pipeline_reuses_one_session(self):
        drv = self._driver()
        drv.generate("第一个问题？")
        drv.generate("第二个问题（重试修正）？")
        # 只有首次 new_chat/open_session：一次完整链路只开一个会话
        self.assertEqual(drv.calls.count("open_session"), 1)
        # 两次提问各 input+send（第二次走 continue_chat，不开新会话）
        self.assertEqual(drv.calls.count("send"), 2)
        self.assertEqual(drv.calls.count("input:第一个问题？"), 1)
        self.assertEqual(drv.calls.count("input:第二个问题（重试修正）？"), 1)
        self.assertTrue(drv._session_owned)

    def test_broken_session_opens_new(self):
        drv = self._driver()
        drv.generate("第一次")
        self.assertEqual(drv.calls.count("open_session"), 1)
        drv._mark_session_broken()
        drv.generate("第二次")
        # 会话坏了才开新会话（open_session 第二次）
        self.assertEqual(drv.calls.count("open_session"), 2)

    def test_generate_timeout_marks_broken(self):
        drv = self._driver()
        drv._wait_result = False
        drv.generate("超时任务")
        self.assertTrue(drv._session_broken)

    def test_delete_only_owned_session(self):
        drv = self._driver()
        # 未使用任何会话 → 不触发删除实现（避免误删用户已有会话）
        self.assertFalse(drv.delete_current_session())
        self.assertFalse(drv._delete_result)

        drv = self._driver()
        drv.generate("生成一篇故事")
        self.assertTrue(drv._session_owned)
        drv._delete_result = True
        self.assertTrue(drv.delete_current_session())
        # 删除后归属清空，再次删除不再触发
        self.assertFalse(drv._session_owned)
        self.assertFalse(drv.delete_current_session())

    def test_delete_failure_does_not_raise(self):
        drv = self._driver()
        drv.generate("生成一篇故事")
        drv._delete_result = False
        self.assertFalse(drv.delete_current_session())  # 只记日志

    def test_delete_when_page_closed(self):
        drv = self._driver()
        drv.generate("生成一篇故事")
        drv._page.open = False
        self.assertFalse(drv.delete_current_session())


class TestResetDriverDeletes(unittest.TestCase):
    """web_drivers.reset_driver：先删会话再关页。"""

    def test_reset_driver_deletes_singleton_session(self):
        class FakeSingleton:
            def __init__(self):
                self.deleted = 0
                self.closed = 0

            def delete_current_session(self):
                self.deleted += 1

            def close_session(self):
                self.closed += 1

        orig = web_drivers._driver_instance
        fake = FakeSingleton()
        web_drivers._driver_instance = fake
        try:
            web_drivers.reset_driver()
            self.assertEqual(fake.deleted, 1)
            self.assertEqual(fake.closed, 1)
            self.assertIsNone(web_drivers._driver_instance)
        finally:
            web_drivers._driver_instance = orig

    def test_reset_driver_skip_delete_when_flag_off(self):
        class FakeSingleton:
            def __init__(self):
                self.deleted = 0
                self.closed = 0

            def delete_current_session(self):
                self.deleted += 1

            def close_session(self):
                self.closed += 1

        orig = web_drivers._driver_instance
        fake = FakeSingleton()
        web_drivers._driver_instance = fake
        try:
            web_drivers.reset_driver(delete_session=False)
            self.assertEqual(fake.deleted, 0)
            self.assertEqual(fake.closed, 1)
        finally:
            web_drivers._driver_instance = orig


class TestMultiWebDriverFramework(unittest.TestCase):
    """多网页版大模型框架：注册表 / 分发器 / 运行时切换 / 删除机制。"""

    def _src(self, rel_path):
        with open(rel_path, encoding="utf-8") as f:
            return f.read()

    def test_registry_has_deepseek_and_doubao(self):
        self.assertIn("DeepSeek", web_drivers._DRIVER_REGISTRY)
        self.assertIn("Doubao", web_drivers._DRIVER_REGISTRY)
        self.assertTrue(web_drivers._impl_available("Doubao"))

    def test_login_dispatchers_exist(self):
        self.assertTrue(callable(web_drivers.web_llm_logged_in))
        self.assertTrue(callable(web_drivers.login_web_flow))

    def test_deepseek_and_doubao_export_login_flow(self):
        import web_drivers.deepseek as ds
        import web_drivers.doubao as db
        self.assertTrue(callable(ds.login_web_flow))
        self.assertTrue(callable(ds.web_llm_logged_in))
        self.assertTrue(callable(db.login_web_flow))
        self.assertTrue(callable(db.web_llm_logged_in))
        self.assertTrue(hasattr(db, "DoubaoDriver"))

    def test_deepseek_delete_machinery(self):
        import web_drivers.deepseek as ds
        # 2026-09 改版：删除端点换成 chat_session 命名空间 + Bearer 鉴权
        from web_drivers.deepseek import DeepSeekDriver as D
        self.assertEqual(D._DELETE_API_PATH, "/api/v0/chat_session/delete")
        self.assertTrue(hasattr(D, "_detect_session_id"))
        self.assertTrue(hasattr(D, "_delete_current_session_impl"))
        self.assertTrue(hasattr(D, "_auth_token"))
        self.assertTrue(hasattr(D, "_delete_session_via_dom"))
        src = self._src("web_drivers/deepseek.py")
        # 完成后必须能走到删除逻辑；删除实现不阻断生成
        self.assertIn("delete_current_session", src)
        self.assertIn("_DELETE_API_PATH", src)

    def test_doubao_uses_only_quick_model(self):
        src = self._src("web_drivers/doubao.py")
        self.assertIn("豆包快速", src)
        # 只校验不折腾：默认即快速，探到未选中才点
        self.assertIn("_model_selected", src)
        self.assertIn("_click_text", src)

    def test_doubao_heartbeat_matches_webui_progress_bar(self):
        # ★ 2026-09-09：豆包旧心跳「豆包生成中…」不匹配 log_capture 的
        # _PROGRESS_RE（要求「故事生成中…」）→ 前端进度条根本不出现。
        # 进度心跳文案必须与 webui/log_capture 同源（与 DeepSeek 一致）。
        import webui.log_capture as lc
        src = self._src("web_drivers/doubao.py")
        self.assertTrue("故事生成中… 已生成 %d 字" in src, "心跳文案缺失")
        self.assertFalse("豆包生成中…" in src, "旧心跳文案仍在（不会出进度条）")
        ev, payload = lc.parse_line(
            "2026-09-09 22:30:31,867 [INFO] 故事生成中… 已生成 4961 字")
        self.assertEqual(ev, "progress")
        self.assertEqual(payload["chars"], 4961)

    def test_doubao_phase_lines_reach_progress_area(self):
        # 阶段提示（等待响应 / 写作任务交付）走「任务进度：」→ 前端不定进度动画
        import webui.log_capture as lc
        src = self._src("web_drivers/doubao.py")
        for phase in ("等待豆包响应", "豆包正在用写作任务撰写正文",
                      "正文在交付卡片里", "等豆包完成上一轮生成后继续"):
            self.assertIn(phase, src, phase)
        ev, payload = lc.parse_line(
            "2026-09-09 22:30:31,867 [INFO] 任务进度：等待豆包响应…")
        self.assertEqual(ev, "progress")
        self.assertTrue(payload.get("task"))
        self.assertIsNone(payload.get("pct"))

    def test_config_web_driver_switch(self):
        import config
        orig = config.WEB_DRIVER_NAME
        try:
            eff = config.set_runtime_web_driver("Doubao", persist=False)
            self.assertEqual(eff["web_driver"], "Doubao")
            self.assertEqual(config.WEB_DRIVER_NAME, "Doubao")
            with self.assertRaises(ValueError):
                config.set_runtime_web_driver("不存在的驱动", persist=False)
        finally:
            config.set_runtime_web_driver(orig, persist=False)

    def test_webui_web_drivers_endpoint(self):
        from webui import server
        client = TestClient(server.app)
        r = client.get("/api/web-drivers")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("current", data)
        self.assertIn("drivers", data)
        names = [d["name"] for d in data["drivers"]]
        self.assertIn("DeepSeek", names)
        self.assertIn("Doubao", names)

    def test_mode_get_exposes_web_driver(self):
        from webui import server
        client = TestClient(server.app)
        r = client.get("/api/mode")
        self.assertEqual(r.status_code, 200)
        self.assertIn("web_driver", r.json())


class FakePageUrl:
    """只有 url 属性的假页面（会话 ID 探测用）。"""

    def __init__(self, url):
        self.url = url


class TestDoubaoDriverUnits(unittest.TestCase):
    """豆包驱动实测结论的代码化：md-box 提取 / /chat/{id} 会话ID / 删除钩子。"""

    def _drv(self):
        from web_drivers.doubao import DoubaoDriver
        return DoubaoDriver({"max_wait": 60})

    def test_detect_session_id_from_chat_path(self):
        d = self._drv()
        d._page_instance = lambda: FakePageUrl(
            "https://www.doubao.com/chat/38440412112955394")
        self.assertEqual(d._detect_session_id(), "38440412112955394")

    def test_extract_reply_text_uses_msg_state(self):
        d = self._drv()
        d._safe_evaluate = lambda js, *a, **k: {
            "user_count": 2, "answer_count": 2,
            "text": "这是回复正文", "card": False}
        self.assertEqual(d._extract_reply_text(), "这是回复正文")

    def test_extract_reply_text_skips_suggestion_and_user_prompt(self):
        # ★ 2026-09-08 根因回归：豆包回复后的建议追问条也是 md-box-root，
        # 用户刚发的 prompt 也是——正文必须取「最后一条用户消息之后的
        # 第一条助手消息」，不能取「最后一个 md-box」。
        import web_drivers.doubao as db
        js = db._MSG_STATE_JS
        self.assertIn("send-msg", js)                 # 用户消息判据
        self.assertIn("compareDocumentPosition", js)  # 卡片限定在本次提问之后
        self.assertIn("slice(nodes.indexOf(lastUser) + 1)", js)
        with open("web_drivers/doubao.py", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("nodes[nodes.length - 1]", src)  # 旧「取最后一条」已移除

    def test_extract_uses_innertext_and_strips_suggestion(self):
        # ★ 2026-09-08 E2E：豆包每段是独立 block，textContent 会把整篇故事
        # 压成一行（段落/章节行全丢）；末尾还会塞一条「需要我帮你…吗？」追问
        import web_drivers.doubao as db
        self.assertIn("innerText", db._MSG_STATE_JS)
        text = "故事正文第一段。\n故事正文第二段。\n需要我帮你微调每章甜度吗？"
        out = db._strip_trailing_suggestion(text)
        self.assertNotIn("需要我帮你", out)
        self.assertTrue(out.endswith("故事正文第二段。"))
        # 正文内部的类似句子不动（只剥末尾）
        inner = "需要我帮你吗？\n故事正文。"
        self.assertEqual(db._strip_trailing_suggestion(inner), inner)

    def test_markdown_rebuild_restores_chapter_headings(self):
        # ★ 2026-09-09 根因回归：豆包把 markdown 渲染成 DOM（## **N** → <h2>N</h2>），
        # 只取 innerText 会丢掉标题语法 → 格式校验「章节 0 个」扣 4 分判废
        # （真实全链路 3/10）。逐块重建后同一篇实测 10/10 通过。
        import web_drivers.doubao as db
        js = db._MSG_STATE_JS
        self.assertIn("toMarkdown", js)
        self.assertIn("H[1-6]", js)     # 识别标题元素
        self.assertIn("repeat", js)     # hN → '#'*N → ## **N**
        self.assertIn("parts.join", js)

    def test_msg_state_empty_on_probe_failure(self):
        d = self._drv()
        d._safe_evaluate = lambda js, *a, **k: None
        self.assertEqual(d._msg_state(), {})
        self.assertEqual(d._extract_reply_text(), "")
        self.assertFalse(d._delivery_card_present())

    def test_send_marks_broken_when_not_accepted(self):
        # ★ 2026-09-08 根因回归：上一轮写作任务在跑时豆包忽略新输入，
        # 重试的 prompt 根本没发出去 → 必须抛错，不能静默把旧回复当结果。
        d = self._drv()
        d._msg_state = lambda: {"user_count": 3, "input_len": 20}
        d._wait_send_ready = lambda timeout=None: True
        d._wait_sent = lambda before, timeout, before_input=-1: False
        broken = []
        d._mark_session_broken = lambda: broken.append(True)

        class _Locator:
            @staticmethod
            def count():
                return 0

        class _Page:
            class keyboard:
                @staticmethod
                def press(k):
                    return None

            @staticmethod
            def locator(sel):
                return _Locator()

        d._page_instance = lambda: _Page()
        with self.assertRaises(RuntimeError):
            d.send(accept_timeout=1)
        self.assertEqual(broken, [True])

    def test_send_ok_when_user_message_appears(self):
        d = self._drv()
        d._msg_state = lambda: {"user_count": 1, "input_len": 0}
        d._wait_send_ready = lambda timeout=None: True
        d._wait_sent = lambda before, timeout, before_input=-1: True

        class _Page:
            class keyboard:
                @staticmethod
                def press(k):
                    return None

        d._page_instance = lambda: _Page()
        self.assertIs(d.send(), d)

    def test_send_waits_for_idle_before_enter(self):
        # ★ 2026-09-09 根因回归：生成期间发送按钮 disabled、新消息被吞 →
        # 发送前必须先等 UI 空闲
        import web_drivers.doubao as db
        with open("web_drivers/doubao.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self._wait_send_ready()", src)
        self.assertIn("flow-end-msg-send", src)
        d = self._drv()
        waits = []
        d._wait_send_ready = lambda timeout=None: waits.append(timeout) or True
        d._msg_state = lambda: {"user_count": 0, "input_len": 5}
        d._wait_sent = lambda before, timeout, before_input=-1: True

        class _Page:
            class keyboard:
                @staticmethod
                def press(k):
                    return None

        d._page_instance = lambda: _Page()
        d.send()
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0], None)  # 用默认窗口 _SEND_READY_SEC

    def test_generating_from_send_button_disabled(self):
        # 生成中判据：发送按钮 disabled（空闲时无论输入框空否都 enabled）
        d = self._drv()
        d._msg_state = lambda: {"generating": True}
        self.assertTrue(d._generating())
        d._msg_state = lambda: {"generating": False}
        self.assertFalse(d._generating())
        d._msg_state = lambda: {}
        self.assertFalse(d._generating())  # 探测失败 → 退回文本稳定判定

    def test_wait_send_ready_returns_when_idle(self):
        d = self._drv()
        seq = {"n": 0}

        def _gen():
            seq["n"] += 1
            return seq["n"] < 2   # 第一次仍生成中，第二次空闲

        d._generating = _gen

        class _Page:
            @staticmethod
            def wait_for_timeout(ms):
                return None

        d._page_instance = lambda: _Page()
        self.assertTrue(d._wait_send_ready(timeout=5))

    def test_wait_sent_accepts_cleared_input(self):
        # 长对话里用户消息可能被虚拟化/延迟渲染 → 输入框清空也算已发送
        d = self._drv()
        d._msg_state = lambda: {"user_count": 2, "input_len": 0}
        self.assertTrue(d._wait_sent(2, 1, before_input_len=20))
        # 输入框本来就没有内容时不算（避免 fill 失败被误判为已发送）
        self.assertFalse(d._wait_sent(2, 1, before_input_len=0))

    def test_module_exports_login_flow_and_delete_endpoints(self):
        import web_drivers.doubao as db
        self.assertTrue(db.DoubaoDriver._DELETE_API_ENDPOINTS)
        self.assertTrue(callable(db.web_llm_logged_in))
        self.assertTrue(callable(db.login_web_flow))

    def test_card_delivery_fallback_asks_inline(self):
        # 豆包卡片式交付（flow-product-card，对话只有引言）→ 补问直接输出全文
        d = self._drv()
        calls = []
        seq = {"n": 0}

        def _extract():
            seq["n"] += 1
            return ("我" * 400) if seq["n"] >= 2 else "我将以细腻的视角记录"

        d._extract_reply_text = _extract
        d._wait_for_delivery_card = lambda timeout=None: True
        d._ask_inline = lambda timeout=None: (calls.append("ask"), True)[1]
        d.wait_complete = lambda max_wait=None: True
        d._after_wait_before_read()
        self.assertEqual(len(calls), 1)  # 补问一次后读到全文即停
        self.assertTrue(getattr(d, "_recovery_ran", False))

    def test_inline_ask_prompt_asks_no_card_delivery(self):
        # 补问措辞必须明确「不要卡片/文档/任务交付界面」（实测有效措辞）
        import web_drivers.doubao as db
        self.assertIn("直接输出全文正文", db._INLINE_ASK_PROMPT)
        self.assertIn("不要使用卡片", db._INLINE_ASK_PROMPT)

    def test_card_fallback_skipped_when_full_text(self):
        # 已拿到完整正文（≥300字）时绝不补问（避免每次生成多打一轮）
        d = self._drv()
        d._extract_reply_text = lambda: "正" * 400
        d._wait_for_delivery_card = lambda timeout=None: True

        def _should_not_call(timeout=None):
            raise AssertionError("不应触发补问")

        d._ask_inline = _should_not_call
        d._after_wait_before_read()  # 不应抛错也不应补问

    def test_card_fallback_skipped_without_card(self):
        # 没有交付卡片（如作者风格分析等短回复）→ 绝不补问
        d = self._drv()
        d._extract_reply_text = lambda: "这段是短说明文字，不是 JSON。"
        d._wait_for_delivery_card = lambda timeout=None: False

        def _should_not_call(timeout=None):
            raise AssertionError("无交付卡片时不应补问")

        d._ask_inline = _should_not_call
        d._after_wait_before_read()

    def test_card_fallback_skipped_for_structured_json(self):
        # 选题筛选/评分/原创审核返回 JSON，本来就该短 → 连卡片等待都跳过
        import web_drivers.doubao as db
        self.assertTrue(db._looks_structured('{"items":[]}'))
        self.assertTrue(db._looks_structured("  [1, 2]"))
        self.assertFalse(db._looks_structured("我将以细腻的视角完成这篇故事"))
        d = self._drv()
        d._extract_reply_text = lambda: '{"items":[{"index":1,"keep":true}]}'

        def _no_wait(timeout=None):
            raise AssertionError("结构化回复不应等卡片")

        d._wait_for_delivery_card = _no_wait
        d._ask_inline = _no_wait
        d._after_wait_before_read()

    def test_card_fallback_retries_when_still_short(self):
        # 补问后若豆包仍给卡片（还是短文本）→ 第二次补问（共 2 次）
        d = self._drv()
        d._wait_for_delivery_card = lambda timeout=None: True
        seq = {"n": 0}

        def _extract():
            seq["n"] += 1
            # 首次读 60 字引言；补问后仍短；第三次读到全文
            if seq["n"] >= 3:
                return "故" * 500
            return "我将以细腻的视角完成这篇故事"

        d._extract_reply_text = _extract
        calls = []
        d._ask_inline = lambda timeout=None: (calls.append("ask"), True)[1]
        d.wait_complete = lambda max_wait=None: True
        d._after_wait_before_read()
        self.assertEqual(len(calls), 2)  # 短则再问一次，第三次读到全文即停

    def test_read_result_runs_recovery_once_for_parallel_path(self):
        # 并行链路直接调 read_result（不走 _after_wait_before_read）→ 兜底
        d = self._drv()
        calls = []
        d._recover_card_delivery = lambda: calls.append("recover")
        d._extract_reply_text = lambda: "正" * 500
        d.read_result()
        self.assertEqual(calls, ["recover"])
        # 串行链路已兜底过 → read_result 不再重复补问
        d._recovery_ran = True
        d.read_result()
        self.assertEqual(calls, ["recover"])

    def test_wait_complete_short_circuits_on_card(self):
        # 对话无正文 + 检测到交付卡片 → 不再空等到 max_wait
        import web_drivers.doubao as db
        with open("web_drivers/doubao.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_delivery_card_present()", src)
        self.assertIn("对话无正文且检测到交付卡片", src)
        self.assertIn("flow-product-card", src)
        self.assertTrue(db._CARD_SELECTORS)

    def test_reply_tail_heading_flags_half_capture(self):
        # ★ 2026-09-19 真实事故回归：豆包一轮 3 次尝试都只读到「引言 + 第 1 章
        # 正文 + ## **2**…## **6** 空壳标题」的半截稿（真实存盘
        # output/story_20260919_092051.md 只有 597 字，格式校验 5/10 判废）
        import web_drivers.doubao as db
        half = chr(10).join([
            "我彻底消失的那天，他在全城媒体面前，官宣了和别人的终身婚约。",
            "", "## **1**", "", "宴会厅的水晶灯亮得刺眼。", "",
            "## **2**", "", "## **3**", "", "## **4**", "",
            "## **5**", "", "## **6**"])
        self.assertTrue(db.reply_tail_is_chapter_heading(half))
        self.assertTrue(db.reply_tail_is_chapter_heading("第 6 章"))
        # 写完的稿子末尾必然是正文句子 / 【ps】收尾
        done = chr(10).join([
            "## **6**", "", "我说：「不欠。」",
            "【ps：后来我把那个碗带回了家。】"])
        self.assertFalse(db.reply_tail_is_chapter_heading(done))
        # 结构化回复（选题筛选/评分/审核）与空串不受影响
        self.assertFalse(db.reply_tail_is_chapter_heading('{"index": 1}'))
        self.assertFalse(db.reply_tail_is_chapter_heading(""))
        # 正文里的普通数字行不算章节标题（不误杀）
        self.assertFalse(db.reply_tail_is_chapter_heading("3 个男人站在门口。"))

    def _drive_wait_complete(self, text, max_wait=1):
        """用假正文跑 wait_complete（关掉真实等待窗口，秒回）。"""
        import config
        import web_drivers.doubao as db
        d = self._drv()
        d._extract_reply_text = lambda: text
        d._generating = lambda: False
        d._msg_state = lambda: {}      # 假页面没有 evaluate，别触发探测噪声

        class _Page:
            @staticmethod
            def wait_for_timeout(ms):
                return None

        d._page_instance = lambda: _Page()
        cfg = config.WEB_DRIVERS[config.WEB_DRIVER_NAME]
        saved = {k: cfg.get(k) for k in ("poll_interval", "stable_count")}
        saved_readback = db._READBACK_MS
        db._READBACK_MS = 1
        cfg["poll_interval"] = 0
        cfg["stable_count"] = 2
        try:
            return d.wait_complete(max_wait=max_wait)
        finally:
            db._READBACK_MS = saved_readback
            cfg.update(saved)

    def test_wait_complete_refuses_half_capture(self):
        # ★ 事故回归：长度稳定 + 重读一致 + UI 空闲三条全成立时，原实现
        # 直接判完成并把半截稿交给下游。现在末尾停在章节标题 → 不判完成，
        # 等到超时返回 False（上层按「未完成」重试，而不是存 597 字废稿）
        half = chr(10).join(["引言第一段。", "", "## **1**", "",
                             "第一章正文。", "", "## **2**", "", "## **3**"])
        with self.assertLogs("web_drivers.doubao", level="WARNING") as cm:
            self.assertFalse(self._drive_wait_complete(half))
        self.assertTrue(any("半截稿" in m for m in cm.output), cm.output)

    def test_wait_complete_accepts_finished_story(self):
        done = chr(10).join(["引言第一段。", "", "## **1**", "",
                             "第一章正文。", "", "我说：「不欠。」"])
        self.assertTrue(self._drive_wait_complete(done, max_wait=2))

    def test_web_driver_persisted_to_state(self):
        import config
        import json
        import os
        import tempfile
        orig = config._WEBUI_MODEL_FILE
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False)
        tmp.close()
        try:
            config._WEBUI_MODEL_FILE = tmp.name
            with open(tmp.name, "w", encoding="utf-8") as f:
                json.dump({"mode": "web"}, f)
            config.set_runtime_web_driver("Doubao", persist=True)
            with open(tmp.name, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("web_driver"), "Doubao")
        finally:
            os.unlink(tmp.name)
            config._WEBUI_MODEL_FILE = orig
            config.set_runtime_web_driver("DeepSeek", persist=False)


class FakeSlotDriver:
    """parallel teardown 用的假 slot 驱动。"""

    def __init__(self):
        self.deleted = 0
        self.closed = 0

    def delete_current_session(self):
        self.deleted += 1

    def close_session(self):
        self.closed += 1


class TestParallelTeardownDeletes(unittest.TestCase):
    """并行调度 teardown 时逐个删除会话。"""

    def test_teardown_deletes_each_slot(self):
        from web_drivers.parallel import ParallelWebRunner, SlotState
        runner = ParallelWebRunner(num_slots=2)
        d0, d1 = FakeSlotDriver(), FakeSlotDriver()
        runner.slots = [SlotState(0, d0), SlotState(1, d1)]
        runner.teardown()
        self.assertEqual(d0.deleted, 1)
        self.assertEqual(d1.deleted, 1)
        self.assertEqual(d0.closed, 1)
        self.assertEqual(d1.closed, 1)


if __name__ == "__main__":
    unittest.main()
