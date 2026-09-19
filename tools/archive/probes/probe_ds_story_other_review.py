# 复核 story_other 档：这些会话是不是漏判的 AutoQuill 链路
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from tools.ds_history_cleanup import (DeepSeekClient, session_text, normalize_messages,
                                      match_fingerprints)

d = json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260917_allcontent.json"), encoding="utf-8"))
other = [e for e in d["candidates"] if e["verdict"] == "story_other"]
bym = {}
for e in other:
    bym.setdefault(e["updated_local"][:7], []).append(e)
picks = []
for m in sorted(bym):
    picks.extend(bym[m][:3])
print("取样 %d 条 story_other（共 %d）" % (len(picks), len(other)))
client = DeepSeekClient(headless=True, delay=0)
client.open()
try:
    for e in picks:
        raw, err = client.history(e["id"])
        norm = normalize_messages(raw or [])
        first = next((m["content"] for m in norm if m["role"] == "USER"), "")
        marks = [k for k in ("知乎", "## ", "铁律", "守则", "自检", "格式规范", "高赞",
                             "Role", "设定", "章节", "禁止") if k in first]
        print("=" * 76)
        print("%s | %s | msgs=%d | 结构标记=%s" % (e["updated_local"], e["title"],
                                                   len(norm), marks))
        print("   HEAD:", first[:230].replace("\n", " / "))
finally:
    client.close()
