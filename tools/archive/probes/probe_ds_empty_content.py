# 排查：老会话消息 content 为空——原始响应里正文到底在哪个字段
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from tools.ds_history_cleanup import DeepSeekClient

SIDS = sys.argv[1:] or [
    "e9e2f7b5-0000-0000-0000-000000000000",
]
report = json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260915_story30d.json"), encoding="utf-8"))
picks = [e["id"] for e in report["candidates"]
         if e["content_checked"] and e["msg_count"] >= 2][:2]
# 另外拿一条最近的（新）会话对照
picks += [s["id"] for s in json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260915_story30d.json"), encoding="utf-8"))["candidates"][:0]] or []
print("样本:", picks)
client = DeepSeekClient(headless=True, delay=0)
client.open()
try:
    for sid in picks:
        status, data = client._req("GET", "/api/v0/chat/history_messages",
                                   {"chat_session_id": sid})
        print("=" * 78)
        print("HTTP", status, "sid", sid)
        raw = json.dumps(data, ensure_ascii=False)
        print("响应长度", len(raw))
        biz = ((data.get("data") or {}).get("biz_data") or {})
        msgs = biz.get("chat_messages") or []
        print("msgs", len(msgs), "keys", list(biz.keys()))
        if msgs:
            m0 = msgs[0]
            print("message keys:", list(m0.keys()))
            for k, v in m0.items():
                sv = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                print("   %-24s = %s" % (k, (sv or "")[:200]))
        print("raw head:", raw[:600])
finally:
    client.close()
