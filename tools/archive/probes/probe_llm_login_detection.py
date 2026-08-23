# ============================================================
# tools/probe_llm_login_detection.py — 验证 DeepSeek 登录检测依据
#
# 修复后的 web_llm_logged_in() 判定 = deepseek.com cookie 存在
# + 页面 URL 不含 sign_in（仅查 cookie 会因过期残留假阳性）。
# 本脚本验证该判定的对外依赖（真实网站行为）：
#   1. 独立临时 context（不碰真实 profile）塞一个假 deepseek cookie
#   2. 真实访问 chat.deepseek.com
#   3. 打印最终 URL 与「未登录」判定结果
#
# 运行：python tools/probe_llm_login_detection.py
# ============================================================

import sys

FAKE_COOKIE = {
    "name": "probe_fake_session",
    "value": "invalid-residue",
    "domain": "chat.deepseek.com",
    "path": "/",
}


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        try:
            ctx = browser.new_context()
            ctx.add_cookies([FAKE_COOKIE])
            page = ctx.new_page()
            page.goto("https://chat.deepseek.com",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)  # 等 SPA 跳转登录页
            url = page.url
            print(f"最终 URL：{url}")
            print(f"判定（URL 不含 sign_in → 已登录）："
                  f"{'sign_in' not in url}")
            if "sign_in" in url:
                print("[OK] 证据成立：cookie 残留但未登录时，"
                      "URL 停在 sign_in → 检测逻辑有效")
                return 0
            print("[FAIL] 未停到 sign_in（页面结构可能变化或已自动"
                  "登录），需重新探测")
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
