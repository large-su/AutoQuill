# ============================================================
# tests/test_web_drivers_dom.py — Web 驱动 DOM 化回归测试
#
# 核心约束：web_drivers 重写后必须走 Playwright DOM 语义接口，
# 不得出现 pyautogui / pyperclip / OCR / 坐标主通道调用。
# （浏览器内的真实行为由 --probe 实测，这里防 Python 侧退化。）
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import unittest


class TestWebDriversDomOnly(unittest.TestCase):
    """web_drivers 必须与物理鼠标/坐标/OCR 解绑。"""

    def _src(self, rel_path):
        with open(rel_path, encoding="utf-8") as f:
            return f.read()

    def test_no_legacy_automation_in_dom_base(self):
        src = self._src("web_drivers/base.py")
        for banned in ("pyautogui", "pyperclip", "ocr_utils",
                       "find_text_on_screen", "numpy"):
            self.assertNotIn(banned, src, banned)

    def test_no_legacy_automation_in_deepseek(self):
        src = self._src("web_drivers/deepseek.py")
        for banned in ("pyautogui", "pyperclip", "ocr_utils",
                       "find_text_on_screen", "numpy"):
            self.assertNotIn(banned, src, banned)

    def test_no_parallel_runner_left(self):
        # 旧 OCR 并行 runner 已随 OCR 栈移除；新 DOM 调度器是 parallel.py
        src = self._src("workflows/base.py")
        self.assertNotIn("parallel_runner", src)
        import os
        self.assertFalse(os.path.exists("web_drivers/parallel_runner.py"))

    def test_factory_no_aizex_has_create_driver(self):
        src = self._src("web_drivers/__init__.py")
        self.assertIn("DeepSeek", src)
        # 注册表只留 DeepSeek；旧驱动（Aizex）不进工厂
        self.assertIn('"DeepSeek"', src)
        self.assertNotIn("AizexDriver", src)
        # create_driver 恢复（并行调度每 slot 一个实例）
        self.assertIn("create_driver", src)
        import web_drivers
        d1 = web_drivers.create_driver()
        d2 = web_drivers.create_driver()
        try:
            self.assertIsNot(d1, d2, "create_driver 每次应返回新实例")
            self.assertIsNot(d1, web_drivers.get_driver(),
                             "create_driver 不应污染单例")
        finally:
            web_drivers.reset_driver()


