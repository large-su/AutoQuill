# ============================================================
# tests/test_web_parallel.py — Web 并行调度器（DOM 版）测试
#
# 调度逻辑用 FakeDriver（duck typing，不碰浏览器）单测；
# 源码断言防退化（无 OCR 依赖、workflows 分发接线）。
#
# 运行：python -m unittest discover -s tests
# ============================================================

import unittest
from unittest import mock


class TestParallelSourceOnly(unittest.TestCase):
    """parallel.py 必须与物理鼠标/坐标/OCR 解绑。"""

    def _src(self, rel_path):
        with open(rel_path, encoding="utf-8") as f:
            return f.read()

    def test_no_legacy_automation_in_parallel(self):
        src = self._src("web_drivers/parallel.py")
        for banned in ("pyautogui", "pyperclip", "ocr_utils",
                       "find_text_on_screen", "numpy"):
            self.assertNotIn(banned, src, banned)

    def test_workflows_dispatch_wiring(self):
        src = self._src("workflows/base.py")
        self.assertIn("web_drivers.parallel", src)
        self.assertIn("_batch_generate_web_parallel", src)
        self.assertIn("_batch_retry_web_parallel", src)
        self.assertIn('parallel_tabs', src)   # 分发条件

    def test_config_parallel_params(self):
        src = self._src("config/__init__.py")
        for key in ("parallel_tabs", "consecutive_fail_threshold",
                    "scan_interval"):
            self.assertIn(f'"{key}"', src, key)

    def test_deepseek_has_new_chat_override(self):
        src = self._src("web_drivers/deepseek.py")
        self.assertIn("def new_chat", src)
        base = self._src("web_drivers/base.py")
        self.assertIn("def new_chat", base)   # 基类默认实现

    def test_parallel_heartbeat_matches_webui(self):
        # 心跳带 [Slot N] 前缀仍被 webui 进度正则命中（re.search）
        import webui.log_capture as lc
        import re
        self.assertTrue(
            re.search(lc._PROGRESS_RE, "[Slot 1] 故事生成中… 已生成 1234 字"))


# ============================================================
# FakeDriver：脚本化假驱动，模拟真实 DeepSeekDriver 的交互原语
# ============================================================

class FakeDriver:
    """按任务提供轮询脚本；未提供脚本的任务恒 (文本 0, 无停止按钮)。

    task_scripts: {该 driver 的派发序号: [len_script, stop_script]}——
        len_script: _current_reply_len() 每轮返回值（耗尽后保持末值）
        stop_script: _stop_button_present() 每轮返回值（耗尽后保持末值）
        序号从 0 计（input() 每派发一次 +1），与全局任务索引无关。
    """

    def __init__(self, task_scripts=None, result="x" * 600, config=None):
        self.calls = []
        self.task_scripts = task_scripts or {}
        self.result = result
        self.config = config or {"stable_count": 2, "max_wait": 600}
        self._task = -1      # input() 派发时推进到当前任务
        self._last_len = 0

    # ---- 派发交互（记录调用顺序）----
    def new_chat(self):
        self.calls.append("new_chat")

    def setup(self):
        self.calls.append("setup")

    def input(self, prompt):
        self.calls.append(f"input:{prompt[:10]}")
        self._task += 1

    def send(self):
        self.calls.append("send")

    def close_session(self):
        self._closed = True
        self.calls.append("close_session")

    # ---- 非阻塞轮询原语 ----
    def _scripts(self):
        return self.task_scripts.get(self._task, ([], []))

    def _current_reply_len(self):
        lens = self._scripts()[0]
        if lens:
            self._last_len = lens.pop(0)
        return self._last_len

    def _stop_button_present(self):
        stops = self._scripts()[1]
        if stops:
            return stops.pop(0)
        return stops[-1] if stops else False

    def read_result(self):
        self.calls.append("read_result")
        if isinstance(self.result, list):   # 按派发序号区分结果
            if self._task < len(self.result):
                return self.result[self._task]
            return self.result[-1]
        return self.result


class FakeTime:
    """可控时钟：time() 由 now 决定，sleep() 推进 now。"""

    def __init__(self, start=0.0, step=1.0):
        self.now = start
        self.step = step

    def time(self):
        return self.now

    def sleep(self, secs):
        self.now += self.step


