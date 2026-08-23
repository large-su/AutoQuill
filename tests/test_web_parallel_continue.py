# -*- coding: utf-8 -*-
"""Web 并行调度器：同会话连续提问（窗口复用）与损坏补位单测。"""
import unittest

import web_drivers as wd
from web_drivers.parallel import ParallelWebRunner


class FakeDriver:
    def __init__(self, fail_new_chat=False):
        self.new_chats = 0
        self.continues = 0
        self.sends = 0
        self.turn = 0
        self.fail_new_chat = fail_new_chat
        self.closed = 0
        self.produce = "故事正文" * 300
        self.config = {"stable_count": 1, "max_wait": 30}

    def new_chat(self):
        if self.fail_new_chat:
            raise RuntimeError("窗口打不开")
        self.new_chats += 1
        self.turn = 0

    def continue_chat(self, prompt):
        self.continues += 1
        self.turn = 1

    def setup(self): pass

    def input(self, prompt): pass

    def send(self):
        self.turn = 1
        self.sends += 1

    def _stop_button_present(self): return False

    def _current_reply_len(self): return len(self.produce) if self.turn else 0

    def _think_len(self): return 0

    def read_result(self): return self.produce

    def close_session(self): self.closed += 1


class FakeFactory:
    def __init__(self, fail_first=False):
        self.instances = []
        self.fail_first = fail_first

    def create(self):
        idx = len(self.instances)
        drv = FakeDriver(fail_new_chat=(self.fail_first and idx == 0))
        self.instances.append(drv)
        return drv


def _task(session_id, title="t"):
    return ("提示词-" + title, {"session_id": session_id, "title": title})


class ParallelContinueTest(unittest.TestCase):
    def setUp(self):
        self.factory = FakeFactory()
        self._orig = wd.create_driver
        wd.create_driver = self.factory.create

    def tearDown(self):
        wd.create_driver = self._orig

    def _run(self, tasks, slots=1):
        runner = ParallelWebRunner(num_slots=slots, threshold=2,
                                   scan_interval=0.01)
        runner.setup()
        return runner.run(tasks)

    def test_same_session_reuses_window(self):
        results = self._run([_task("A", "a1"), _task("A", "a2")])
        drv = self.factory.instances[0]
        self.assertEqual(drv.new_chats, 2, "setup + 首个任务各开一次会话")
        self.assertGreaterEqual(drv.continues, 1, "后续请求应 continue_chat")
        self.assertTrue(all(r and len(r) >= 500 for r in results))

    def test_different_session_opens_new_chat(self):
        results = self._run([_task("A", "a1"), _task("B", "b1")])
        drv = self.factory.instances[0]
        self.assertEqual(drv.new_chats, 3, "setup + 两个不同 session 各一次")
        self.assertEqual(drv.continues, 0)
        self.assertTrue(all(r for r in results))

    def test_dead_slot_replaced_with_new_window(self):
        # 前 2 个任务耗尽损坏的窗口（threshold=2 → 重置 3 次失败 → DEAD），
        # 第 3 个任务仍在队列：应开新窗口补位并完成
        self.factory = FakeFactory(fail_first=True)
        wd.create_driver = self.factory.create
        results = self._run([_task("A", "a1"), _task("A", "a2"), _task("A", "a3")],
                            slots=1)
        self.assertGreaterEqual(len(self.factory.instances), 2, "应有新窗口补位")
        done = [r for r in results if r]
        self.assertGreaterEqual(len(done), 1, "补位后应能完成任务")


if __name__ == "__main__":
    unittest.main()
