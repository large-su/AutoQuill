# ============================================================
# tests/test_llm_client.py — LLM API 网络层流式调用测试
#
# 重点防回归：思维链（reasoning_content）心跳日志——前端进度条
# 与 server watchdog 都依赖它（推理模型长思维链期间无 content）。
# ============================================================

import json
import time
import unittest
from unittest import mock

from llm_client import _call_llm_streaming


class _FakeResponse:
    def __init__(self, lines, hold=False):
        """hold=True 模拟流挂起：close() 前 iter_lines 不结束。"""
        self._lines = list(lines)
        self._hold = hold
        self.status_code = 200
        self.encoding = "utf-8"
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        if self._hold:
            while not self.closed:
                time.sleep(0.05)
            return iter([])
        return iter(self._lines)

    def close(self):
        self.closed = True


def _sse(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False)


class TestCallLlmStreaming(unittest.TestCase):
    def _call(self, lines, **kw):
        resp = _FakeResponse(lines)
        with mock.patch("llm_client.requests.post",
                        return_value=resp), \
             mock.patch("config.LLM_API_KEY", "test-key"):
            return _call_llm_streaming("hello", max_tokens=100, **kw)

    def test_reasoning_heartbeat_shows_cumulative_total(self):
        # 300+300 reasoning 超过 400 阈值 → 心跳展示累计 600（非窗口 400）
        lines = [
            _sse({"choices": [{"delta": {"reasoning_content": "思" * 300}}]}),
            _sse({"choices": [{"delta": {"reasoning_content": "考" * 300}}]}),
            _sse({"choices": [{"delta": {"content": "正文"}}]}),
            "data: [DONE]",
        ]
        with self.assertLogs("llm_client", level="INFO") as cm:
            full, _elapsed, err = self._call(lines)
        self.assertIsNone(err)
        self.assertEqual(full, "正文")
        joined = "\n".join(cm.output)
        self.assertIn("模型思考中… 已思考 600 字符", joined)

    def test_no_heartbeat_below_threshold(self):
        lines = [
            _sse({"choices": [{"delta": {"reasoning_content": "a" * 399}}]}),
            "data: [DONE]",
        ]
        with self.assertLogs("llm_client", level="INFO") as cm:
            full, _elapsed, err = self._call(lines)
        self.assertIsNone(err)
        self.assertNotIn("模型思考中", "\n".join(cm.output))

    def test_content_still_streamed_and_returned(self):
        lines = [
            _sse({"choices": [{"delta": {"content": "你好"}}]}),
            _sse({"choices": [{"delta": {"content": "世界"}}]}),
            "data: [DONE]",
        ]
        chunks = []
        full, _elapsed, err = self._call(lines, on_chunk=chunks.append)
        self.assertIsNone(err)
        self.assertEqual(full, "你好世界")
        self.assertEqual("".join(chunks), "你好世界")

    def test_key_url_model_overrides_used_in_payload(self):
        resp = _FakeResponse(["data: [DONE]"])
        with mock.patch("llm_client.requests.post",
                        return_value=resp) as post:
            _call_llm_streaming("hello", max_tokens=100,
                                api_key="k2", base_url="u2", model="m2")
        call = post.call_args
        self.assertEqual(call.args[0], "u2/chat/completions")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer k2")
        self.assertEqual(call.kwargs["json"]["model"], "m2")

    def test_usage_chunk_skipped_without_error(self):
        # include_usage 尾包（无 choices）不崩、不计入正文
        resp = _FakeResponse([_sse({"usage": {"total_tokens": 10}}),
                              "data: [DONE]"])
        with mock.patch("llm_client.requests.post", return_value=resp), \
             mock.patch("config.LLM_API_KEY", "test-key"):
            full, _elapsed, err = _call_llm_streaming("hello", max_tokens=100)
        self.assertEqual(full, "")
        self.assertIsNone(err)

    def test_stalled_stream_watchdog_returns_timeout_error(self):
        # 流挂起（无任何 token）：watchdog 在 first-token 超时后强制关流
        resp = _FakeResponse([], hold=True)
        with mock.patch("llm_client.requests.post", return_value=resp), \
             mock.patch("config.LLM_API_KEY", "test-key"), \
             mock.patch("config.LLM_API_STREAM_FIRST_TOKEN_TIMEOUT", 0.2):
            full, _elapsed, err = _call_llm_streaming("hello", max_tokens=100)
        self.assertEqual(full, "")
        self.assertIn("首个内容 token", err or "")
        self.assertTrue(resp.closed)


if __name__ == "__main__":
    unittest.main()
