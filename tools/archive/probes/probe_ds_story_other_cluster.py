# 全量复核：150 条 story_other 的首条用户消息聚类（判断是否漏判 AutoQuill）
import json, os, sys, collections
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from tools.ds_history_cleanup import (DeepSeekClient, session_text, normalize_messages,
                                      match_fingerprints, FINGERPRINTS)

d = json.load(open(os.path.join(ROOT, "data/cleanup/scan_20260917_allcontent.json"), encoding="utf-8"))
other = [e for e in d["candidates"] if e["verdict"] == "story_other"]
print("复核 story_other:", len(other))

client = DeepSeekClient(headless=True, delay=0.15)
client.open()
rows = []
try:
    for i, e in enumerate(other, 1):
        raw, err = client.history(e["id"])
        norm = normalize_messages(raw or [])
        first = next((m["content"] for m in norm if m["role"] == "USER"), "")
        rows.append({"id": e["id"], "title": e["title"], "when": e["updated_local"],
                     "head": first[:90].replace("\n", " ⏎ "),
                     "fp": match_fingerprints(first), "len": len(first)})
        if i % 25 == 0:
            print("  …%d/%d" % (i, len(other)), flush=True)
finally:
    client.close()

json.dump(rows, open(os.path.join(ROOT, "data/cleanup/story_other_heads.json"), "w",
                     encoding="utf-8"), ensure_ascii=False, indent=1)

# 聚类：同一模板首条消息前 40 字
c = collections.Counter(r["head"][:40] for r in rows)
print("\n== 首条消息模板聚类（前 40 字）==")
for head, n in c.most_common(30):
    print("  %3d  %s" % (n, head))
print("\n== 命中任意指纹的行 ==")
for r in rows:
    if r["fp"]:
        print("  ", r["when"], r["title"], r["fp"])
print("\n== 含「你是一位」开头的行 ==")
for r in rows:
    if r["head"].startswith("你是一位"):
        print("  ", r["when"], r["title"], "|", r["head"][:70])
