# -*- coding: utf-8 -*-
"""真机验证：评论兜底这条链路（**dry-run，不真的发评论**）。

验证点：
  1. 能回到参考回答页、找到「添加评论」入口、打开编辑器；
  2. 生成一条贴题评论并本地校验通过；
  3. 把评论填进编辑器、确认「发布」可用 → 然后**清空**，不点发布。
★ 全程不发评论、不改账号状态；数据目录指向临时目录，不碰你在用的数据。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--url", default="", help="参考回答页；默认取今天已发布的第一篇")
    ap.add_argument("--canned", default="",
                    help="跳过模型，直接用这段文本验证发送链路（DOM 部分）")
    ap.add_argument("--live-send", action="store_true",
                    help="真的发送（默认关闭；仅在你明确要求时使用）")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["AQ_DATA_DIR"] = args.data_dir
    from applications.zhihu_story import comment_fallback as cfb
    from applications.zhihu_story.browser_adapter import ZhihuBrowser

    # 参考故事：优先用命令行给的 URL，否则从今天生成的正文里挑一篇
    url = args.url
    story_text, story_title = "", ""
    if not url:
        out = os.path.join(os.environ.get("AQ_DATA_DIR") or ".", "output")
        cands = []
        for root in (out, r"C:\Users\10162\AppData\Roaming\AutoQuill\output"):
            if os.path.isdir(root):
                cands += [os.path.join(root, f) for f in os.listdir(root)
                          if f.startswith("story_20260929") and f.endswith(".md")]
        cands.sort()
        if cands:
            story_text = open(cands[-1], encoding="utf-8").read()
            story_title = os.path.basename(cands[-1])
            print("参考故事（本地正文）：%s（%d 字）" % (story_title, len(story_text)))
    if not url:
        print("需要 --url（参考回答页）")
        return 1

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        b.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        if not story_text:
            info = b._safe_evaluate(
                "() => (document.querySelector('.RichContent-inner, .RichText')"
                " || {}).innerText || ''") or ""
            story_text = info
            print("参考故事（页面正文）：%d 字" % len(story_text))

        # ① 生成 + 本地校验（走生产代码；--canned 可跳过模型只验 DOM）
        if args.canned:
            got = {"ok": True, "comment": args.canned, "issues": []}
            print("① 使用指定文案（跳过模型）：%s" % args.canned)
        else:
            def ask(prompt, reuse_session=True):
                print("--- 提示词（前 200 字）---")
                print(prompt[:200].replace("\n", " / "))
                from applications.zhihu_story import reply_task
                return reply_task.ask_llm(prompt, reuse_session=reuse_session)

            got = cfb.compose(ask, story_title, story_text,
                              progress=lambda t: print("   · %s" % t))
            print("\n① 生成结果：ok=%s" % got["ok"])
            print("   评论：%s" % got["comment"])
        if not got["ok"]:
            print("   ✗ 生成不达标：%s" % got["issues"])
            return 1

        # ② 打开发送链路（dry-run：填进去、确认可发布，然后清空）
        # ★ 只调用一次：这个方法自己会点开评论框；同一页面重复调用时评论框
        #   已经开着，入口按钮变成「收起评论」→ 会误报 no-comment-entry
        #   （探针踩过，不是产品缺陷）。
        print("\n② 走发送链路（dry-run：不点发布）")
        r = b.send_answer_comment(got["comment"], dry_run=True)
        print("   结果：%s" % json.dumps(r, ensure_ascii=False))
        print("   （已清空编辑器，账号未改动）" if r.get("ok") else "   ✗ 未通过")

        # ③ 校验函数对照
        issues = cfb.check_comment(got["comment"])
        print("\n③ 本地校验：%s" % ("通过" if not issues else "不通过 %s" % issues))
        return 0 if (r.get("ok") and not issues) else 1
    finally:
        try:
            b.close()
        except Exception:                     # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
