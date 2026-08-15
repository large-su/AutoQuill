"""探测 chat.deepseek.com 回复容器能否拿到原始 markdown。

目标：确认 Web 通道 read_result 用 innerText 提取时丢失的
`## **N**` 章节标题，是否可以通过 DOM 内部结构找回原文。

用法：python -m tools.probe_deepseek_markdown
（需 Edge 持久化 profile 已登录 DeepSeek；自动点开「古言换妻反转」会话）
"""

import json
import sys


def main():
    from applications.zhihu_story.browser_adapter import get_browser
    browser = get_browser()
    page = browser.context.new_page()
    page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded",
              timeout=30000)
    page.wait_for_timeout(6000)

    # 打开一个有故事回复的历史会话
    page.evaluate(
        """() => {
          const items = Array.from(document.querySelectorAll(
            'div[class*="conversation"], div[class*="session"], li, a'));
          const el = items.find(i =>
            (i.innerText || '').trim().startsWith('古言换妻反转'));
          if (el) el.click();
          return !!el;
        }"""
    )
    page.wait_for_timeout(5000)
    print(f"\n=== DeepSeek 回复容器探测: {page.title()} ===")

    # 1) 正文容器结构（哈希 class 环境）
    info = page.evaluate(
        """() => {
          const sels = [
            "div[class*='ds-assistant-message-main-content']",
            "div[class*='ds-markdown']",
            "div[class*='message'] div[class*='markdown']",
          ];
          let el = null, used = null;
          for (const s of sels) {
            const hit = document.querySelector(s);
            if (hit) { el = hit; used = s; break; }
          }
          if (!el) return {found: false};
          const attrs = {};
          for (const a of el.attributes) attrs[a.name] = a.value.slice(0, 120);
          return {
            found: true, selector: used,
            className: el.className.toString().slice(0, 160),
            attrs: attrs,
            innerTextLen: el.innerText.length,
            innerTextHead: el.innerText.slice(0, 80).replace(/\\n/g, '|'),
            outerHTMLHead: el.outerHTML.slice(0, 260),
          };
        }"""
    )
    print("\n--- 1) 正文容器 ---")
    print(json.dumps(info, ensure_ascii=False, indent=1))

    if not info.get("found"):
        cand = page.evaluate(
            """() => {
              const all = Array.from(document.querySelectorAll('div'));
              return all
                .filter(d => (d.innerText || '').length > 800)
                .slice(0, 5)
                .map(d => ({
                  cls: (d.className || '').toString().slice(0, 90),
                  len: (d.innerText || '').length,
                  head: (d.innerText || '').slice(0, 50).replace(/\\n/g, '|'),
                }));
            }"""
        )
        print("\n--- 1b) 长文本容器候选 ---")
        print(json.dumps(cand, ensure_ascii=False, indent=1))

    # 2) React fiber 里找原始 markdown 内容
    fiber_info = page.evaluate(
        """() => {
          const el = document.querySelector(
            "div[class*='ds-assistant-message-main-content'], "
            "div[class*='ds-markdown']");
          if (!el) return {found: false};
          const key = Object.keys(el).find(k => k.startsWith('__reactFiber'));
          if (!key) return {found: false};
          let node = el[key], depth = 0;
          const cands = [];
          while (node && depth < 40) {
            const p = node.memoizedProps || {};
            const children = p.children;
            if (typeof children === 'string' && children.length > 200
                && children.indexOf('##') >= 0) {
              cands.push({depth: depth, len: children.length,
                          head: children.slice(0, 150).replace(/\\n/g, '|')});
            }
            node = node.return;
            depth += 1;
          }
          return {found: true, cands: cands.slice(0, 3)};
        }"""
    )
    print("\n--- 2) React fiber 原始内容 ---")
    print(json.dumps(fiber_info, ensure_ascii=False, indent=1))

    # 3) 复制按钮 + 剪贴板（DeepSeek 回复带复制按钮）
    copy_info = page.evaluate(
        """() => {
          const btns = Array.from(document.querySelectorAll('button'))
            .filter(b => /复制|cop/i.test(b.innerText || ''));
          return {count: btns.length};
        }"""
    )
    print("\n--- 3) 复制按钮数 ---")
    print(json.dumps(copy_info, ensure_ascii=False, indent=1))
    if copy_info.get("count", 0) > 0:
        clip = page.evaluate(
            """async () => {
              const btns = Array.from(document.querySelectorAll('button'))
                .filter(b => /复制|cop/i.test(b.innerText || ''));
              btns[btns.length - 1].click();
              await new Promise(r => setTimeout(r, 800));
              try {
                const t = await navigator.clipboard.readText();
                return {ok: true, len: t.length,
                        head: t.slice(0, 120).replace(/\\n/g, '|')};
              } catch (e) {
                return {ok: false, err: String(e)};
              }
            }"""
        )
        print("\n--- 3b) 剪贴板内容 ---")
        print(json.dumps(clip, ensure_ascii=False, indent=1))
    page.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"探测失败：{exc}", file=sys.stderr)
        sys.exit(1)
