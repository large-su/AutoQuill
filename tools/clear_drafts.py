# ============================================================
# tools/clear_drafts.py — 删除知乎创作中心「回答草稿箱」的全部草稿
#
# 用法（Windows，项目根目录）：
#   PYTHONIOENCODING=utf-8 python tools/clear_drafts.py --dry-run   # 只数不删
#   PYTHONIOENCODING=utf-8 python tools/clear_drafts.py --max 10    # 最多删 10 篇
#   PYTHONIOENCODING=utf-8 python tools/clear_drafts.py             # 删全部（需确认）
#
# 交互流程（实测 DOM 结构，2026-08-15）：
#   草稿卡 div.CreationManage-CreationCard → 卡内删除按钮（含零宽空格
#   ​）→ 确认弹窗 Modal（"删除后无法恢复"）→ 点「确定」→ 等弹窗消失
#   → 列表刷新，重复下一张卡。页面滚动懒加载，删除数接近当前列表末尾
#   时滚动触发加载更多。
#
# 可控性：--dry-run 预览 / --max 限量 / 默认逐篇打印进度，Ctrl+C 随时中断
# （已删的不可恢复，删除前请确认草稿确实不需要）。
# ============================================================

import argparse
import logging
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("clear_drafts")

DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

# ---- DOM 定位（已实测，2026-08-15） ----

# 确认弹窗存在？
MODAL_JS = """() => {
  const m = document.querySelector('.Modal-content');
  return m ? (m.innerText || '').includes('删除') : false;
}"""

# 点击弹窗「确定」（ModalButtonGroup 内的确定按钮，非取消）
CONFIRM_JS = """() => {
  const group = document.querySelector('.ModalButtonGroup');
  if (!group) return false;
  const btns = group.querySelectorAll('button');
  for (const b of btns) {
    if ((b.innerText || '').trim() === '确定') { b.click(); return true; }
  }
  return false;
}"""

# 页面滚动到底（触发懒加载）
SCROLL_JS = """() => {
  window.scrollTo(0, document.body.scrollHeight);
  return true;
}"""


def card_count(page):
    """当前已渲染的草稿卡数量。"""
    try:
        return page.evaluate(
            "() => document.querySelectorAll('.CreationManage-CreationCard').length")
    except Exception:
        return 0


def wait_modal(page, timeout=10):
    """等确认弹窗出现，返回 bool。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if page.evaluate(MODAL_JS):
            return True
        time.sleep(0.5)
    return False


def wait_modal_gone(page, timeout=10):
    """等确认弹窗消失（删除完成），返回 bool。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not page.evaluate(MODAL_JS):
            return True
        time.sleep(0.5)
    return False


def get_cards(page):
    """获取当前已渲染的草稿卡数量（JS 里 count，避免元素序列化丢失引用）。"""
    return page.evaluate(
        "() => document.querySelectorAll("
        "'.CreationManage-CreationCard').length") or 0


