#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 味检测器（规则版，无需 LLM，纯本地统计）

检测中文故事文本中的"AI 机器味"信号：
  1. AI 万能连接词密度（然而/与此同时/总而言之/在这个X的时代…）
  2. AI 高频修饰语（仿佛/似乎/瞬间/终于/深深地/默默地…）
  3. "不是A而是B"式排比句式
  4. 超长句比例（>45 字）
  5. 连续两句句首重复率
  6. 平均句长

输出 0-100 的 AI 味指数：越高越像 AI 生成。
用途：①发布前自查终稿；②批量对比"真人文章 vs AI 生成"的指标差异。

用法：
  python tools/ai_flavor_check.py output/story_xxx.md
  python tools/ai_flavor_check.py output --sample 50   # 目录=批量 AI 生成稿
  python tools/ai_flavor_check.py --zhihu data/published_answers_2026-08-23.json --sample 50   # 真人对比
"""
import argparse
import glob
import json
import os
import random
import re
import statistics
import sys

CONNECTORS = [
    "然而", "因此", "与此同时", "总而言之", "不可否认", "综上所述",
    "由此可见", "在这个", "让我们", "不禁让人", "意味深长",
    "值得一提的是", "不难发现", "这不仅仅是",
]
# 中文 AI 高频句式（正则）
PATTERN_SCORES = [
    (re.compile(r"一旦[^。，！？]{0,14}就"), 5),         # 一旦…就
    (re.compile(r"只有[^。，！？]{0,12}才"), 5),         # 只有…才
    (re.compile(r"遮羞布|面具|画皮|烟幕弹|锦囊|伪装成"), 5),  # 揭露式比喻
    (re.compile(r"最[^。，！？]{0,12}的地方在于|真正[^。，！？]{0,8}的是"), 5),  # 极值判断
]
FILLERS = [
    "仿佛", "似乎", "瞬间", "终于", "深深地", "默默地", "缓缓地",
    "淡淡地", "轻轻", "愣住了", "颤抖着", "微微",
    "喃喃", "猛然", "倏地", "低声", "心口", "眼底",
]
LONG_SENT_CHARS = 45
MAX_WINDOW_CHARS = 6000  # 检查前 6000 字（开头 AI 味最密集）


def _clean(text):
    text = re.sub(r"^#{1,6}\s*.*$", "", text, flags=re.M)  # 去除标题/章节头
    text = re.sub(r"^[\s]*[-*_]{3,}[\s]*$", "", text, flags=re.M)  # 分隔线
    return text


def _sentences(text):
    return [s.strip() for s in re.split(r"[。！？!?；;\n]", text) if s.strip()]


def check_text(text):
    text = _clean(text or "")[:MAX_WINDOW_CHARS]
    sents = _sentences(text)
    if not sents:
        return None
    conn = sum(text.count(c) for c in CONNECTORS)
    fill = sum(text.count(f) for f in FILLERS)
    par = len(re.findall(r"(?:不仅仅|不只是|不是)[^。！？]{0,24}而是", text))
    pat = sum(1 for p, _ in PATTERN_SCORES if p.findall(text))
    long_n = sum(1 for s in sents if len(s) > LONG_SENT_CHARS)
    long_ratio = long_n / len(sents)
    starts = [s[:4] for s in sents if s]
    dup = sum(1 for i in range(1, len(starts)) if starts[i] == starts[i - 1])
    start_dup_ratio = dup / max(1, len(starts) - 1)
    avg_len = statistics.mean(len(s) for s in sents)

    score = 0
    score += min(24, conn * 6)
    score += min(30, fill * 3)   # 修饰语是最强判别信号（AI 每 6k 字 7-18 个，真人 0-2）
    score += min(24, par * 12)
    score += min(20, pat * 10)   # 关联句式/揭露比喻/极值判断
    if long_ratio > 0.25:
        score += 8
    if start_dup_ratio > 0.18:
        score += 10
    if avg_len > 38:
        score += 6
    total = min(100, score)

    metrics = {
        "连接词": conn, "修饰语": fill, "排比": par, "句式": pat,
        "长句比例": f"{long_ratio:.2f}",
        "句首重复": f"{start_dup_ratio:.2f}",
        "平均句长": round(avg_len, 1),
    }
    return metrics, total


def _verdict(score):
    if score < 25:
        return "低（像人手写）"
    if score < 45:
        return "中（有一定AI味）"
    return "高（AI味明显）"


def main():
    ap = argparse.ArgumentParser(description="AI 味检测器（规则版）")
    ap.add_argument("paths", nargs="*", help=".md/.txt 文件或目录")
    ap.add_argument("--zhihu", help="知乎快照 JSON（真人文章对比）")
    ap.add_argument("--sample", type=int, default=40, help="抽样条数")
    ap.add_argument("--full", action="store_true", help="逐条明细（默认只给摘要）")
    args = ap.parse_args()

    groups = {}  # group -> [(name, score)]

    def add(group, name, text):
        got = check_text(text)
        if got:
            groups.setdefault(group, []).append((name, *got))

    if args.zhihu:
        rows = json.load(open(args.zhihu, encoding="utf-8"))
        random.seed(7)
        for r in random.sample(rows, min(args.sample, len(rows))):
            add("真人(知乎高赞)", r.get("title", "?")[:24], r.get("content", ""))

    for p in args.paths:
        if os.path.isdir(p):
            files = sorted(glob.glob(os.path.join(p, "**", "*.md"), recursive=True))
            random.seed(7)
            for f in random.sample(files, min(args.sample, len(files))):
                add(os.path.basename(p), os.path.basename(f), open(f, encoding="utf-8", errors="ignore").read())
        elif os.path.isfile(p):
            add("单篇", os.path.basename(p), open(p, encoding="utf-8", errors="ignore").read())

    for group, items in groups.items():
        scores = [it[2] for it in items]
        avg = statistics.mean(scores)
        print("=" * 60)
        print(f"组：{group}  篇数={len(items)}  平均AI味={avg:.0f}/100（{_verdict(avg)}）")
        if args.full:
            for name, metrics, score in sorted(items, key=lambda x: -x[2])[:12]:
                print(f"  [{score:3d}] {name}  连接词={metrics['连接词']} 修饰语={metrics['修饰语']} "
                      f"排比={metrics['排比']} 长句={metrics['长句比例']} 句首重复={metrics['句首重复']} 均长={metrics['平均句长']}")
        else:
            lo, hi = min(scores), max(scores)
            print(f"  范围 {lo:.0f}-{hi:.0f}；顶部3篇：")
            for name, metrics, score in sorted(items, key=lambda x: -x[2])[:3]:
                print(f"    [{score:3d}] {name}")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
