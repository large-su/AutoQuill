# 临时探测脚本：知乎创作中心草稿箱页面结构
# 目的：定位草稿列表容器、每篇草稿条目的删除交互（按钮/勾选框）、
#       删除确认弹窗、分页结构，为写批量删除脚本做准备。
# 用法：PYTHONIOENCODING=utf-8 python tools/debug_draft_page.py
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

# 页面整体结构：列表容器 + 条目数 + 按钮文本分布
STRUCT_JS = """() => {
  const out = {};
  // 常见列表容器候选
  const listCands = [
    "div[class*='Draft']", "div[class*='draft']",
    "div[class*='List']", "div[class*='list']",
    "main", "[class*='creator']",
  ];
  const hits = [];
  for (const s of listCands) {
    const els = document.querySelectorAll(s);
    if (els.length) hits.push({sel: s, n: els.length});
  }
  out.listCandidates = hits;

  // 所有按钮的可见文本（找删除/全选/翻页）
  const btns = [];
  for (const el of document.querySelectorAll('button, [role=button]')) {
    const t = (el.innerText || '').trim().slice(0, 20);
    if (t) btns.push(t);
  }
  out.buttons = btns.slice(0, 40);

  // 输入框（勾选框 checkbox）
  const checks = [];
  for (const el of document.querySelectorAll('input[type=checkbox]')) {
    checks.push({checked: el.checked, name: el.name || ''});
  }
  out.checkboxes = checks;

  // 页面可见文本片段（顶部）
  out.bodyText = document.body.innerText.slice(0, 600);
  return out;
}"""

# 候选条目：找含"删除"文本的可点击元素 → dump 祖先链
DEL_JS = """() => {
  const out = [];
  const all = Array.from(document.querySelectorAll(
      'div,button,span,li,label,a,[role=button]'));
  for (const el of all) {
    const t = (el.innerText || '').trim();
    if (t === '删除' || t === '删 除') {
      const chain = [];
      let p = el;
      for (let i = 0; i < 6 && p; i++) {
        chain.push({tag: p.tagName, cls: p.className ? String(p.className).slice(0, 80) : ''});
        p = p.parentElement;
      }
      out.push({chain});
    }
  }
  return out.slice(0, 6);
}"""


def main():
    from applications.zhihu_story.browser_adapter import get_browser, _check_cancel

    browser = get_browser()
    page = browser.page
    print(f"当前页: {page.url}")
    print(f"导航到草稿箱: {DRAFT_URL}")
    page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=20000)
    # 等 SPA 渲染
    for _ in range(10):
        time.sleep(1)
        r = page.evaluate(STRUCT_JS)
        if r["listCandidates"] and (r["buttons"] or r["bodyText"]):
            break

    r = page.evaluate(STRUCT_JS)
    print("\n=== 列表容器候选 ===")
    for h in r["listCandidates"]:
        print(f"  {h['sel']}: {h['n']} 个")
    print("\n=== 页面按钮文本 ===")
    print(" ", r["buttons"])
    print("\n=== 勾选框 ===")
    print(" ", r["checkboxes"])
    print("\n=== 页面文本片段 ===")
    print(" ", r["bodyText"][:400])

    print("\n=== 删除按钮位置（前 6 个） ===")
    dels = page.evaluate(DEL_JS)
    for i, d in enumerate(dels):
        print(f"  [{i}] 祖先链:")
        for c in d["chain"]:
            print(f"      {c['tag']}  cls={c['cls']!r}")
    if not dels:
        print("  未找到纯文本'删除'按钮（可能需 hover 或删除在弹层）")

    # 保持浏览器打开，人工可查看
    print("\n页面已打开，可在 Edge 中人工查看。10 秒后关闭…")
    time.sleep(10)
    browser.close()


if __name__ == "__main__":
    main()
