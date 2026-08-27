# ============================================================
# tools/generate_with_author.py — 作者风格驱动的故事生成工具
#
# 分模块链路第三步：注入已提炼的作者技能签名生成故事。
#
# 用法：
#   python tools/generate_with_author.py --question "知乎问题" \
#       --author 镜中花 [--genre 甜宠文] [--out data/stories/xxx.md]
#
# 前置条件：
#   1. 已采集该作者故事（tools/collect_author_pw.py）
#   2. 已提炼技能签名（python -m applications.zhihu_story.author_profiler 镜中花）
#
# 架构位置：Layer 5 (Applications) — 生成链路独立入口
# ============================================================

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)


def main():
    parser = argparse.ArgumentParser(description="作者风格驱动生成（注入技能签名）")
    parser.add_argument("--question", required=True, help="知乎问题标题")
    parser.add_argument("--author", required=True, help="作者名（须已提炼技能签名）")
    parser.add_argument("--genre", default="现代言情",
                        help="题材（对应配方 genre，默认现代言情）")
    parser.add_argument("--out", default="",
                        help="输出文件（默认不写盘，仅打印）")
    args = parser.parse_args()

    from applications.zhihu_story.author_profiler import load_author_profile
    profile = load_author_profile(args.author)
    if not profile:
        print(f"  ❌ 未找到「{args.author}」的技能签名。"
              f"请先运行：python -m applications.zhihu_story.author_profiler {args.author}")
        sys.exit(1)

    # 用作者签名中的基调解出配方风格（缺省时以作者技能为准）
    sig = profile.get("signature", {})
    recipe = {
        "genre": args.genre,
        "hook": " / ".join(sig.get("opening_patterns", ["高能梗概开场"]))[:200],
        "style": sig.get("style", "")[:200],
        "perspective": "第一人称",
        "tone": sig.get("tone", ""),
        "conflict": "",
        "pacing": "",
        "character": "",
    }

    print(f"  生成问题：{args.question}")
    print(f"  作者：{args.author}（技能签名已加载，{profile['profiled_at']}）")
    print()

    from story_generation import generate_story
    story = generate_story(args.question, recipe=recipe, author=args.author)

    print()
    if not story:
        print("  ❌ 生成失败")
        sys.exit(1)

    print(f"  ✓ 生成完成：{len(story)} 字符")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(story)
        print(f"  ✓ 已保存 → {args.out}")
    else:
        print()
        print("  ── 生成结果 ──")
        print(story)


if __name__ == "__main__":
    main()
