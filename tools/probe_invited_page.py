#!/usr/bin/env python3
"""探测知乎创作中心「邀请回答」页 DOM 结构（选题三模式调研用）。

用项目自身登录态浏览器打开邀请页，验证：
  1. 现有 _RECOMMEND_QUESTIONS_JS 能否直接提取候选（标题/链接/互动）
  2. 页面结构证据：卡片容器类名、问题链接数量、首张卡片 HTML

用法：python tools/probe_invited_page.py
"""
import json
import sys
import time

INVITED_URL = "https://www.zhihu.com/creator/featured-question/invited"


def main():
    from applications.zhihu_story.browser_adapter import (
        close_shared_browser, get_browser, _RECOMMEND_QUESTIONS_JS)

    browser = get_browser()
    try:
        browser.page.goto(INVITED_URL, wait_until="domcontentloaded",
                          timeout=60000)
        time.sleep(2)
        browser.page.evaluate("() => window.scrollBy(0, 800)")
        time.sleep(1)

        print("=" * 60)
        print("页面基本信息")
        info = browser.page.evaluate("""() => ({
          url: location.href,
          title: document.title,
          logged_in: !!document.cookie.includes('z_c0'),
          body_head: document.body ? document.body.innerText.slice(0, 300) : '',
        })""")
        print(json.dumps(info, ensure_ascii=False, indent=2))

        print("=" * 60)
        print("尝试现有 _RECOMMEND_QUESTIONS_JS 提取")
        items = browser._safe_evaluate(_RECOMMEND_QUESTIONS_JS) or []
        print(f"提取到 {len(items)} 个候选")
        for it in items[:10]:
            print(json.dumps(it, ensure_ascii=False))

        print("=" * 60)
        print("DOM 结构证据")
        dump = browser.page.evaluate("""() => {
          const out = { question_links: document.querySelectorAll('a[href*="/question/"]').length };
          const cls = {};
          for (const sel of ['.ToolsQuestion', '.TopstoryItem', '.List-item',
                             '.QuestionItem', '.ContentItem', '.QuestionCard',
                             '.FeatureList-item', '.InviteItem', 'table tr']) {
            cls[sel] = document.querySelectorAll(sel).length;
          }
          out.card_classes = cls;
          const first = document.querySelector('.ToolsQuestion, .List-item, .QuestionItem, .ContentItem, table tr');
          out.first_card_html = first ? first.outerHTML.slice(0, 1800) : null;
          return out;
        }""")
        print(json.dumps(dump, ensure_ascii=False, indent=2))
    finally:
        close_shared_browser()


if __name__ == "__main__":
    sys.exit(main())
