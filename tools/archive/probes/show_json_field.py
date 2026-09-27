# -*- coding: utf-8 -*-
# 通用 JSON 字段打印：python tools/archive/probes/show_json_field.py <json> <key> [index]
import json
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

data = json.load(open(sys.argv[1], encoding='utf-8'))
key = sys.argv[2]
node = data
for part in key.split('.'):
    if isinstance(node, list):
        node = node[int(part)]
    else:
        node = (node or {}).get(part)
if isinstance(node, list):
    for i, item in enumerate(node):
        print('[%d] %s' % (i, json.dumps(item, ensure_ascii=False, default=str)[:200]))
else:
    print(json.dumps(node, ensure_ascii=False, indent=2, default=str)[:2000])
