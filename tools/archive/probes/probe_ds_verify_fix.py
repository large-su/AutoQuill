# 验证修复：旧格式（fragments）会话现在能否命中指纹
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from tools.ds_history_cleanup import (DeepSeekClient, session_text,
                                      match_fingerprints,
                                      fingerprint_weight, normalize_messages)

report = json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260915_story30d.json"), encoding="utf-8"))
checked = [e for e in report["candidates"] if e["content_checked"]]
picks = checked[:4] + sorted(checked, key=lambda e: -e["msg_count"])[:2]
client = DeepSeekClient(headless=True, delay=0)
client.open()
try:
    for e in picks:
        raw, err = client.history(e["id"])
        norm = normalize_messages(raw or [])
        fp = match_fingerprints(session_text(norm))
        first_user = next((m["content"] for m in norm if m["role"] == "USER"), "")
        print("=" * 78)
        print("%s | %s | msgs=%d | 权重=%d" % (e["updated_local"], e["title"],
                                               len(norm), fingerprint_weight(fp)))
        print("  指纹:", fp)
        print("  首条用户消息前 160 字:", first_user[:160].replace("\n", " / "))
finally:
    client.close()
