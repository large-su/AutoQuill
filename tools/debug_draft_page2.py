# 临时探测脚本2：草稿条目结构 + 删除按钮 + 确认弹窗
# 只点开删除确认弹窗（不点确认），dump 弹窗结构后按 Esc 取消。
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

# 找所有含"删除"文本（含零宽空格）的元素 → dump 条目结构
DEL_JS = """() => {
  const out = [];
  const all = Array.from(document.querySelectorAll('div,button,span,li,label,a,[role=button]'));
  for (const el of all) {
    const t = (el.innerText || '').trim();
    if (t.replace(/\\u200b/g, '').trim() === '删除') {
      const chain = [];
      let p = el;
      for (let i = 0; i < 8 && p; i++) {
        chain.push({tag: p.tagName, cls: p.className ? String(p.className).slice(0, 100) : ''});
        p = p.parentElement;
      }
      out.push(chain);
      if (out.length >= 3) break;
    }
  }
  return out;
}"""

# 找页面所有模态弹窗（Modal）结构
MODAL_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll(
      '[class*=Modal], [class*=modal], [class*=Dialog], [class*=dialog], [class*=Popover], [role=dialog]')) {
    const t = (el.innerText || '').trim().slice(0, 200);
    out.push({cls: el.className ? String(el.className).slice(0, 90) : el.tagName, text: t});
  }
  return out;
}"""


def main():
    from applications.zhihu_story.browser_adapter import get_browser

    browser = get_browser()
    page = browser.page
    page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)

    dels = page.evaluate(DEL_JS)
    print(f"=== 删除按钮（含零宽空格，共 {len(dels)} 组 dump，每组取 3） ===")
    for i, chain in enumerate(dels):
        print(f"--- 第 {i+1} 个删除按钮祖先链 ---")
        for c in chain:
            print(f"  {c['tag']}  cls={c['cls']!r}")

    # 点击第一个删除按钮（不点确认），看弹窗
    if dels:
        print("\n=== 点击第 1 个删除按钮（仅弹窗，不确认） ===")
        # 用 JS 直接 click 叶子（含删除文本的元素）
        page.evaluate("""() => {
          const all = Array.from(document.querySelectorAll('div,button,span,li,label,a,[role=button]'));
          const el = all.find(x => (x.innerText || '').replace(/\\u200b/g, '').trim() === '删除');
          if (el) el.click();
        }""")
        time.sleep(1.5)
        print("\n=== 点击后页面弹窗 ===")
        for m in page.evaluate(MODAL_JS):
            print(f"  cls={m['cls']!r}")
            print(f"    text={m['text']!r}")
        # 按 Esc 取消（不真删）
        page.keyboard.press("Escape")
        time.sleep(1)
        print("\n已按 Esc 取消")

    print("\n页面保持打开 15 秒供人工查看…")
    time.sleep(15)
    browser.close()


if __name__ == "__main__":
    main()
