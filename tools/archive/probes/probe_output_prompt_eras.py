# 从 output/ 历史产物里挖出各时期 AutoQuill 提示词模板（跨版本指纹来源）
import os, re, sys, glob, collections
sys.stdout.reconfigure(encoding="utf-8")

files = sorted(glob.glob("output/story_*.md"))
print("output/story_*.md 共", len(files))
by_prefix = collections.defaultdict(lambda: {"n": 0, "first": "", "last": "", "sample": ""})
for path in files:
    m = re.search(r"story_\d+_(\d{8})_", os.path.basename(path))
    day = m.group(1) if m else "?"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(400)
    except Exception:
        continue
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    key = first[:24]
    rec = by_prefix[key]
    rec["n"] += 1
    rec["first"] = rec["first"] or day
    rec["last"] = day
    if not rec["sample"]:
        rec["sample"] = " / ".join(lines[1:5])[:150]
print("\n== 首行模板聚类 ==")
for key, rec in sorted(by_prefix.items(), key=lambda kv: -kv[1]["n"]):
    print("%5d  %s ~ %s  |  %s" % (rec["n"], rec["first"], rec["last"], key))
    print("        样例: %s" % rec["sample"])
