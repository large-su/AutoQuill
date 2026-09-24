# -*- coding: utf-8 -*-
"""开篇体检：我方产物 vs 采集参考的开篇指标对比 + 总结体命中清单（零 LLM、零浏览器）。

用法：
    python tools/archive/probes/opening_stats.py                 # 全库
    python tools/archive/probes/opening_stats.py 202609          # 只统计文件名含该串的产物
    python tools/archive/probes/opening_stats.py 202609 --list   # 逐篇列出命中

口径（2026-09-19 用户反馈"打开第一页读不进去"时定的诊断口径）：
  只看引言（第一个章节标题之前）前 10 句，比较三件事——
    对话句比例     参考素材几乎篇篇有对话，我方常常一句都没有
    抽象评价句比例 "所有人都说…／没人知道…／只有我…" 这类下结论的句子
    具体名词密度    手机/照片/协议/碗…（每百字），读者能"看见"的锚点
  命中判定复用 core.detectors.check_summary_opening（与发布前质检同源）。
"""
import glob
import os
import re
import sys
import statistics

sys.path.insert(0, os.getcwd())
from core.detectors import check_summary_opening  # noqa: E402

DIALOG_RE = re.compile("[「『“]")
ABSTR = ["所有人", "没人", "从来", "终究", "到头来", "从始至终", "这场", "一场", "其实",
         "原来", "命运", "救赎", "遗憾", "心疼", "卑微", "偏爱", "深情", "温柔", "冷漠",
         "绝望", "全世界", "仿佛", "像是", "注定", "讽刺", "所谓", "而已", "再也"]
CONCRETE = ["手机", "照片", "抽屉", "协议", "戒指", "碗", "门", "床", "桌", "茶", "杯",
            "血", "刀", "雨", "车", "房", "钱", "纸", "信", "衣柜", "医院", "手术",
            "警察", "老板", "老公", "男友", "前男友", "丈夫", "妻子", "妈", "爸", "儿子",
            "女儿", "妹妹", "姐姐", "班主任", "同学", "校长", "出租屋", "婚礼", "订婚",
            "离婚", "老师", "医生", "护士", "饭", "酒", "烟", "猫", "狗", "井", "电梯",
            "公司", "办公室", "教室", "考场", "成绩", "工资", "病历", "检查", "合同"]
HEAD_RE = re.compile("^(?:#{1,6}[ ]*)?[*]{0,2}[0-9]{1,2}[*]{0,2}$")


def intro_of(text):
    lines = text.split(chr(10))
    for i, line in enumerate(lines):
        s = line.strip()
        if s and HEAD_RE.match(s):
            return chr(10).join(lines[:i])
    return chr(10).join(lines[:24])


def opening_metrics(text):
    flat = re.sub("[ " + chr(10) + chr(12288) + "]+", "", intro_of(text))
    sents = [s for s in re.split("[。！？!?…]+", flat) if s.strip()]
    head = sents[:10]
    joined = "".join(head)
    return {
        "对话句比": sum(1 for s in head if DIALOG_RE.search(s)) / max(1, len(head)),
        "抽象句比": sum(1 for s in head if any(w in s for w in ABSTR)) / max(1, len(head)),
        "具体名词": sum(joined.count(w) for w in CONCRETE) / max(1, len(joined)) * 100,
    }


def summarize(label, metrics_rows):
    med = {k: statistics.median([r[k] for r in metrics_rows]) for k in
           ("对话句比", "抽象句比", "具体名词")}
    print("%-14s %5d %12.2f %12.2f %14.2f"
          % (label, len(metrics_rows), med["对话句比"], med["抽象句比"],
             med["具体名词"]))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pattern = args[0] if args else ""
    show_list = "--list" in sys.argv

    print("%-14s %5s %12s %12s %14s"
          % ("数据源", "n", "对话句比", "抽象句比", "具体名词/百字"))
    print("=== 采集参考（data/collected_stories.jsonl）===")
    import json
    ref_path = os.path.join("data", "collected_stories.jsonl")
    refs = []
    if os.path.exists(ref_path):
        with open(ref_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    answer = json.loads(line).get("answer") or ""
                    if len(answer) > 500:
                        refs.append(answer)
    if refs:
        summarize("采集参考", [opening_metrics(t) for t in refs])
    else:
        print("（采集库缺失）")

    files = [f for f in sorted(glob.glob(os.path.join("output", "story_*.md")))
             if pattern in os.path.basename(f)]
    rows, hits = [], []
    for f in files:
        text = open(f, encoding="utf-8").read()
        if len(text) < 800:
            continue
        flat = [opening_metrics(text)]
        rows += flat
        r = check_summary_opening(text)
        if r["flagged"]:
            hits.append((os.path.basename(f), r))
    if rows:
        print()
        print("=== 我方产物（output/story_*.md" + (" 过滤：" + pattern if pattern else "") + "）===")
        summarize("我方产物", rows)
        print()
        print("总结体开头命中：%d/%d = %.0f%%" % (len(hits), len(rows),
                                              100.0 * len(hits) / len(rows)))
        if show_list:
            for name, r in hits:
                print("  %-34s %s" % (name, r["reason"]))
    else:
        print("（没有匹配的产物）")


if __name__ == "__main__":
    main()
