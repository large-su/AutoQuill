# 临时探测脚本：DeepSeek 网页版大模式 tab 的真实文本与 radiogroup 结构
# 目的：定位「快速/专家/识图」模式 tab 的实际 DOM，修订 setup 的模式切换。
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications.zhihu_story.browser_adapter import get_browser

DUMP_JS = """() => {
  const out = {radiogroups: [], modeTexts: [], toggles: []};
  // 1. 所有 radiogroup 结构
  for (const g of document.querySelectorAll('[role=radiogroup]')) {
    const radios = [];
    for (const r of g.querySelectorAll('[role=radio]')) {
      radios.push({
        checked: r.getAttribute('aria-checked'),
        text: (r.innerText || '').trim().slice(0, 30),
        cls: (r.className || '').toString().slice(0, 60),
      });
    }
    out.radiogroups.push(radios);
  }
  // 2. 含「模式」字样的可见元素（找 leaf）
  const all = Array.from(document.querySelectorAll('div,span,button'));
  for (const el of all) {
    const t = (el.textContent || '').trim();
    if (t.includes('模式') && t.length < 12
        && !Array.from(el.children).some(c =>
            (c.textContent || '').includes('模式'))
        && el.offsetParent !== null) {
      out.modeTexts.push({text: t, cls: (el.className || '').toString().slice(0, 60)});
    }
  }
  // 3. 开关
  for (const el of document.querySelectorAll('[class*=ds-toggle-button]')) {
    out.toggles.push({
      text: (el.innerText || '').trim().slice(0, 20),
      selected: String(el.className).includes('--selected'),
      cls: (el.className || '').toString().slice(0, 80),
    });
  }
  return out;
}"""


def main():
    from web_drivers import create_driver
    driver = create_driver()
    driver.open_session()

    page = driver._page_instance()
    time.sleep(2)

    print("=== 当前 radiogroup / 模式文本 / 开关 ===")
    r = page.evaluate(DUMP_JS)
    print("radiogroups:")
    for grp in r["radiogroups"]:
        for rd in grp:
            print(f"  [role=radio] checked={rd['checked']!r} text={rd['text']!r} cls={rd['cls']!r}")
    if not r["radiogroups"]:
        print("  （无 radiogroup）")
    print("含「模式」文本的 leaf 元素:")
    for m in r["modeTexts"]:
        print(f"  {m['text']!r}  cls={m['cls']!r}")
    print("开关:")
    for t in r["toggles"]:
        print(f"  {t['text']!r} selected={t['selected']} cls={t['cls']!r}")

    # 尝试点击「专家模式」
    print("\n=== 尝试点击「专家模式」 ===")
    clicked = page.evaluate("""() => {
      const all = Array.from(document.querySelectorAll('div,span,button,[role=tab],[role=radio]'));
      const leaf = all.find(el =>
          (el.textContent || '').includes('专家模式') &&
          !Array.from(el.children).some(c =>
              (c.textContent || '').includes('专家模式')) &&
          el.offsetParent !== null);
      if (!leaf) return 'no-leaf';
      let target = leaf;
      let p = leaf.parentElement;
      while (p) {
        const t = p.tagName.toLowerCase();
        if (t === 'button' || p.getAttribute('role') === 'tab'
            || p.getAttribute('role') === 'radio'
            || /toggle/.test(String(p.className))) {
          target = p; break;
        }
        p = p.parentElement;
      }
      target.click();
      return 'clicked:' + target.tagName + ':' + (target.className || '').toString().slice(0, 40);
    }""")
    print("点击结果:", clicked)
    time.sleep(2)

    print("\n=== 点击后 radiogroup 状态 ===")
    r2 = page.evaluate(DUMP_JS)
    for grp in r2["radiogroups"]:
        for rd in grp:
            print(f"  [role=radio] checked={rd['checked']!r} text={rd['text']!r}")

    print("\n页面保持打开 15 秒供人工查看…")
    time.sleep(15)
    driver.close()


if __name__ == "__main__":
    main()
