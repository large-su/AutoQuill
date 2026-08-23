# ============================================================
# tools/smoke_dom_workflow.py — 知乎 DOM 工作流冒烟验证
#
# 不调用鼠标/坐标/OCR，走完整 workflow 语义接口验证：
#   1. 登录态检测（z_c0 cookie）
#   2. 推荐页 DOM 解析 + 评分 + 选题（不发布）
#   3. 问题页可回答性检测 + 首答 DOM 提取
#   4. 作者技能 profile 加载（AUTHOR_PROFILE 注入链路）
#
# 运行：python tools/smoke_dom_workflow.py [问题URL]
# ============================================================

import sys

sys.path.insert(0, ".")

from workflows.zhihu import ZhihuWorkflow


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    results = {}

    wf = ZhihuWorkflow()
    browser = wf._browser()

    # [1] 登录态
    logged = browser.is_logged_in()
    results["登录态"] = logged
    print(f"[1] 登录态（z_c0 cookie）: {logged}")

    # [2] 选题链路
    if url:
        browser.open_question(url)
        chosen = url
        print(f"[2] 使用给定问题页（跳过选题）: {url}")
    else:
        chosen = wf.select_topic()
        results["选题"] = bool(chosen and "/question/" in chosen)
        print(f"[2] 选题 → {chosen}")

    # [3] 可回答性检测（DOM）
    can, reason = browser.check_answerable()
    results["可回答性检测"] = can
    print(f"[3] 可回答性检测: {can}（{reason}）")

    # [4] 首答 DOM 提取
    data = browser.get_primary_answer(min_length=1)
    ok = bool(data and data.get("answer") and len(data["answer"]) >= 500)
    results["首答提取"] = ok
    if data:
        footer = data.get("footer") or {}
        print(f"[4] 首答提取: 标题「{data.get('title', '')[:30]}...」 "
              f"{len(data.get('answer') or '')}字 "
              f"赞={footer.get('likes')} 评={footer.get('comments')}")
    else:
        print("[4] 首答提取失败")

    # [5] 作者技能注入链路（不实际生成）
    from config.story import AUTHOR_PROFILE
    from llm_api import _load_author_profile_or_none
    profile = _load_author_profile_or_none(AUTHOR_PROFILE)
    results["作者技能注入"] = bool(profile)
    if profile:
        sig = profile.get("signature") or {}
        excerpts = sig.get("excerpts") or {}
        fields = [k for k, v in sig.items() if v]
        print(f"[5] 作者技能注入: 「{profile.get('author', '?')}」profile "
              f"已就绪（{len(fields)} 个技能字段，"
              f"{len(excerpts)} 段文风摘录）")
    else:
        print(f"[5] 作者技能注入: 未找到 profile（AUTHOR_PROFILE={AUTHOR_PROFILE!r}）")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n===== 冒烟验证: {passed}/{total} PASS =====")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
