# -*- coding: utf-8 -*-
import pathlib, re
p = pathlib.Path('tools/_pq3.py')
src = p.read_text(encoding='utf-8')
lines = src.splitlines()
out = []
for l in lines:
    stripped = l.strip()
    if stripped.endswith(chr(39) + chr(39)) and not stripped.endswith(chr(39) * 3):
        l = l + chr(39)
    out.append(l)
p.write_text(chr(10).join(out) + chr(10), encoding='utf-8')
print('all closers fixed')