#!/usr/bin/env python3
"""统计项目中 Python 代码的行数/字符数，快速评估代码体量。"""

import os
from pathlib import Path


def count_py_files(root: str) -> dict:
    """递归统计目录下所有 .py 文件的行数、有效代码行、字符数。"""
    results = {}
    base = Path(root)

    for py_file in sorted(base.rglob("*.py")):
        # 跳过自身和虚拟环境
        if py_file.name == "count_code.py" or "__pycache__" in py_file.parts:
            continue

        with open(py_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total_lines = len(lines)
        total_chars = sum(len(line) for line in lines)

        # 有效代码行：排除纯空白行和纯注释行（以 # 开头）
        code_lines = 0
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                code_lines += 1

        rel_path = str(py_file.relative_to(base))
        results[rel_path] = {
            "total_lines": total_lines,
            "code_lines": code_lines,
            "total_chars": total_chars,
        }

    return results


def main():
    root = Path(__file__).parent
    stats = count_py_files(root)

    # ===== 打印单文件明细 =====
    print(f"{'文件':<55} {'总行数':>8} {'有效代码行':>10} {'字符数':>10}")
    print("-" * 85)

    sum_total = sum_code = sum_chars = 0
    for filename, v in stats.items():
        print(
            f"{filename:<55} {v['total_lines']:>8} {v['code_lines']:>10} {v['total_chars']:>10}"
        )
        sum_total += v["total_lines"]
        sum_code += v["code_lines"]
        sum_chars += v["total_chars"]

    # ===== 汇总 =====
    print("-" * 85)
    print(f"{'【合计】':<55} {sum_total:>8} {sum_code:>10} {sum_chars:>10}")
    print()
    print(
        f"共 {len(stats)} 个 .py 文件 | "
        f"总行数: {sum_total} | "
        f"有效代码行: {sum_code} (占比 {sum_code/sum_total*100:.1f}%) | "
        f"总字符数: {sum_chars}"
    )


if __name__ == "__main__":
    main()
