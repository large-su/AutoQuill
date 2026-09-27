# -*- coding: utf-8 -*-
# 通用探针 JSON 摘要打印：python tools/archive/probes/show_probe_json.py <json> <key>
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

data = json.load(open(sys.argv[1], encoding="utf-8"))
key = sys.argv[2] if len(sys.argv) > 2 else ""
section = sys.argv[3] if len(sys.argv) > 3 else "all"
d = data[key] if key else data
print("URL:", d.get("url"))
print("TITLE:", d.get("title"))
if section in ("all", "classes"):
    print("=== CLASSES ===")
    for c in d.get("class_like") or []:
        print("   ", c)
if section in ("all", "buttons"):
    print("=== BUTTONS ===")
    for b in d.get("buttons") or []:
        print("   ", b.get("tag"), "|", (b.get("text") or "").replace(chr(10), " / ")[:50], "|", (b.get("cls") or "")[:60], "|", (b.get("href") or "")[:70])
if section in ("all", "people"):
    print("=== PEOPLE LINKS ===")
    for b in d.get("people_links") or []:
        print("   ", b.get("text"), "|", b.get("href"))
if section in ("all", "answers"):
    print("=== ANSWER LINKS ===")
    for b in d.get("answer_links") or []:
        print("   ", b.get("text"), "|", b.get("href"))
if section in ("all", "cards"):
    print("=== CARDS === count=", d.get("count"))
    for c in (d.get("cards") or [])[:3]:
        print("---", (c.get("text") or "")[:200].replace(chr(10), " / "))
        print(c.get("html") or "")
if section in ("all", "items"):
    print("=== ITEMS ===")
    for it in (d.get("items") or []):
        i = it.get("info") or {}
        print("   [%s] %s | %s" % (i.get("tag"), (i.get("cls") or "")[:70], (i.get("text") or "").replace(chr(10), " / ")))
if section in ("all", "body"):
    print("=== BODY ===")
    print(d.get("body_text") or "")
if section in ("all", "html"):
    print("=== HTML ===")
    print((d.get("html_head") or "")[:6000])