class TestWebDriversDomSemantics(unittest.TestCase):
    """DOM 驱动语义接线。"""

    def _src(self, rel_path):
        with open(rel_path, encoding="utf-8") as f:
            return f.read()

    def test_base_provides_safe_evaluate_with_cancel(self):
        src = self._src("web_drivers/base.py")
        self.assertIn("_safe_evaluate", src)
        self.assertIn("_check_cancel", src)     # 取消检查点

    def test_pool_owns_bounded_evaluate_implementation(self):
        # 有界交互唯一实现下沉 browser_pool（base/browser_adapter 委托）
        src = self._src("web_drivers/browser_pool.py")
        self.assertIn("safe_evaluate", src)
        self.assertIn("Promise.race", src)      # 自限时哨兵
        self.assertIn("__aq_timeout__", src)
        self.assertIn("_check_cancel", src)     # 取消检查点

    def test_base_uses_shared_browser_context(self):
        src = self._src("web_drivers/base.py")
        self.assertIn("get_browser", src)       # 复用知乎共享浏览器
        self.assertIn("context.new_page", src)  # 独立页面不碰知乎流程

    def test_base_probe_selectors_candidates(self):
        # selector 探测：前端改版时扩展候选列表即可
        src = self._src("web_drivers/base.py")
        self.assertIn("_probe_selectors", src)
        self.assertIn("querySelector", src)

    def test_deepseek_input_uses_fill(self):
        src = self._src("web_drivers/deepseek.py")
        self.assertIn(".fill(prompt, timeout=120000)", src)
        # textarea 纯文本，非剪贴板；超时放宽因 SPA 逐行处理大 prompt
        self.assertIn("_INPUT_SELECTORS", src)

    def test_deepseek_selector_candidate_lists(self):
        import web_drivers.deepseek as d
        for name in ("_INPUT_SELECTORS", "_SEND_SELECTORS",
                     "_STOP_SELECTORS", "_RESULT_SELECTORS"):
            cands = getattr(d, name)
            self.assertIsInstance(cands, tuple, name)
            self.assertGreaterEqual(len(cands), 2,
                                    f"{name} 至少 2 个候选（前端改版兜底）")

    def test_deepseek_progress_heartbeat_matches_webui(self):
        # 进度心跳文案必须与 webui/log_capture 识别一致（进度条同源）
        import web_drivers.deepseek as d
        import webui.log_capture as lc
        dsrc = self._src("web_drivers/deepseek.py")
        self.assertIn("故事生成中", dsrc)   # 生成阶段心跳
        self.assertIn("模型思考中", dsrc)   # 思考阶段心跳
        self.assertIn("think_len", dsrc)   # 思考容器长度（_read_probe 返回）
        # 2026-09 改版：正文长度只在 div.ds-message 里按正文容器
        # （ds-assistant-message-main-content）取，思考容器另算，
        # 不再用 ds-markdown 兜底把思考文本计入正文
        self.assertIn("ds-assistant-message-main-content", dsrc)
        self.assertIn("ds-think-content", dsrc)
        self.assertIn("_read_probe", dsrc)
        # log_capture 的进度正则能匹配两阶段文案
        import re
        self.assertTrue(
            re.search(lc._PROGRESS_RE, "故事生成中… 已生成 1234 字"))
        self.assertTrue(
            re.search(lc._THINK_PROGRESS_RE, "模型思考中… 已思考 1234 字符"))

    def test_deepseek_cancel_checkpoints_in_wait(self):
        # 生成中轮询必须带取消检查点（Web 控制台停止按钮）
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("_check_cancel()", src)

    def test_deepseek_readback_verifies_stability(self):
        # 回归：文本稳定判定前必须重读验证——LLM 流式输出间歇停顿
        # 可 >8s（长 JSON 输出），「连续 N 轮不变」可能是暂停而非完成，
        # 判定点读回残缺内容会解析失败（2026-08-15 线上剖析两次失败）
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("_READBACK_MS", src)          # 重读验证窗口常量
        self.assertIn("re_len", src)                # 判定前重读
        self.assertIn("稳定判定后输出仍增长", src)   # 增长则继续等待

    def test_deepseek_failure_dump_page_state(self):
        # 全探测失败 → 子类调用 _dump_page_state（基类实现 raise RuntimeError）
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("_dump_page_state", src)
        base = self._src("web_drivers/base.py")
        self.assertIn("_dump_page_state", base)
        self.assertIn("raise RuntimeError", base)

    def test_deepseek_probe_cli_exists(self):
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("--probe", src)
        self.assertIn("__main__", src)

    def test_deepseek_result_selector_prefers_main_content(self):
        # 回归：深度思考开启时页面有思考容器排在正文前，querySelector
        # 只取第一个匹配——首个候选必须是正文容器
        # （ds-assistant-message-main-content），否则误读思考过程
        # 造成「文本稳定」误判完成（2026-08-15 线上故障根因）
        import web_drivers.deepseek as d
        first = d._RESULT_SELECTORS[0]
        self.assertIn("ds-assistant-message-main-content", first)
        # 兜底候选保留旧版（无思考容器的 UI）
        self.assertEqual(len(d._RESULT_SELECTORS), 4)

    def test_markdown_rebuild_shared_by_both_drivers(self):
        # ★ 2026-09-19 事故回归：网页端把 ## **N** 渲染成 h2 元素，只读
        # innerText 的通道会只剩裸章节号 → 格式校验「章节 0 个」必扣 4 分，
        # 通道满分只剩 6/10（当天 DeepSeek 5 轮丢 4 篇完整稿，一篇差 24 字）。
        # 逐块重建抽到 base.MARKDOWN_REBUILD_JS，两个驱动共用同一份实现。
        import web_drivers.base as b
        walker = b.MARKDOWN_REBUILD_JS
        for needle in ("toMarkdown", "H[1-6]", "repeat", "parts.join",
                       "const NL"):
            self.assertIn(needle, walker, needle)
        for mod in ("web_drivers/deepseek.py", "web_drivers/doubao.py"):
            self.assertIn("MARKDOWN_REBUILD_JS", self._src(mod), mod)
        ds = self._src("web_drivers/deepseek.py")
        probe = ds[ds.index("def _read_probe"):
                  ds.index("def _detect_session_id")]
        self.assertIn("%(walker)s", probe)   # walker 注入 evaluate 函数体
        self.assertIn("toMarkdown(c)", probe)  # 正文走逐块重建

    def test_deepseek_setup_is_noop_after_ui_revamp(self):
        # 2026-09 官网取消「快速/专家/识图」三大模式：setup() 必须是空操作，
        # 不得再点模式 tab，也不得再切深度思考/智能搜索开关
        # （用户约定：登录后直接用网页端默认状态）
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("def setup(self)", src)
        self.assertIn("使用网页端默认模式", src)
        for gone in ("_radio_group_selected", "_MODE_TEXT", "_set_toggle",
                     "_toggle_state", "smart_search", "deep_think"):
            self.assertNotIn(gone, src, f"改版后不该再出现 {gone}")
        # 只允许 --probe 里把 radiogroup 当"旧结构探测项"报出来，
        # setup 路径不得再依赖它
        self.assertNotIn("aria-checked", src)
        import web_drivers.deepseek as d
        drv = d.DeepSeekDriver({"url": "https://chat.deepseek.com/"})
        self.assertIs(drv.setup(), drv)   # 空操作，返回 self

    def test_deepseek_reply_anchor_guards_against_stale_reply(self):
        # 2026-09 虚拟列表适配：读取必须锚定「发送之后的新消息」。
        # 旧实现用 querySelector 取第一个正文容器，虚拟列表里第一个可能是
        # 上一轮回复 → 把旧回复当本次结果（2026-09-09 线上故障：
        # 生成 prompt 发出 11s 后读回上一步筛选的 191 字）
        src = self._src("web_drivers/deepseek.py")
        for needle in ("_mark_reply_anchor", "_ANCHOR_ATTR",
                       "_MESSAGE_SELECTOR", "div.ds-message", "_read_probe"):
            self.assertIn(needle, src, needle)
        send_body = src[src.index("def send(self)"):
                        src.index("def wait_complete")]
        self.assertIn("self._mark_reply_anchor()", send_body,
                      "发送前必须先打锚点")
        rr = src[src.index("def read_result"):src.index("def _mark_reply_anchor")]
        self.assertIn("_dump_page_state", rr,
                      "读不到新回复必须 loud-fail，不得回落旧回复")

    def test_deepseek_session_delete_revamped(self):
        # 2026-09-12 实测链路：
        #   接口 POST /api/v0/chat_session/delete
        #        body {"chat_session_ids": ["<uuid>"]}
        #        header Authorization: Bearer <localStorage userToken.value>
        #   DOM  侧栏 a[href='/a/chat/s/<uuid>'] → ⋯ → 菜单「删除」
        #        → 弹窗「删除该对话」
        src = self._src("web_drivers/deepseek.py")
        for needle in ("/api/v0/chat_session/delete", "chat_session_ids",
                       "userToken", "Bearer", "ds-dropdown-menu-option",
                       "删除该对话", "ds-modal-content", "/a/chat/s/"):
            self.assertIn(needle, src, needle)
        # 改版前的旧端点已随站点下线，不得残留（否则每次都白打一遍 404）
        self.assertNotIn("/api/v0/chat/delete_history", src)
        self.assertNotIn("/api/v0/chat/session/delete", src)

    def test_deepseek_auth_token_parsing_and_new_chat_text(self):
        # userToken 是 JSON（{"value": ...}），解析失败/缺失返回空串
        import web_drivers.deepseek as d
        drv = d.DeepSeekDriver({"url": "https://chat.deepseek.com/"})
        drv._safe_evaluate = lambda js, *a, **k: '{"value": "tok123", "__version": 1}'
        self.assertEqual(drv._auth_token(), "tok123")
        drv._safe_evaluate = lambda js, *a, **k: "plain-token"
        self.assertEqual(drv._auth_token(), "plain-token")
        drv._safe_evaluate = lambda js, *a, **k: ""
        self.assertEqual(drv._auth_token(), "")
        # 改版后新会话按钮文案 = 开启新对话（无 aria-label），
        # 需要按文本找叶子再点最近的可点击祖先
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("开启新对话", src)
        self.assertIn("closest(", src)

    def test_config_web_mode_preset_fully_removed(self):
        # 2026-09 改版后网页端没有模式可选 → 预设机制整体退役：
        # 配置里不再有 mode/deep_think/smart_search，config 不再导出
        # set_web_mode_preset，后端也不再暴露 /api/web-preset
        import config
        from config import WEB_DRIVERS
        self.assertFalse(hasattr(config, "set_web_mode_preset"))
        for name, cfg in WEB_DRIVERS.items():
            for key in ("mode", "deep_think", "smart_search", "preset",
                        "preset_supported"):
                self.assertNotIn(key, cfg, f"{name} 残留 {key}")
        with open("webui/api_settings.py", encoding="utf-8") as f:
            api_src = f.read()
        self.assertNotIn("/api/web-preset", api_src)
        self.assertNotIn("WEB_PRESET", api_src)
        with open("webui/static/index.html", encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("webPresetSel", html)
        with open("webui/static/app.js", encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn("webPreset", js)


class TestLegacyFullyRemoved(unittest.TestCase):
    """旧 OCR/Aizex/image-gen 已归档，不得残留在主链路或打包。"""

    def test_legacy_dir_gone(self):
        import os
        self.assertFalse(os.path.exists("web_drivers/legacy/aizex.py"))
        self.assertFalse(os.path.exists("web_drivers/legacy/__init__.py"))
        self.assertFalse(os.path.exists("workflows/image_gen.py"))
        self.assertFalse(os.path.exists("ocr_utils.py"))

    def test_main_chain_no_image_gen_entry(self):
        # --image-gen CLI 入口已随功能一并移除
        with open("main.py", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("--image-gen", src)


if __name__ == "__main__":
    unittest.main()