# ============================================================
# SlotState 轮询纯逻辑（_poll）
# ============================================================

class TestPollLogic(unittest.TestCase):

    def _poll_slot(self, drv, config=None):
        import time as _time
        from web_drivers.parallel import SlotState, ParallelWebRunner
        drv.config = config or drv.config
        runner = ParallelWebRunner(num_slots=1)
        slot = SlotState(0, drv)
        slot.status = "GENERATING"
        slot.start_time = _time.time()   # 真实时钟：max_wait 判定基于它
        drv._task = 0   # _poll 单测不经过 run 派发，直接定位任务 0
        return runner, slot

    def test_done_when_stop_button_appeared_then_gone(self):
        drv = FakeDriver(task_scripts={
            0: ([0, 0, 0], [True, True, False])})
        runner, slot = self._poll_slot(drv)
        self.assertEqual(runner._poll(slot), "CONTINUING")
        self.assertEqual(runner._poll(slot), "CONTINUING")
        # 第三轮：停止按钮曾出现且现在消失 → DONE（即使文本为 0）
        self.assertEqual(runner._poll(slot), "DONE")

    def test_done_when_text_stable(self):
        drv = FakeDriver(task_scripts={
            0: ([100, 100, 100, 100, 100], [])})
        runner, slot = self._poll_slot(drv)
        self.assertEqual(runner._poll(slot), "CONTINUING")
        self.assertEqual(runner._poll(slot), "CONTINUING")
        # 第三轮：stable=2 且文本 >0 → 进入 read-back 验证（防停顿误判）
        self.assertEqual(runner._poll(slot), "CONTINUING")
        # 第四、五轮：重读长度不变 × 2 → DONE
        self.assertEqual(runner._poll(slot), "CONTINUING")
        self.assertEqual(runner._poll(slot), "DONE")

    def test_readback_growth_resets_stability(self):
        # read-back 发现长度增长（LLM 停顿恢复）→ 重置继续等待
        drv = FakeDriver(task_scripts={
            0: ([100, 100, 100, 120, 120, 120, 120, 120], [])})
        runner, slot = self._poll_slot(drv)
        self.assertEqual(runner._poll(slot), "CONTINUING")  # 100 首次
        self.assertEqual(runner._poll(slot), "CONTINUING")  # 100 稳定
        self.assertEqual(runner._poll(slot), "CONTINUING")  # stable=2 → 进入验证
        self.assertEqual(runner._poll(slot), "CONTINUING")  # 重读发现 120 → 重置
        self.assertEqual(runner._poll(slot), "CONTINUING")  # 120 重新计数
        self.assertEqual(runner._poll(slot), "CONTINUING")  # 120 稳定 stable=2
        self.assertEqual(runner._poll(slot), "CONTINUING")  # 进入验证（pending）
        self.assertEqual(runner._poll(slot), "DONE")        # 重读一致 ×2 → 完成

    def test_never_false_positive_with_zero_text(self):
        # 停止按钮从未出现 + 文本恒 0 → 永不 DONE（仅超时），防误判
        drv = FakeDriver()
        runner, slot = self._poll_slot(drv)
        for _ in range(5):
            self.assertEqual(runner._poll(slot), "CONTINUING")

    def test_timeout_when_zero_text_forever(self):
        drv = FakeDriver(config={"stable_count": 2, "max_wait": 10})
        runner, slot = self._poll_slot(drv)
        with mock.patch("web_drivers.parallel.time", FakeTime(start=11.0)):
            slot.start_time = 0.0   # FakeTime 坐标：11 - 0 > 10 → 超时
            self.assertEqual(runner._poll(slot), "TIMEOUT")

    def test_cancel_raises(self):
        # 取消钩子置位 → WorkflowCancelled 冒泡（Web 控制台停止按钮）
        from applications.zhihu_story.browser_adapter import (
            set_cancel_hook, WorkflowCancelled)
        drv = FakeDriver()
        runner, slot = self._poll_slot(drv)
        set_cancel_hook(lambda: True)
        try:
            with self.assertRaises(WorkflowCancelled):
                runner._poll(slot)
        finally:
            set_cancel_hook(None)


