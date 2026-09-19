# 排查：已查内容的旧会话为什么没命中指纹
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from tools.ds_history_cleanup import DeepSeekClient, session_text, match_fingerprints

report = json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260915_story30d.json"), encoding="utf-8"))
checked = [e for e in report["candidates"] if e["content_checked"] and e["msg_count"] >= 2]
print("checked(>=2 msgs):", len(checked))
# 挑标题最像写故事的几条 + 消息最多的几条
by_title = [e for e in checked if any(k in e["title"] for k in ("创作", "小说", "故事", "虐文", "男主"))][:3]
by_len = sorted(checked, key=lambda e: -e["msg_count"])[:3]
picks = by_title + by_len
seen = set()
client = DeepSeekClient(headless=True, delay=0)
client.open()
try:
    for e in picks:
        if e["id"] in seen: continue
        seen.add(e["id"])
        msgs, err = client.history(e["id"])
        print("=" * 78)
        print(e["updated_local"], "|", e["title"], "| msgs:", len(msgs or []), "err:", err)
        for m in (msgs or [])[:3]:
            role = m.get("role")
            c = str(m.get("content") or "")
            print("  - role=%s len=%d" % (role, len(c)))
            if role == "USER":
                print("    USER 前 900 字：")
                print("    " + c[:900].replace("\n", "\n    "))
        print("  指纹命中:", match_fingerprints(session_text(msgs or [])))
finally:
    client.close()
