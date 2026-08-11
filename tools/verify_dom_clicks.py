# ============================================================
# tools/verify_dom_clicks.py — DOM 指令驱动点击链路验证
#
# 验证目标：在知乎问题页上，完全不调用鼠标/坐标/OCR，
# 仅通过浏览器内部 DOM 指令完成按钮点击：
#   1. 关注问题（点击 → 变已关注 → 再点击取消 → 恢复原状）
#   2. 写回答（点击 → 编辑器出现 → Esc 关闭）
# 关注按钮点击两次是为了恢复账号原状，可安全重跑。
#
# 运行：python tools/verify_dom_clicks.py [问题页URL]
# ============================================================

import json
import sys

sys.path.insert(0, ".")
from applications.zhihu_story.browser_adapter import ZhihuBrowser

DEFAULT_URL = ("https://www.zhihu.com/question/483237921/"
               "answer/1975536805026738478")


def follow_states(browser):
    return browser.page.evaluate("""() =>
      Array.from(document.querySelectorAll('button.FollowButton'))
        .map(el => (el.textContent || '').trim()).filter(Boolean)""")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    results = {}

    with ZhihuBrowser() as b:
        b.page.goto(url, wait_until="domcontentloaded")
        b.page.wait_for_timeout(4000)
        print(f"页面: {b.page.title()}")
        print("FollowButton 初始状态:", follow_states(b))

        # [1] 关注问题
        print("\n[1] DOM click 关注问题")
        b.click(text="关注问题")
        b.page.wait_for_timeout(1500)
        s1 = follow_states(b)
        results["关注"] = s1 and s1[0] == "已关注"
        print(f"    点击后: {s1}  -> {'PASS' if results['关注'] else 'FAIL'}")

        # [2] 取消关注（恢复原状）
        print("[2] DOM click 已关注（取消）")
        b.click(text="已关注")
        b.page.wait_for_timeout(1500)
        s2 = follow_states(b)
        results["取消"] = s2 and s2[0] == "关注问题"
        print(f"    取消后: {s2}  -> {'PASS' if results['取消'] else 'FAIL'}")

        # [3] 写回答
        print("[3] DOM click 写回答")
        b.click(text="写回答")
        b.page.wait_for_timeout(2500)
        editor = b.page.evaluate("""() => {
          const el = document.querySelector(
            '[contenteditable="true"], .Editable, .AnswerForm-editor');
          return el ? true : false;
        }""")
        results["写回答"] = editor
        print(f"    编辑器出现: {editor}  -> {'PASS' if editor else 'FAIL'}")
        if editor:
            b.page.keyboard.press("Escape")
            b.page.wait_for_timeout(800)
            print("    已按 Esc 关闭编辑器（不留草稿）")

    passed = sum(results.values())
    total = len(results)
    print(f"\n===== 验证结果: {passed}/{total} PASS =====")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
