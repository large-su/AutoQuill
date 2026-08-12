# ============================================================
# tests/test_webui_logparse.py — Web 控制台日志解析
# ============================================================

import logging
import queue
import unittest

from webui import log_capture


class TestParseLine(unittest.TestCase):

    def test_stage(self):
        ev, p = log_capture.parse_line("2026-08-11 12:00:00 [INFO] 步骤 2：提取内容")
        self.assertEqual(ev, "stage")
        self.assertEqual(p["num"], 2)

    def test_stage_colon(self):
        ev, p = log_capture.parse_line("步骤 3: 生成故事")
        self.assertEqual(ev, "stage")
        self.assertEqual(p["num"], 3)

    def test_progress(self):
        ev, p = log_capture.parse_line("生成中… 累计输出 1234 字符")
        self.assertEqual(ev, "progress")
        self.assertEqual(p["chars"], 1234)

    def test_task_progress_with_pct(self):
        ev, p = log_capture.parse_line("任务进度：已读取 5 篇样本 | 15%")
        self.assertEqual(ev, "progress")
        self.assertTrue(p.get("task"))
        self.assertEqual(p["pct"], 15)
        self.assertIn("已读取 5 篇样本", p["text"])

    def test_task_progress_indeterminate(self):
        # 剖析中：无百分比 → pct=None（前端显示不确定动画）
        ev, p = log_capture.parse_line(
            "任务进度：大模型剖析中（分析文风与技法）…")
        self.assertEqual(ev, "progress")
        self.assertTrue(p.get("task"))
        self.assertIsNone(p["pct"])

    def test_result_ok(self):
        for kw in ("提取成功", "格式检测：9/10", "流式生成完成",
                   "草稿已保存", "✓ 服务端草稿已确认"):
            ev, _ = log_capture.parse_line(f"… {kw} …")
            self.assertEqual(ev, "result", f"kw={kw}")

    def test_run_end(self):
        for kw in ("本轮完成：成功 1/1 轮", "批量任务结束：累计发布 2 篇",
                   "目标达成！", "EXIT"):
            ev, _ = log_capture.parse_line(kw)
            self.assertEqual(ev, "run_end", f"kw={kw}")

    def test_error(self):
        for kw in ("[ERROR] 提取失败", "[WARNING] 放弃本轮",
                   "生成异常：超时", "降级为快速模式"):
            ev, _ = log_capture.parse_line(kw)
            self.assertEqual(ev, "error", f"kw={kw}")

    def test_plain_log(self):
        ev, p = log_capture.parse_line("随便一行普通日志")
        self.assertEqual(ev, "log")
        self.assertIn("text", p)

    def test_empty(self):
        self.assertIsNone(log_capture.parse_line(""))
        self.assertIsNone(log_capture.parse_line("   "))

    def test_result_precedes_error(self):
        # 「提取成功」与「失败」同行时优先算成功
        ev, _ = log_capture.parse_line("提取成功（上次失败已跳过）")
        self.assertEqual(ev, "result")


class TestCaptureDrain(unittest.TestCase):

    def test_roundtrip(self):
        q = queue.Queue()
        handler = log_capture.CaptureHandler(q)
        handler.setFormatter(logging.Formatter(log_capture._FORMAT))
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            logging.getLogger("test.logparse").info("步骤 1：开始")
            logging.getLogger("test.logparse").warning("生成中… 累计输出 500 字符")
            lines = log_capture.drain(q)
            self.assertEqual(len(lines), 2)
            ev, _ = log_capture.parse_line(lines[0])
            self.assertEqual(ev, "stage")
            # drain 后队列清空
            self.assertEqual(log_capture.drain(q), [])
        finally:
            log_capture.uninstall(handler)
            root.setLevel(old_level)

    def test_install_uninstall(self):
        handler = log_capture.install()
        try:
            self.assertIn(handler, logging.getLogger().handlers)
        finally:
            log_capture.uninstall(handler)
        self.assertNotIn(handler, logging.getLogger().handlers)


class TestSummarize(unittest.TestCase):

    def test_short(self):
        self.assertEqual(log_capture.summarize("短行"), "短行")

    def test_truncate(self):
        long_line = "长" * 200
        s = log_capture.summarize(long_line)
        self.assertEqual(len(s), 91)  # 90 字 + 省略号
        self.assertTrue(s.endswith("…"))


if __name__ == "__main__":
    import logging
    unittest.main()
