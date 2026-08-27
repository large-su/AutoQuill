#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 味检测器 CLI——规则核心已收敛至 core/detectors.py。

本文件只是命令行外壳：常量与评分纯函数(check_ai_flavor/flavor_verdict)
均从 core.detectors 导入并保留旧别名(_sentences/_clean/check_text/_verdict)，
既有调用方(tests/webui/workflows)零改动。用法不变：
  python tools/ai_flavor_check.py output/story_xxx.md
  python tools/ai_flavor_check.py output --sample 50
  python tools/ai_flavor_check.py --zhihu data/published_answers_xxx.json --sample 50"""
import argparse
import glob
import json
import os
import random
import statistics
import sys

# 允许从任意工作目录独立运行：先把项目根挂上 sys.path 再 import core.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.detectors import (
    CONNECTORS, FILLERS, LONG_SENT_CHARS, MAX_WINDOW_CHARS, PATTERN_SCORES,
    _clean, _sentences, check_ai_flavor as check_text,
    flavor_verdict as _verdict,
)

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
