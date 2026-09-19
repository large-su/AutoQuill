import json, sys, collections
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from tools.ds_history_cleanup import story_kind
rows = json.load(open("data/cleanup/story_other_heads.json", encoding="utf-8"))
kinds = collections.Counter(story_kind(r["head"], r["title"]) for r in rows)
print("story_other 共", len(rows), dict(kinds))
print("\n== template 档（疑似工具生成的模板式提示词）按月份 ==")
t = [r for r in rows if story_kind(r["head"], r["title"]) == "template"]
print(collections.Counter(r["when"][:7] for r in t))
print("\n== template 档首行模板 ==")
for head, n in collections.Counter(r["head"][:30] for r in t).most_common(10):
    print("  %3d %s" % (n, head))
print("\n== manual 档抽样 15 ==")
m = [r for r in rows if story_kind(r["head"], r["title"]) == "manual"]
print("manual 共", len(m))
for r in m[:15]:
    print("   %s %-24s %s" % (r["when"], r["title"][:24], r["head"][:60]))
