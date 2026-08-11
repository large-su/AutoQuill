# ============================================================
# tools/collect_author.py — 作者页多故事批量采集
#
# 从作者回答列表页（zhihu.com/people/{token}/answers）批量采集
# 该作者的故事。纯 UIA 通道（不动鼠标），逐项读取列表里的
# 问题链接 → 点击进入回答页 → 复用提取接缝读全文 → 返回列表
# 继续，直到指定数量或列表读尽。
#
# 用法：
#   1. Edge 打开作者回答列表页（已登录，窗口可见）
#   2. python tools/collect_author.py [--count N] [--out 文件]
#
# 架构位置：Layer 3 采集通道 — 作者维度编排（tools/ 工具层）
# ============================================================

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications.zhihu_story.a11y_probe import (
    _find_edge_window,
    _normalize_ui_text,
    _read_live_web_records,
    extract_live_primary_answer,
)

BACK_NAVIGATION_TIMEOUT = 12.0  # 返回作者主页后的等待超时
LIST_TAB_TIMEOUT = 10.0         # 等待列表出现 / 点击"回答"Tab 后的等待


def _find_author_name(records):
    """从记录里找当前详情页作者名（UserLink-link 链接文本）。"""
    for r in records:
        if r["type"] == "HyperlinkControl" and "UserLink-link" in r["class_name"]:
            name = _normalize_ui_text(r["name"])
            if name:
                return name
    return None


def _click_author_link(automation, window, author_name):
    """在详情页点击作者名链接，返回作者主页。"""
    _, records = _read_live_web_records()
    for r in records:
        if r["type"] == "HyperlinkControl" and "UserLink-link" in r["class_name"]:
            if _normalize_ui_text(r["name"]) == author_name:
                left, top, right, bottom = r["rect"]
                cx, cy = (left + right) // 2, (top + bottom) // 2
                ctrl = automation.ControlFromPoint(cx, cy)
                if ctrl is not None:
                    try:
                        ctrl.Click(simulateMove=False)
                        return True
                    except Exception:
                        pass
    return False


def _wait_for_list(automation, timeout):
    """等待页面变成回答列表页（出现 List-item 或回答链接）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _, records = _read_live_web_records()
            if any("List-item" in r["class_name"] for r in records):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _find_answer_links(records):
    """从 UIA 记录中提取 (问题标题, 回答链接) 列表。"""
    links = []
    for r in records:
        val = r.get("value") or ""
        if r["type"] == "HyperlinkControl" and "/answer/" in val:
            title = _normalize_ui_text(r["name"])
            if title and title not in {t for t, _ in links}:
                links.append((title, val))
    return links


def _click_answer(automation, window, answer_url):
    """在当前列表 UIA 树上找到该回答链接控件并点击进入详情页。"""
    _, records = _read_live_web_records()
    target = None
    for r in records:
        if r["type"] == "HyperlinkControl" and r.get("value") == answer_url:
            # 找到对应的真实控件（在窗口子树里按 AutomationId/位置匹配）
            target = r
            break
    if target is None:
        return False

    # 通过 uiautomation 在窗口中按 rect 找到控件并 Invoke
    left, top, right, bottom = target["rect"]
    cx, cy = (left + right) // 2, (top + bottom) // 2
    ctrl = automation.ControlFromPoint(cx, cy)
    if ctrl is None:
        return False
    try:
        ctrl.GetClickablePoint()
        ctrl.Click(simulateMove=False)
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="作者页多故事批量采集（UIA 通道）")
    parser.add_argument("--count", type=int, default=5, help="最多采集篇数（默认 5）")
    parser.add_argument("--out", default=os.path.join("data", "collected_stories.jsonl"),
                        help="输出 JSONL 文件（默认 data/collected_stories.jsonl）")
    args = parser.parse_args()

    import uiautomation as automation
    window = _find_edge_window(automation)

    # 已采集的回答链接（断点续采，避免重复）
    done = set()
    if os.path.exists(args.out):
        for line in open(args.out, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("answer_url"):
                    done.add(rec["answer_url"])
            except json.JSONDecodeError:
                continue

    collected = []
    author_name = None
    try:
        while len(collected) < args.count:
            # 1. 读当前列表页的链接
            _, records = _read_live_web_records()
            links = _find_answer_links(records)
            print(f"  列表页发现 {len(links)} 个回答链接（已采 {len(collected)}/{args.count}）")

            fresh = [(t, u) for t, u in links if u not in done]
            if not fresh:
                print("  列表已读完，停止。")
                break

            for title, url in fresh:
                if len(collected) >= args.count:
                    break

                # 2. 点击进入详情页
                if not _click_answer(automation, window, url):
                    print(f"  ⚠ 无法点击：{title[:30]}")
                    done.add(url)  # 跳过，避免死循环
                    continue

                # 3. 等待详情页出现并提取全文（返回 4 元组，末位是 reason）
                time.sleep(1.0)
                story_title, answer, footer, _reason = extract_live_primary_answer(
                    min_length=200,
                    wait_timeout=10.0,
                    poll_interval=0.5,
                )
                if not (story_title and answer):
                    print(f"  ⚠ 详情页未读到内容：{title[:30]}（可能无全文）")
                else:
                    record = {
                        "source": "author_page",
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

                # 4. 返回作者主页（点作者名链接，页面内导航）
                if author_name is None:
                    author_name = _find_author_name(records)
                if author_name:
                    _click_author_link(automation, window, author_name)
                    time.sleep(2.0)
                # 5. 等待列表页出现（必要时点击"回答" Tab）
                if not _wait_for_list(automation, BACK_NAVIGATION_TIMEOUT):
                    print("  ⚠ 返回列表页失败，尝试点击「回答」Tab")
                    _, records = _read_live_web_records()
                    for r in records:
                        if r["type"] == "HyperlinkControl" and r["class_name"] == "Tabs-link":
                            name = _normalize_ui_text(r["name"])
                            if name == "回答":
                                left, top, right, bottom = r["rect"]
                                ctrl = automation.ControlFromPoint(
                                    (left + right) // 2, (top + bottom) // 2)
                                if ctrl is not None:
                                    try:
                                        ctrl.Click(simulateMove=False)
                                    except Exception:
                                        pass
                                break
                    if not _wait_for_list(automation, LIST_TAB_TIMEOUT):
                        print("  ❌ 无法返回列表页，停止（下次可从断点继续）")
                        break
    except KeyboardInterrupt:
        print("\n  用户中断，保存已采集内容。")
    finally:
        print(f"\n  完成：本次新增 {len(collected)} 篇，累计输出 {args.out}")

if __name__ == "__main__":
    main()
