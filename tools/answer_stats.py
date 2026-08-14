# ============================================================
# tools/answer_stats.py — 统计知乎创作中心「回答管理」页的成绩
#
# 用法（Windows，项目根目录）：
#   PYTHONIOENCODING=utf-8 python tools/answer_stats.py
#   PYTHONIOENCODING=utf-8 python tools/answer_stats.py --sort views
#   PYTHONIOENCODING=utf-8 python tools/answer_stats.py --csv output/xx.csv
#
# 数据来源：回答管理页每条卡片（div.CreationManage-CreationCard）的
#   题目、发布时间、阅读/赞同/评论/收藏/喜欢。滚动懒加载直到取完。
# 统计字段 ≥1 万显示为「12.3 万」，解析时 ×10000。
# --sort 用页面自带排序按钮（time/views/likes/comments/edit）切换列表顺序；
#   排序切换后知乎懒加载失效（仅首屏），首屏即该排序的 TOP，并与全量比对。
# 输出：总量汇总 + Top 榜（阅读/点赞/评论）+ 卡片明细（可落盘 csv）。
#
# 说明：只读统计，不删除、不修改任何内容。
# ============================================================

import argparse
import csv
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = "https://www.zhihu.com/creator/manage/creation/answer"

# 提取每条卡片：题目 / 发布时间 / 各统计数字
CARD_JS = """() => {
  const cards = document.querySelectorAll('.CreationManage-CreationCard');
  const out = [];
  for (const card of cards) {
    const txt = (card.innerText || '');
    // 题目：卡片第一段（'编辑'前的内容，取第一行非空）
    const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
    const title = lines[0] || '';
    // 发布时间：'发布于'后一段
    const mTime = txt.match(/发布于\\s*([^\\n]+)/);
    const published = mTime ? mTime[1].trim() : '';
    // 统计：'阅读'前数字。知乎 ≥1 万显示为「12.3 万」（数字与万字间有空格），
    // 取最后一个匹配（统计区在卡片底部，正文里偶发的「数字 阅读」不会干扰）。
    const stat = (name) => {
      const re = new RegExp(
        '((?:\\\\d+(?:\\\\.\\\\d+)?\\\\s*万|\\\\d+(?:\\\\.\\\\d+)?))\\\\s*' + name, 'g');
      const all = [...txt.matchAll(re)];
      if (!all.length) return 0;
      const v = all[all.length - 1][1];
      return v.includes('万') ? Math.round(parseFloat(v) * 10000) : parseInt(v, 10);
    };
    out.push({
      title: title,
      published: published,
      reads: stat('阅读'),
      likes: stat('赞同'),
      comments: stat('评论'),
      favorites: stat('收藏'),
      hearts: stat('喜欢'),
    });
  }
  return out;
}"""


SORT_LABELS = {"time": "发布时间", "views": "浏览量", "likes": "赞同数",
               "comments": "评论数", "edit": "编辑时间"}
# 各排序对应的统计字段（用于与全量数据交叉验证）
SORT_KEY = {"views": "reads", "likes": "likes", "comments": "comments",
            "time": "reads", "edit": "reads"}


def click_sort(page, sort_key):
    """点击页面自带的排序按钮展开菜单并选择目标排序。返回是否成功。"""
    menu = page.evaluate("""() => {
      const btns = Array.from(document.querySelectorAll('button'));
      for (const b of btns) {
        const t = (b.innerText || '').replace(/\\u200b/g, '').trim();
        if (t === '按发布时间排序') { b.click(); return true; }
      }
      return false;
    }""")
    if not menu:
        return False
    time.sleep(2)
    label = "按" + SORT_LABELS[sort_key] + "排序"
    picked = page.evaluate("""(label) => {
      const sels = ['[role=menuitem]', '[role=option]',
                    '[class*=Popover] [class*=Option]',
                    '[class*=Popover] li', '[class*=Popover] [class*=item]',
                    '[class*=Popover] div', '[class*=Popover] span'];
      for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
          const t = (el.innerText || '').replace(/\\u200b/g, '').trim();
          if (t === label) { el.click(); return true; }
        }
      }
      return false;
    }""", label)
    if not picked:
        print(f"  未找到排序选项「{label}」")
        return False
    print(f"  已切换排序：{label}")
    time.sleep(2)
    return True