def delete_one(page):
    """删除当前列表第一张草稿卡。成功返回 True。"""
    # 全程 JS 内完成：点击第一张卡的删除按钮（元素引用不跨 evaluate 传递）
    clicked = page.evaluate("""() => {
      const card = document.querySelector('.CreationManage-CreationCard');
      if (!card) return 'no-card';
      const btns = card.querySelectorAll('button.CreationCard-ActionButton');
      for (const b of btns) {
        if ((b.innerText || '').replace(/\\u200b/g, '').trim() === '删除') {
          b.click();
          return 'clicked';
        }
      }
      return 'no-del-btn';
    }""")
    if clicked != "clicked":
        log.error("删除失败（%s），中止", clicked)
        return False
    if not wait_modal(page):
        log.error("点击删除后确认弹窗未出现，中止")
        return False
    if not page.evaluate(CONFIRM_JS):
        log.error("弹窗内未找到「确定」按钮，中止")
        return False
    if not wait_modal_gone(page):
        log.error("删除后弹窗未消失（可能失败），中止")
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="删除知乎回答草稿箱全部草稿")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计不删除")
    ap.add_argument("--max", type=int, default=0,
                    help="最多删除 N 篇（默认 0 = 全部）")
    ap.add_argument("--keep", type=int, default=0,
                    help="删到剩余 N 篇即停（默认 0 = 全部删除）")
    ap.add_argument("--interval", type=float, default=1.5,
                    help="每篇删除间隔秒（默认 1.5，网络慢可调大）")
    args = ap.parse_args()
    if args.keep and args.max:
        ap.error("--keep 与 --max 不能同时使用")

    from applications.zhihu_story.browser_adapter import get_browser

    browser = get_browser()
    page = browser.page
    page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)

    # 先滚动到底触发懒加载，摸清总量
    page.evaluate(SCROLL_JS)
    time.sleep(3)
    page.evaluate(SCROLL_JS)
    time.sleep(3)
    n = card_count(page)
    print(f"当前已渲染草稿卡：{n} 篇")

    if args.dry_run:
        print("dry-run：不执行删除")
        return

    if n == 0:
        print("草稿箱为空，无需删除")
        return

    target = (f"删到剩余 {args.keep} 篇" if args.keep
              else f"删除 {args.max} 篇" if args.max else "全部删除")
    print(f"目标：{target}（每篇间隔 {args.interval}s，Ctrl+C 可中断）")
    if not args.dry_run and n >= 10:
        r = input(f"确认执行？删除后无法恢复（y/N）：").strip().lower()
        if r != "y":
            print("已取消")
            return

    deleted = 0
    reload_count = 0
    try:
        while True:
            # 每删 5 篇滚动到底，触发懒加载补充新卡片
            if deleted and deleted % 5 == 0:
                page.evaluate(SCROLL_JS)
                time.sleep(2)

            cards = get_cards(page)

            # 触发刷新确认：全删模式列表空 / 保留模式卡片数 <= 目标。
            # 卡片少可能是懒加载没加载全，刷新页面重进后仍少才判定达标。
            need_reload = (cards == 0) or (args.keep and cards <= args.keep)
            if need_reload:
                reload_count += 1
                if reload_count > 3:
                    print(f"\n刷新 {reload_count} 次仍无足够卡片，停止"
                          f"（已删除 {deleted} 篇）")
                    break
                print(f"  当前 {cards} 张卡，刷新页面确认"
                      f"（第 {reload_count} 次）…")
                page.goto(DRAFT_URL, wait_until="domcontentloaded",
                          timeout=20000)
                time.sleep(4)
                page.evaluate(SCROLL_JS)
                time.sleep(2)
                cards = get_cards(page)
                if cards == 0:
                    print(f"\n草稿已全部删除（共 {deleted} 篇）")
                    break
                if args.keep and cards <= args.keep:
                    print(f"\n剩余 {cards} 篇，达到保留目标"
                          f"（已删除 {deleted} 篇）")
                    break

            if args.max and deleted >= args.max:
                print(f"\n已达 --max {args.max} 上限，停止"
                      f"（已删除 {deleted} 篇）")
                break

            # 读第一张卡标题（JS 内取，元素不跨 evaluate）
            title = page.evaluate("""() => {
              const card = document.querySelector('.CreationManage-CreationCard');
              if (!card) return '';
              const t = card.querySelector('.CreationCard-Title, [class*=Title]');
              return t ? (t.innerText || '').trim().slice(0, 40) : '';
            }""") or "（无标题）"
            if not delete_one(page):
                log.error("删除失败，中止")
                break
            deleted += 1
            print(f"  [{deleted}] 已删除：{title}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n用户中断，已删除 {deleted} 篇")

    print(f"完成：共删除 {deleted} 篇回答草稿")
    browser.close()


if __name__ == "__main__":
    main()