# ============================================================
# run() 调度逻辑
# ============================================================

class TestRunScheduling(unittest.TestCase):

    def _runner(self, num_slots=2, **kw):
        from web_drivers.parallel import ParallelWebRunner
        return ParallelWebRunner(num_slots=num_slots,
                                 scan_interval=0, **kw)

    def _mk_slot(self, runner, i, drv):
        from web_drivers.parallel import SlotState
        return SlotState(i, drv)

    def test_all_success_order_preserved(self):
        # 2 slots × 4 任务全成功：结果与 tasks 顺序一致
        d0 = FakeDriver(task_scripts={
            0: ([600, 600, 600], []),   # d0 的第 0 次派发（全局任务 0）
            1: ([600, 600, 600], []),   # d0 的第 1 次派发（全局任务 2）
        })
        d1 = FakeDriver(task_scripts={
            0: ([600, 600, 600], []),   # d1 的第 0 次派发（全局任务 1）
            1: ([600, 600, 600], []),   # d1 的第 1 次派发（全局任务 3）
        })
        runner = self._runner(num_slots=2)
        runner.slots = [self._mk_slot(runner, 0, d0),
                        self._mk_slot(runner, 1, d1)]
        results = runner.run([(f"任务{i}", None) for i in range(4)])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r and len(r) >= 500 for r in results))
        # 每个任务派发前调用序列：new_chat → setup → input → send
        for d in (d0, d1):
            for call in ("new_chat", "setup", "send"):
                self.assertIn(call, d.calls, call)

    def test_failure_continues_to_next_task(self):
        # slot 0 首任务结果 <500 → 失败 → 继续接任务 2（成功后计数清零）
        d0 = FakeDriver(result=["short", "x" * 600], task_scripts={
            0: ([600, 600, 600], []),
            1: ([600, 600, 600], []),
        })
        d1 = FakeDriver(task_scripts={
            0: ([600, 600, 600], []),
            1: ([600, 600, 600], []),
        })
        runner = self._runner(num_slots=2)
        runner.slots = [self._mk_slot(runner, 0, d0),
                        self._mk_slot(runner, 1, d1)]
        results = runner.run([(f"任务{i}", None) for i in range(4)])
        self.assertIsNone(results[0])     # 任务 0 失败
        self.assertIsNotNone(results[1])
        self.assertIsNotNone(results[2])  # slot 0 续派成功
        self.assertIsNotNone(results[3])

    def test_threshold_resets_slot(self):
        # 连续 2 次失败 → 重置（close_session 被调），随后继续接任务
        d0 = FakeDriver(result="short", task_scripts={
            0: ([600, 600, 600], []),
            1: ([600, 600, 600], []),
            2: ([600, 600, 600], []),
        })
        runner = self._runner(num_slots=1, threshold=2)
        runner.slots = [self._mk_slot(runner, 0, d0)]
        results = runner.run([(f"任务{i}", None) for i in range(3)])
        self.assertIsNone(results[0])
        self.assertIsNone(results[1])     # 第 2 次失败 → 触发重置
        self.assertIn("close_session", d0.calls)
        self.assertIsNone(results[2])     # 重置后续派仍失败（result 恒 short）

    def test_timeout_resets_and_continues(self):
        # 任务 0 零文本超时 → 重置会话 → 任务 1 正常完成
        d0 = FakeDriver(config={"stable_count": 2, "max_wait": 3},
                        task_scripts={
                            0: ([], []),                  # 任务 0：恒 0 → 超时
                            1: ([600, 600, 600], []),     # 任务 1：稳定 → 成功
                        })
        runner = self._runner(num_slots=1)
        runner.slots = [self._mk_slot(runner, 0, d0)]
        # AdvancingTime：每轮扫描推进 1s；max_wait=3 → 任务 0 第 4 轮超时
        fake = FakeTime(step=1.0)
        with mock.patch("web_drivers.parallel.time", fake):
            results = runner.run([(f"任务{i}", None) for i in range(2)])
        self.assertIsNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertIn("close_session", d0.calls)   # 超时后重置发生

    def test_empty_tasks_returns_empty(self):
        runner = self._runner()
        self.assertEqual(runner.run([]), [])


if __name__ == "__main__":
    unittest.main()
