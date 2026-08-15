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
        self.assertIn(".fill(prompt)", src)     # textarea 纯文本，非剪贴板
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
        self.assertIn("_think_len", dsrc)  # 思考容器长度
        # 思考阶段正文选择器未命中时不得把思考文本计入正文长度
        # （ds-markdown 兜底会误匹配思考容器，2026-08-15 实测）
        self.assertIn('"ds-assistant-message-main-content" not in sel', dsrc)
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

    def test_deepseek_setup_target_state_driven(self):
        # setup() 必须「先读后点」：按目标状态（mode/deep_think/
        # smart_search）与当前不一致才点击，不得盲目点击破坏手动状态
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("def setup(self)", src)
        self.assertIn("_toggle_state", src)    # 读开关当前状态
        self.assertIn("_set_toggle", src)      # 目标状态驱动
        self.assertIn("_radio_group_selected", src)  # 读大模式
        self.assertIn("--selected", src)       # 开关状态类名
        self.assertIn("smart_search", src)
        # 回归：radiogroup 返回完整文本（含「模式」），配置是英文键
        # （fast/expert），必须经 _MODE_TEXT 映射后才能比对/查找，
        # 否则会去点「expert模式」这类不存在的文本（2026-08-15 故障）
        import web_drivers.deepseek as d
        self.assertEqual(d._MODE_TEXT["fast"], "快速模式")
        self.assertEqual(d._MODE_TEXT["expert"], "专家模式")
        self.assertNotIn("expert模式", src)

    def test_config_web_preset_translation(self):
        # 预设 → 目标字段翻译：fast = 快速+深思+搜索；expert = 专家+深思
        from config import set_web_mode_preset, WEB_DRIVERS, WEB_DRIVER_NAME
        old = dict(WEB_DRIVERS[WEB_DRIVER_NAME])
        try:
            set_web_mode_preset("fast", persist=False)
            cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
            self.assertEqual(cfg["mode"], "fast")
            self.assertTrue(cfg["deep_think"])
            self.assertTrue(cfg["smart_search"])
            set_web_mode_preset("expert", persist=False)
            cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
            self.assertEqual(cfg["mode"], "expert")
            self.assertTrue(cfg["deep_think"])
            self.assertFalse(cfg["smart_search"])
            with self.assertRaises(ValueError):
                set_web_mode_preset("bogus", persist=False)
        finally:
            WEB_DRIVERS[WEB_DRIVER_NAME].clear()
            WEB_DRIVERS[WEB_DRIVER_NAME].update(old)


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
