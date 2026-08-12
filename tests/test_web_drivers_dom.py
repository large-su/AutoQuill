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
        self.assertIn("Promise.race", src)      # 自限时哨兵
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
        self.assertIn("累计输出", dsrc)
        self.assertIn("生成中", dsrc)
        # log_capture 的进度正则能匹配该文案
        import re
        self.assertTrue(
            re.search(lc._PROGRESS_RE, "生成中… 累计输出 1234 字符"))

    def test_deepseek_cancel_checkpoints_in_wait(self):
        # 生成中轮询必须带取消检查点（Web 控制台停止按钮）
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("_check_cancel()", src)

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


class TestLegacyIsolation(unittest.TestCase):
    """旧 OCR 驱动必须隔离在 legacy 包内，不影响主链路。"""

    def test_legacy_exists_but_separate(self):
        import os
        self.assertTrue(os.path.exists("web_drivers/legacy/aizex.py"))
        # 新 base 不得 import legacy（旧驱动不污染主链路）
        with open("web_drivers/base.py", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("legacy", src)

    def test_image_gen_still_uses_legacy_aizex(self):
        # --image-gen 功能依赖旧 Aizex 驱动（保留引用）
        with open("workflows/image_gen.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("web_drivers.legacy.aizex", src)


if __name__ == "__main__":
    unittest.main()
