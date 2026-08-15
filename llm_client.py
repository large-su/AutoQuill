# ============================================================
# llm_client.py — LLM API 网络层（由 llm_api.py 拆分，2026-08）
#
# 职责：底层 HTTP 流式调用与连通性测试。不包含业务逻辑。
#
# 架构位置：Layer 0 (Tools) — 被 story_generation / story_scoring /
#           workflows / main 共享。
#
# 运行时切换模型原理：函数内 `from config import ...` 动态读取
# 当前模块属性，Web 控制台 set_runtime_model 重赋值后立即生效。
# ============================================================

import json
import logging
import threading
import time

import requests

log = logging.getLogger(__name__)


def _call_llm_streaming(user_message, max_tokens, temperature=None,
                         on_chunk=None, label="LLM",
                         api_key=None, base_url=None, model=None):
    """
    通用的流式 chat.completions 调用。

    参数：
        user_message: 完整的用户消息文本
        max_tokens:   max_tokens 参数
        temperature:  温度（None 则用 config.LLM_API_TEMPERATURE）
        on_chunk:     可选回调 fn(content_chunk: str)。
                      若为 None：不打印不回调（静默累积）；
                      若为 sys.stdout.write：实时打印到终端。
        label:        日志标签
        api_key/base_url/model: 显式覆盖（None → config 根配置）。
                      供文风剖析等复用 KB 专属配置（kb_manager 语义）。

    返回：(full_content: str, elapsed: float, error: str or None)
    """
    from config import (
        LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL,
        LLM_API_TEMPERATURE, LLM_API_TIMEOUT,
        LLM_API_FREQUENCY_PENALTY, LLM_API_PRESENCE_PENALTY,
        LLM_API_EXTRA_BODY,
    )
    if api_key is not None:
        LLM_API_KEY = api_key
    if base_url is not None:
        LLM_API_BASE_URL = base_url
    if model is not None:
        LLM_API_MODEL = model
    from config import (
        LLM_API_CONNECT_TIMEOUT, LLM_API_STREAM_READ_TIMEOUT,
        LLM_API_STREAM_FIRST_TOKEN_TIMEOUT,
        LLM_API_STREAM_IDLE_TIMEOUT,
    )

    if not LLM_API_KEY:
        return "", 0.0, "API Key 未配置"

    url = f"{LLM_API_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": user_message}],
        "max_tokens": max_tokens,
        "temperature": temperature if temperature is not None else LLM_API_TEMPERATURE,
        "frequency_penalty": LLM_API_FREQUENCY_PENALTY,
        "presence_penalty": LLM_API_PRESENCE_PENALTY,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)

    start = time.time()
    full_content = ""
    last_usage = None
    response = None
    stream_stop = None
    stream_watchdog = None
    stream_state = {
        'first_content_at': None,
        'last_content_at': None,
        'timeout_reason': None,
    }
    # 思维链心跳：推理模型（deepseek-v4 等）先流 reasoning_content 再流
    # content，思维链期间无 on_chunk 回调，前端进度条与 server watchdog
    # 都依赖此心跳（否则长思维链被误判卡死）
    _reasoning = {"n": 0, "total": 0}

    try:
        response = requests.post(
            url, headers=headers, json=payload,
            timeout=(LLM_API_CONNECT_TIMEOUT, LLM_API_STREAM_READ_TIMEOUT),
            stream=True,
        )
        response.encoding = "utf-8"

        if response.status_code != 200:
            return full_content, time.time() - start, \
                f"HTTP {response.status_code}: {response.text[:300]}"

        # Socket read timeout 无法区分 SSE 心跳与真实文本，
        # 用独立计时器观察实际内容 token 的到达。
        stream_started = time.time()
        stream_stop = threading.Event()

        def _watch_stream_content():
            while not stream_stop.wait(0.5):
                now = time.time()
                first_content_at = stream_state['first_content_at']
                if first_content_at is None:
                    if now - stream_started < LLM_API_STREAM_FIRST_TOKEN_TIMEOUT:
                        continue
                    stream_state['timeout_reason'] = (
                        f"等待首个内容 token 超过 {LLM_API_STREAM_FIRST_TOKEN_TIMEOUT}s "
                        "（连接后无内容）"
                    )
                elif now - stream_state['last_content_at'] >= LLM_API_STREAM_IDLE_TIMEOUT:
                    stream_state['timeout_reason'] = (
                        f"内容停滞超过 {LLM_API_STREAM_IDLE_TIMEOUT}s（无新 token）"
                    )
                else:
                    continue

                try:
                    response.close()
                except Exception:
                    pass
                return

        stream_watchdog = threading.Thread(
            target=_watch_stream_content,
            name=f"llm-stream-watchdog-{label}",
            daemon=True,
        )
        stream_watchdog.start()

        for line in response.iter_lines(decode_unicode=True):
            if stream_state['timeout_reason']:
                break
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                if "usage" in chunk and chunk.get("usage"):
                    last_usage = chunk["usage"]
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                # 推理模型（deepseek-v4 等）先流 reasoning_content 再流 content：
                # 思维链期间的增量同样视为"流有活动"，避免看门狗误杀
                if delta.get("reasoning_content"):
                    now = time.time()
                    if stream_state['first_content_at'] is None:
                        stream_state['first_content_at'] = now
                        log.info(f"  {label}：收到首个 token（等待 "
                                 f"{now - stream_started:.1f}s），开始输出")
                    stream_state['last_content_at'] = now
                    _reasoning["n"] += len(delta["reasoning_content"])
                    _reasoning["total"] += len(delta["reasoning_content"])
                    if _reasoning["n"] >= 400:
                        log.info(f"  {label}：模型思考中… "
                                 f"已思考 {_reasoning['total']} 字符")
                        _reasoning["n"] = 0
                    continue
                content = delta.get("content", "")
                if content:
                    now = time.time()
                    if stream_state['first_content_at'] is None:
                        stream_state['first_content_at'] = now
                        log.info(f"  {label}：收到首个 token（等待 "
                                 f"{now - stream_started:.1f}s），开始输出")
                    stream_state['last_content_at'] = now
                    full_content += content
                    if on_chunk:
                        try:
                            on_chunk(content)
                        except Exception:
                            pass
            except json.JSONDecodeError:
                continue

        if stream_state['timeout_reason']:
            return (
                full_content,
                time.time() - start,
                stream_state['timeout_reason'],
            )

        if last_usage:
            try:
                from llm_token_tracker import tracker
                tracker.report(LLM_API_MODEL, last_usage)
            except Exception:
                pass

        return full_content, time.time() - start, None

    except requests.exceptions.Timeout:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return (
            full_content,
            time.time() - start,
            f"超时：连接/读取超过 {LLM_API_STREAM_READ_TIMEOUT}s",
        )
    except requests.exceptions.ConnectionError as e:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return full_content, time.time() - start, f"ConnectionError: {e}"
    except Exception as e:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return full_content, time.time() - start, f"Exception: {e}"
    finally:
        if stream_stop is not None:
            stream_stop.set()
        if stream_watchdog is not None:
            stream_watchdog.join(timeout=1)
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def test_api_connection():
    """测试 API 连接"""
    from config import (
        LLM_API_KEY,
        LLM_API_BASE_URL,
        LLM_API_MODEL,
        LLM_API_EXTRA_BODY,
    )

    if not LLM_API_KEY:
        print("  ❌ API Key 未配置！")
        return False

    print(f"  测试 API 连接...")
    print(f"  地址：{LLM_API_BASE_URL}")
    print(f"  模型：{LLM_API_MODEL}")
    print(f"  Key：{LLM_API_KEY[:8]}...{LLM_API_KEY[-4:]}")

    url = f"{LLM_API_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": "请回复：连接成功"}],
        "max_tokens": 1000,
        "stream": False
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)

    try:
        start = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.encoding = "utf-8"  # 强制 UTF-8
        elapsed = time.time() - start

        if response.status_code == 200:
            reply = response.json()["choices"][0]["message"]["content"]
            print(f"  ✓ 连接成功！（{elapsed:.1f}s）回复：{reply}")
            return True
        else:
            print(f"  ❌ HTTP {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ {e}")
        return False
