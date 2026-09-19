# 取样：不同时期会话的首条用户提示词（找跨版本指纹）
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from tools.ds_history_cleanup import (DeepSeekClient, session_text,
                                      match_fingerprints, normalize_messages)

report = json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260915_story30d.json"), encoding="utf-8"))
cand = [e for e in report["candidates"] if e["content_checked"]]
# 按月份分组，每月抽 2 条
bymonth = {}
for e in cand:
    bymonth.setdefault(e["updated_local"][:7], []).append(e)
picks = []
for month in sorted(bymonth):
    picks.extend(bymonth[month][:2])
print("取样月份:", sorted(bymonth))
client = DeepSeekClient(headless=True, delay=0)
client.open()
try:
    for e in picks:
        raw, err = client.history(e["id"])
        norm = normalize_messages(raw or [])
        first_user = next((m["content"] for m in norm if m["role"] == "USER"), "")
        fp = match_fingerprints(session_text(norm))
        print("=" * 78)
        print("%s | %s | msgs=%d | fp=%s" % (e["updated_local"], e["title"],
                                             len(norm), fp))
        print("  HEAD:", first_user[:260].replace("\n", " / "))
finally:
    client.close()
