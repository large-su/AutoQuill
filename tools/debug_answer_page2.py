# 临时探测脚本2：回答条目结构与统计字段
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = "https://www.zhihu.com/creator/manage/creation/answer"

# 找一个回答条目的祖先链：从"分享"按钮往上找容器
ENTRY_JS = """() => {
  const all = Array.from(document.querySelectorAll('div,button,span,li,a'));
  const share = all.find(el =>
      (el.innerText || '').replace(/\\u200b/g, '').trim() === '分享' &&
      el.offsetParent !== null);
  if (!share) return null;
  const chain = [];
  let p = share;
  for (let i = 0; i < 9 && p; i++) {
    chain.push({tag: p.tagName, cls: p.className ? String(p.className).slice(0, 110) : ''});
    p = p.parentElement;
  }
  return chain;
}"""

# 统计字段：常见 class 里的数字文本
STATS_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('[class*=Stat], [class*=stat], [class*=Meta], [class*=meta], [class*=Count], [class*=count], [class*=Views], [class*=Likes]')) {
    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    if (t && t.length < 40) out.push({cls: el.className ? String(el.className).slice(0, 70) : '', text: t});
  }
  return out.slice(0, 40);
}"""

# 所有"分享"按钮所在卡片的内容结构（dump 第 1 个卡片全文）
FIRST_CARD_JS = """() => {
  const all = Array.from(document.querySelectorAll('div,button,span,li,a'));
  const share = all.find(el =>
      (el.innerText || '').replace(/\\u200b/g, '').trim() === '分享' &&
      el.offsetParent !== null);
  if (!share) return '';
  let card = share;
  for (let i = 0; i < 6; i++) {
    card = card.parentElement;
    if (card && /Card|Item|row|Row/.test(String(card.className))) break;
  }
  return card ? (card.innerText || '').slice(0, 500) : '';
}"""


def main():
    from applications.zhihu_story.browser_adapter import get_browser

    browser = get_browser()
    page = browser.page
    page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)

    print("=== 回答条目祖先链（从分享按钮上溯） ===")
    chain = page.evaluate(ENTRY_JS)
    if chain:
        for c in chain:
            print(f"  {c['tag']}  cls={c['cls']!r}")
    else:
        print("  未找到分享按钮")

    print("\n=== 统计字段元素 ===")
    for s in page.evaluate(STATS_JS):
        print(f"  cls={s['cls']!r}  text={s['text']!r}")

    print("\n=== 第一张卡内容 ===")
    print(" ", page.evaluate(FIRST_CARD_JS))

    print("\n页面保持打开 15 秒供人工查看…")
    time.sleep(15)
    browser.close()


if __name__ == "__main__":
    main()
