"""Exercise independent SSE consumers and bounded unattended log capture."""
import asyncio
import logging
import unittest
from unittest import mock

from webui import api_runs, log_capture


class LogStreamsTest(unittest.TestCase):
    def tearDown(self):
        log_capture.drain()

    def test_unattended_capture_is_bounded(self):
        log_capture.drain()
        handler = log_capture.CaptureHandler()
        for i in range(2500):
            handler.emit(logging.makeLogRecord({"msg": "line %d" % i}))
        rows = log_capture.drain()
        self.assertLessEqual(len(rows), 2000)
        self.assertEqual(rows[-1], "line 2499")

    def test_two_open_consoles_receive_same_log(self):
        async def check():
            original_subscribers = set(log_capture._SUBSCRIBERS)
            # Stop the legacy synchronous polling loop once its queue is empty;
            # this makes the missing second-client event fail without hanging.
            with mock.patch.object(api_runs.time, "sleep", side_effect=EOFError):
                left = api_runs.api_events().body_iterator
                right = api_runs.api_events().body_iterator
                try:
                    await anext(left)   # initial state / subscription
                    await anext(right)
                    log_capture.CaptureHandler().emit(logging.makeLogRecord({
                        "msg": "health-stream-event", "levelno": logging.INFO}))
                    a = await anext(left)
                    b = await anext(right, "")
                    self.assertIn("health-stream-event", a)
                    self.assertIn("health-stream-event", b)
                finally:
                    await left.aclose()
                    await right.aclose()
            self.assertEqual(log_capture._SUBSCRIBERS, original_subscribers)
        asyncio.run(check())

    def test_disconnected_client_releases_subscription(self):
        async def check():
            before = set(log_capture._SUBSCRIBERS)
            request = mock.Mock()
            request.is_disconnected = mock.AsyncMock(return_value=True)
            stream = api_runs.api_events(request).body_iterator
            self.assertEqual(await anext(stream, "closed"), "closed")
            self.assertEqual(log_capture._SUBSCRIBERS, before)
        asyncio.run(check())

    def test_cancellation_releases_subscription_while_idle(self):
        async def check():
            before = set(log_capture._SUBSCRIBERS)
            stream = api_runs.api_events().body_iterator
            await anext(stream)
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(log_capture._SUBSCRIBERS, before)
        asyncio.run(check())

    def test_slow_subscriber_keeps_latest_bounded_tail(self):
        q = log_capture.subscribe()
        try:
            handler = log_capture.CaptureHandler()
            for i in range(2500):
                handler.emit(logging.makeLogRecord({"msg": "line %d" % i}))
            rows = log_capture.drain(q)
            self.assertEqual(len(rows), log_capture.MAX_PENDING_LINES)
            self.assertEqual(rows[-1], "line 2499")
        finally:
            log_capture.unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
