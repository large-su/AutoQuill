# -*- coding: utf-8 -*-
"""回填历史快照到反馈闭环表现台账，并打印题材先验摘要。

用法：
    python tools/seed_feedback.py            # 回填 + 摘要（幂等，可重复执行）
    python tools/seed_feedback.py --verbose  # 逐文件显示回填数

数据去向：data/state/story_performance.jsonl（每条=一篇的一次观测）。
"""
import argparse
import io
import os
import sys

# 工具脚本独立运行：把仓库根加入 sys.path（与 tools/auto_test.py 一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true",
                        help="逐文件打印回填条数")
    args = parser.parse_args()

    from core import feedback_loop

    print("== 回填历史快照 ==")
    n = feedback_loop.seed_from_snapshots(verbose=args.verbose)
    print(f"入账观测 {n} 条 → data/state/story_performance.jsonl")

    print("\n== 题材先验（发布后日均互动分，含 90 天衰减）==")
    s = feedback_loop.summarize(auto_seed=False)
    if not s["n_articles"]:
        print("（暂无观测）")
        return 0
    overall = s["overall"]["score"]
    print(f"文章数 {s['n_articles']}  全局中位分 {overall:.3f}")
    for g, info in sorted(s["genres"].items(),
                          key=lambda kv: kv[1]["score"], reverse=True):
        print(f"  {g:<8} n={info['n']:<3} 分={info['score']:.3f} "
              f"赞/天={info['likes_per_day']:.2f} "
              f"评/天={info['comments_per_day']:.2f} "
              f"藏/天={info['collects_per_day']:.2f} "
              f"选题乘数≈{max(0.5, min(2.0, 1 + 0.5 * (info['boost_1x'] - 1))):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
