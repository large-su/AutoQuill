# ============================================================
# tools/collect_author_pw.py — 作者页多故事批量采集（Playwright 通道）
#
# 从作者回答列表页（zhihu.com/people/{token}/answers）批量采集
# 该作者的故事。通过 Playwright MCP（browser_evaluate / browser_tabs）
# 操作已连接的 Edge：读列表链接 → 新开标签页进详情 → 提取全文 →
# 写 JSONL → 关闭标签页返回列表，直到指定数量或列表读尽。
#
# 用法：
#   1. 确保 Playwright MCP 已连接 Edge（重启 Claude Code 会话）
#   2. Edge 打开作者回答列表页（已登录）
#   3. 在本会话内运行：python tools/collect_author_pw.py [--count N]
#
# 注意：本脚本通过 MCP 工具运行，需从 Claude Code 会话内调用
# （stdin/stdout 由 MCP 连接，不能在普通终端独立执行）。
#
# 架构位置：Layer 3 采集通道 — 作者维度编排（Playwright 变体）
# ============================================================

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications.zhihu_story.browser_adapter import _PRIMARY_ANSWER_JS as _EXTRACT_JS


# 列表页提取 JS：收集所有回答链接（问题标题 + URL）
_LIST_JS = r"""
() => {
  const links = Array.from(document.querySelectorAll('h2.ContentItem-title a[href*="/answer/"]'));
  const seen = new Set();
  const out = [];
  for (const a of links) {
    const url = a.href.split('#')[0];
    if (seen.has(url)) continue;
    seen.add(url);
    out.push({ title: a.textContent.trim(), url });
  }
  return out;
}
"""

# 滚动列表 JS：滚动到底部加载更多（返回是否还有更多可加载）
_SCROLL_JS = r"""
() => {
  const before = document.querySelectorAll('h2.ContentItem-title a[href*="/answer/"]').length;
  window.scrollTo(0, document.body.scrollHeight);
  // 返回当前总链接数，供比较
  return before;
}
"""


def main():
    parser = argparse.ArgumentParser(description="作者页多故事批量采集（Playwright 通道）")
    parser.add_argument("--count", type=int, default=5, help="最多采集篇数（默认 5）")
    parser.add_argument("--out", default=os.path.join("data", "collected_stories.jsonl"),
                        help="输出 JSONL 文件（默认 data/collected_stories.jsonl）")
    parser.add_argument("--author", default="", help="作者名（写入记录，可选）")
    args = parser.parse_args()

    # 已采集的回答链接（断点续采）
    done = set()
    if os.path.exists(args.out):
        for line in open(args.out, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                footer = rec.get("footer") or {}
                url = footer.get("answer_url") or rec.get("answer_url")
                if url:
                    done.add(url)
            except json.JSONDecodeError:
                continue

    collected = []
    try:
        while len(collected) < args.count:
            # 1. 读当前列表页的回答链接
            links = _evaluate(_LIST_JS)
            print(f"  列表页发现 {len(links)} 个回答链接（已采 {len(collected)}/{args.count}）")

            fresh = [(t, u) for t, u in links if u not in done]
            if not fresh:
                print("  列表已读完，尝试滚动加载更多...")
                before = _evaluate(_SCROLL_JS)
                time.sleep(2.0)
                links2 = _evaluate(_LIST_JS)
                fresh = [(t, u) for t, u in links2 if u not in done]
                if not fresh:
                    print("  滚动后仍无新链接，停止。")
                    break

            for title, url in fresh:
                if len(collected) >= args.count:
                    break

                # 2. 新开标签页进详情
                print(f"  打开详情：{title[:30]}...")
                _open_tab(url)
                time.sleep(1.5)

                # 3. 提取全文
                story_title, answer, footer = _extract()
                if not (story_title and answer):
                    print(f"  ⚠ 详情页未读到内容：{title[:30]}（可能无全文）")
                else:
                    record = {
                        "source": "author_page",
                        "author": args.author,
                        "title": story_title,
                        "answer": answer,
                        "footer": footer,
                        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    with open(args.out, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    likes = (footer or {}).get("likes")
                    print(f"  ✓ [{len(collected) + 1}/{args.count}] {story_title[:32]} "
                          f"({len(answer)} 字, 赞同={likes})")
                    collected.append(record)

                done.add(url)

                # 4. 关闭详情标签页，回列表
                _close_tab()
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n  用户中断，保存已采集内容。")
    finally:
        print(f"\n  完成：本次新增 {len(collected)} 篇，累计输出 {args.out}")
        # 输出已采集链接，供断点续采核对
        if collected:
            print("  新增回答 URL：")
            for rec in collected:
                print(f"    {rec['footer'].get('answer_url')}")


def _evaluate(js):
    """通过 Playwright MCP 执行 JS 并返回结果。"""
    raise NotImplementedError("由会话内注入（见 run 函数）")


def _open_tab(url):
    raise NotImplementedError("由会话内注入（见 run 函数）")


def _close_tab():
    raise NotImplementedError("由会话内注入（见 run 函数）")


def _extract():
    """提取当前详情页（复用 browser_adapter 的已验证 JS）。"""
    result = _evaluate(_EXTRACT_JS)
    title = (result.get("title") or "").strip()
    answer = (result.get("answer") or "").strip()
    footer = result.get("footer") or {}
    if title and answer:
        return title, answer, footer
    return "", "", None


def run(evaluate, open_tab, close_tab, **kwargs):
    """会话内入口：注入 MCP 工具函数后执行主流程。

    evaluate: (js) -> 返回值
    open_tab: (url) -> None（新开标签页并切过去）
    close_tab: () -> None（关闭当前标签页）
    """
    global _evaluate, _open_tab, _close_tab
    _evaluate = evaluate
    _open_tab = open_tab
    _close_tab = close_tab
    main(**kwargs)


if __name__ == "__main__":
    print("本脚本需在 Claude Code 会话内通过 run() 注入 MCP 工具执行。")
