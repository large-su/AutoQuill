# -*- coding: utf-8 -*-
# 把探针里的 JS 抽出来交给 node 做语法检查（定位反复出现的 Invalid token）
import importlib.util as u
import subprocess
import sys

sys.path.insert(0, '.')
spec = u.spec_from_file_location('probe', 'tools/archive/probes/probe_manage_reply.py')
m = u.module_from_spec(spec)
spec.loader.exec_module(m)

for name in ('CLICK_REPLY_JS', 'EDITOR_JS'):
    js = getattr(m, name)
    wrapped = 'const _fn = ' + js + ';'
    path = 'data/cleanup/_check_%s.js' % name
    open(path, 'w', encoding='utf-8').write(wrapped)
    r = subprocess.run(['node', '--check', path], capture_output=True, text=True)
    print('---', name, 'rc=%s' % r.returncode)
    if r.returncode:
        print(r.stderr[:400])
    else:
        print('语法 OK；前 80 字符:', repr(js[:80]))
