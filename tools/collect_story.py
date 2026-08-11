# ============================================================
# tools/collect_story.py — 单篇故事采集验证工具
#
# 从当前屏幕上已打开的知乎回答页，通过 UIA 通道提取
# 首条回答的 (title, answer, footer)，追加写入 JSONL 文件。
#
# 用法：
#   1. Edge 打开知乎问题/回答页（已登录，页面可见）
#   2. 运行：python tools/collect_story.py [--out 输出文件]
#
# 架构位置：Layer 3 采集通道的独立入口，复用现有提取接缝
# （applications/zhihu_story/extractors.py），后续作者页
# 多故事采集将基于同一接缝扩展。
# ============================================================

import argparse
import json
import sys
import time
import os

# 项目根加入 sys.path（本脚本位于 tools/ 下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    parser = argparse.ArgumentParser(description="单篇故事采集（UIA 通道）")
    parser.add_argument("--out", default=os.path.join("data", "collected_stories.jsonl"),
                        help="输出 JSONL 文件（默认 data/collected_stories.jsonl）")
    parser.add_argument("--min-length", type=int, default=200,
                        help="答案正文最小长度（默认 200）")
    parser.add_argument("--wait-timeout", type=float, default=10.0,
                        help="UIA 等待首答出现的超时（默认 10s）")
    args = parser.parse_args()

    from applications.zhihu_story.extractors import UiaAnswerExtractor

    print("  采集准备：请确认 Edge 已打开知乎回答页（页面可见）")

    extractor = UiaAnswerExtractor(
        min_length=args.min_length,
        wait_timeout=args.wait_timeout,
        poll_interval=0.5,
    )
    print("  正在读取页面（UIA 无障碍树）...")
    title, answer, footer = extractor.extract()

    if not (title and answer):
        print("  ❌ 采集失败：未读到有效内容（页面可能不在知乎回答页，或首答过短）")
        sys.exit(1)

    record = {
        "source": "uia",
        "title": title,
        "answer": answer,
        "footer": footer,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    likes = footer.get("likes") if footer else None
    print(f"  ✓ 采集成功：{title[:40]}")
    print(f"    正文 {len(answer)} 字，赞同={likes}")
    print(f"    已追加写入 {args.out}")

if __name__ == "__main__":
    main()
