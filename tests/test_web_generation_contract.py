"""Offline contract tests for the provider-neutral generation observer."""
import unittest
from unittest import mock

from web_drivers.base import WebLLMDriver
from web_drivers.parallel import ParallelWebRunner, SlotState
from web_drivers.browser_pool import WebLoginRequired, WorkflowCancelled, generation_activity_scope


class _Driver(WebLLMDriver):
    def __init__(self, samples, config=None):
        super().__init__(config or {"stable_count": 2, "max_wait": 30})
        self.samples = iter(samples)

    def _generation_probe(self):
        return next(self.samples)

    # Abstract lifecycle is irrelevant to this non-blocking contract.
    def setup(self): pass
    def input(self, prompt): pass
    def send(self): pass
    def wait_complete(self, max_wait=None): pass
    def read_result(self): pass


class GenerationContractTests(unittest.TestCase):
    def test_deepseek_growth_restarts_stability_before_parallel_completion(self):
        from web_drivers.deepseek import DeepSeekDriver
        driver = DeepSeekDriver({'url': 'https://chat.deepseek.com/', 'stable_count': 2, 'max_wait': 60})
        driver._raise_if_logged_out = lambda *a: None
        driver._stop_button_present = lambda: False
        sizes = iter([100, 100, 100, 120, 120, 120, 120])
        driver._read_probe = lambda: {'main': '字' * next(sizes), 'think_len': 0}
        slot = SlotState(0, driver)
        runner = ParallelWebRunner(num_slots=1)
        states = [runner._poll(slot) for _ in range(7)]
        self.assertEqual(states[:6], ['CONTINUING'] * 6)
        self.assertEqual(states[-1], 'DONE')

    def test_shared_wait_checks_cancellation_before_sampling(self):
        driver = _Driver([])
        driver._page_instance = mock.Mock(side_effect=AssertionError('must not access page'))
        with mock.patch('web_drivers.browser_pool._check_cancel', side_effect=WorkflowCancelled('stop')):
            with self.assertRaises(WorkflowCancelled):
                WebLLMDriver.wait_complete(driver)

    def test_doubao_card_and_chapter_tail_keep_distinct_completion_semantics(self):
        from web_drivers.doubao import DoubaoDriver
        driver = DoubaoDriver({'url': 'https://www.doubao.com/chat/', 'stable_count': 2, 'max_wait': 60})
        tracker = {'stable': 2, 'stop_seen': True}
        self.assertTrue(driver._generation_completed({'text': '', 'card': True, 'generating': False}, tracker))
        self.assertFalse(driver._generation_completed({'text': '', 'card': True, 'generating': True}, tracker))
        self.assertFalse(driver._generation_completed({'text': '正文\n\n## **2**', 'card': False, 'generating': False}, tracker))

    def test_observer_logs_actual_growth_as_frontend_progress(self):
        driver = _Driver([{'text': '正文', 'generating': True}, {'text': '', 'thinking_chars': 30, 'generating': True}])
        with self.assertLogs('web_drivers.base', level='INFO') as logs:
            driver.poll_generation()
            driver.poll_generation()
        self.assertTrue(any('故事生成中… 已生成 2 字' in line for line in logs.output))
        self.assertTrue(any('模型思考中… 已思考 30 字符' in line for line in logs.output))

    def test_typed_login_and_cancel_errors_are_not_converted_to_failed(self):
        for error in (WebLoginRequired("login"), WorkflowCancelled("stop")):
            driver = _Driver([])
            driver._generation_probe = mock.Mock(side_effect=error)
            with self.assertRaises(type(error)):
                driver.poll_generation()

    def test_activity_callback_contains_deadline_and_is_readable(self):
        seen = []
        driver = _Driver([{"text": "", "generating": True}])
        with generation_activity_scope(lambda driver_id, sample: seen.append(sample)):
            snapshot = driver.poll_generation(max_wait=30)
        self.assertEqual(driver.active_generation_status(), snapshot)
        self.assertEqual(seen[-1]["state"], "thinking")
        self.assertGreater(seen[-1]["deadline"], seen[-1]["at"])

    def test_completion_requires_site_evidence_or_stable_reply(self):
        driver = _Driver([
            {"text": "answer", "generating": True},
            {"text": "answer", "generating": False},
            {"text": "answer", "generating": False},
        ])
        self.assertEqual(driver.poll_generation()["state"], "generating")
        # A site-specific stop control was observed, then disappeared: that is
        # stronger completion evidence than waiting for a third stable sample.
        self.assertEqual(driver.poll_generation()["state"], "completed")

    def test_waiting_has_observable_state_but_does_not_fake_progress(self):
        driver = _Driver([{"text": "", "generating": False}],
                         {"stable_count": 2, "max_wait": 30})
        result = driver.poll_generation()
        self.assertEqual(result["state"], "waiting")
        self.assertFalse(result["progressed"])
        self.assertEqual(result["text"], "")

    def test_parallel_uses_public_generation_contract(self):
        driver = _Driver([{"text": "x" * 600, "generating": False,
                           "done": True}])
        driver.config = {"stable_count": 2, "max_wait": 30}
        slot = SlotState(0, driver)
        slot.status = SlotState.GENERATING
        slot.start_time = 1
        runner = ParallelWebRunner(num_slots=1, scan_interval=0)
        with mock.patch("web_drivers.parallel.time.time", return_value=2):
            self.assertEqual(runner._poll(slot), "DONE")


if __name__ == "__main__":
    unittest.main()
