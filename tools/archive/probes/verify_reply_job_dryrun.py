# -*- coding: utf-8 -*-
# ============================================================
# verify_reply_job_dryrun.py - 评论回复作业真机端到端演练（只生成不发送）
#
# 走完整链路：采集 20 条 → 预筛 → 网页版大模型挑最友善 → 读回当时题目与回答
#            → 写回复 → 本地硬校验 → 落台账（sent=False）→ 删会话。
# 全程不发送任何评论。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/verify_reply_job_dryrun.py [条数]
# ============================================================

import json
import os
import sys

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def main():
    # 先 import 应用层：浏览器工厂由 browser_adapter 注册（browser_pool 不依赖 applications）
    from applications.zhihu_story import browser_adapter  # noqa: F401
    from applications.zhihu_story import reply_task
    from web_drivers import reset_driver
    from web_drivers.browser_pool import close_shared_browser, get_browser

    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    b = get_browser()
    out = {}
    try:
        print('登录态：%s' % b.is_logged_in())
        r = reply_task.run_reply_job(
            b, count=count, dry_run=True,
            progress=lambda t: print('   ·', t))
        out = r
        print('=' * 60)
        print('结果：ok=%s units=%s' % (r.get('ok'), r.get('units')))
        print('说明：%s' % r.get('detail'))
        print('-' * 60)
        for rec in r.get('records') or []:
            print('【读者评论】%s' % rec.get('comment'))
            print('【当时的题目】%s' % (rec.get('question') or '（未读到）'))
            print('【生成的回复】%s' % rec.get('reply'))
            print('【字数】评论 %d 字 → 回复 %d 字'
                  % (len(rec.get('comment') or ''), len(rec.get('reply') or '')))
            print('【校验问题】%s' % (rec.get('issues') or '无'))
            print('【是否发送】%s' % ('已发送' if rec.get('sent') else '演练（未发送）'))
        print('-' * 60)
        dropped = r.get('dropped') or []
        print('被过滤掉的评论 %d 条：' % len(dropped))
        for d in dropped:
            print('   [%s] %s' % (d.get('reason'), (d.get('text') or '')[:30]))
    finally:
        try:
            reset_driver(delete_session=True)
        except Exception as exc:
            print('删会话异常：', exc)
        try:
            close_shared_browser()
        except Exception as exc:
            print('关浏览器异常：', exc)
    outdir = os.path.join('data', 'cleanup')
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'verify_reply_job.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print('结果已存：%s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
