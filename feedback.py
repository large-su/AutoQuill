#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoQuill 意见反馈 CLI —— 随手记录使用中遇到的问题。

用法：
  python feedback.py 遇到的问题描述...               追加一条反馈
  python feedback.py -c 选题 "选题老是选到非故事题"
  python feedback.py --context "问题链接 q/123" 描述...
  python feedback.py --list [N]                     查看最近 N 条（默认 20）
  python feedback.py --cat                          列出可选分类
  python feedback.py                                进入多行输入（Ctrl+Z 后回车结束）

反馈写入 core.paths.data("feedback.md")（源码态=项目根 feedback.md，
安装态=%APPDATA%\\AutoQuill\\feedback.md）。后续迭代请翻阅该文件。
"""
import sys

# Windows GBK 控制台：打印 emoji/特殊符号会崩（main.py banner 同款坑）。
# 保持当前编码，仅把无法编码的字符替换为 ?，而非抛 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from core.user_feedback import CATEGORIES, read, record


def _print_entry(e):
    print(f"  [{e['time']}] 【{e['category']}】")
    if e.get("context"):
        print(f"    上下文：{e['context']}")
    for ln in e["text"].split("\n"):
        print(f"    {ln}")
    print()


def main():
    argv = sys.argv[1:]

    if argv and argv[0] == "--list":
        n = 20
        if len(argv) > 1 and argv[1].isdigit():
            n = int(argv[1])
        entries = read(limit=n)
        if not entries:
            print("（暂无反馈记录）")
            return
        print(f"最近 {len(entries)} 条反馈：\n")
        for e in entries:
            _print_entry(e)
        return

    if argv and argv[0] == "--cat":
        print("可选分类：", "、".join(CATEGORIES))
        return

    # 解析选项 + 文本
    category = None
    context = None
    text_parts = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--category") and i + 1 < len(argv):
            category = argv[i + 1]
            i += 2
        elif a == "--context" and i + 1 < len(argv):
            context = argv[i + 1]
            i += 2
        else:
            text_parts.append(a)
            i += 1
    text = " ".join(text_parts).strip()

    if not text:
        print("请输入反馈内容（多行，输入完按 Ctrl+Z 后回车结束）：")
        text = sys.stdin.read().strip()
    if not text:
        print("未输入内容，已取消。")
        return

    entry = record(text, category=category, context=context)
    if entry:
        print("[OK] 已记录反馈：")
        print(entry)
    else:
        print("[X] 记录失败")


if __name__ == "__main__":
    main()