def load_all(page):
    """滚动加载直到卡片数不再增长，返回所有卡片数据。"""
    last = 0
    stuck = 0
    while stuck < 5:
        cards = page.evaluate(CARD_JS)
        n = len(cards)
        if n == last:
            stuck += 1
        else:
            stuck = 0
            last = n
            print(f"  已加载 {n} 篇…", end="\r")
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)
    print(f"  加载完成：共 {last} 篇")
    return page.evaluate(CARD_JS)


def main():
    ap = argparse.ArgumentParser(description="统计知乎回答管理页成绩")
    ap.add_argument("--csv", default="", help="明细落盘路径（可选）")
    ap.add_argument("--top", type=int, default=10, help="Top 榜条数")
    ap.add_argument("--sort", default="",
                    choices=["time", "views", "likes", "comments", "edit"],
                    help="用页面自带排序按钮切换列表顺序（默认不切换）")
    args = ap.parse_args()

    from applications.zhihu_story.browser_adapter import get_browser

    browser = get_browser()
    page = browser.page
    page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)

    print("加载回答列表…")
    items = load_all(page)

    if args.sort:
        # 先加载全量再切排序：排序切换后知乎懒加载失效（只渲染首屏）
        click_sort(page, args.sort)
        time.sleep(4)
        screen = page.evaluate(CARD_JS)
        key = SORT_KEY[args.sort]
        print(f"\n【页面排序首屏（按{SORT_LABELS[args.sort]}）】")
        for i, it in enumerate(screen, 1):
            print(f"  {i:>2}. [{it['reads']:>7}] {it['title'][:40]}"
                  f"（{it['published']}）")
        ranked = sorted(items, key=lambda x: x[key], reverse=True)[:len(screen)]
        same = sum(1 for a, b in zip(ranked, screen) if a["title"] == b["title"])
        print(f"  与全量数据排序比对：{same}/{len(screen)} 条一致"
              f"（{args.sort} 排序）")
    browser.close()

    if not items:
        print("未获取到数据")
        return

    # ---- 汇总 ----
    total = len(items)
    sums = {}
    for k in ("reads", "likes", "comments", "favorites", "hearts"):
        sums[k] = sum(i[k] for i in items)
    n_with_reads = sum(1 for i in items if i["reads"] > 0)
    n_with_likes = sum(1 for i in items if i["likes"] > 0)

    print(f"\n{'='*46}")
    print(f"回答总数：{total} 篇")
    print(f"累计阅读：{sums['reads']:,}")
    print(f"累计赞同：{sums['likes']:,}")
    print(f"累计评论：{sums['comments']:,}")
    print(f"累计收藏：{sums['favorites']:,}")
    print(f"累计喜欢：{sums['hearts']:,}")
    if total:
        print(f"平均阅读：{sums['reads']/total:,.0f} / 篇")
        print(f"有阅读记录：{n_with_reads} 篇（{n_with_reads/total*100:.0f}%）")
        print(f"有赞同记录：{n_with_likes} 篇（{n_with_likes/total*100:.0f}%）")
    print(f"{'='*46}")

    # ---- Top 榜 ----
    def top(key, label):
        print(f"\n【{label} TOP {min(args.top, total)}】")
        ranked = sorted(items, key=lambda x: x[key], reverse=True)
        for i, it in enumerate(ranked[:args.top], 1):
            if it[key] == 0 and i > 5:
                break
            print(f"  {i:>2}. [{it[key]:>6}] {it['title'][:40]}"
                  f"（{it['published']}）")

    top("reads", "阅读量")
    top("likes", "赞同数")

    # ---- 明细落盘 ----
    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=["title", "published", "reads", "likes",
                               "comments", "favorites", "hearts"])
            w.writeheader()
            w.writerows(items)
        print(f"\n明细已保存：{args.csv}")


if __name__ == "__main__":
    main()
