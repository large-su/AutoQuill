# -*- coding: utf-8 -*-
# ============================================================
# verify_manage_reply_dryrun.py — 管理页回复路径演练（只填不发送）
#
# 验证新的推荐路径：管理页卡片 → 回复 → 编辑器 → 输入 → 「发布」可用 → 清空。
# 顺带查一件事：管理页卡片上能不能看到「已回复」（决定发送后怎么核实）。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/verify_manage_reply_dryrun.py
# ============================================================

import json
import os
import sys

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

OUR_REPLY = '没啥深意，就一狗血爽文，看个热闹。'      # 22:46 真发出去的那条
DRAFT = '（演练）这条不会发出去，只验证路径。'


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    out = {}
    try:
        b.start()
        # 1) 管理页能看到「已回复」吗？（用已经真发过的那条回复验证）
        got = b.collect_manage_comments(limit=20)
        cards = got.get('comments') or []
        hits = [c for c in cards if OUR_REPLY in (c.get('text') or '')]
        print('管理页卡片 %d 张；含我们已发回复的卡片：%d 张' % (len(cards), len(hits)))
        out['cards'] = len(cards)
        out['cards_with_our_reply'] = len(hits)
        if hits:
            print('   → 管理页会显示回复内容，可用于发送后核实 ✓')
            print('   样例：', (hits[0].get('text') or '')[:80])
        else:
            print('   → 本页没看到我们的回复（那条评论可能已不在首屏 20 条里，结论不确定）')
        if not cards:
            print('!! 没有卡片，退出')
            return 1
        target = cards[0]
        print('2) 拿最新一张卡演练：%s | %s'
              % (target.get('author'), (target.get('text') or '')[:30]))
        r = b.send_reply_from_manage(target.get('text'), DRAFT, dry_run=True)
        out['dry_run'] = r
        print('3) 演练结果：', json.dumps(r, ensure_ascii=False))
        # 4) 演练后确认没留下东西
        left = b._safe_evaluate(
            "() => { const e = document.querySelector('div.public-DraftEditor-content');"
            " return e ? (e.innerText || '').trim() : '(没有编辑器)'; }")
        out['editor_leftover'] = left
        print('4) 编辑器残留：', repr(left))
    finally:
        b.close()
    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'verify_manage_reply.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
