"""快照数据层公共工具（已发布内容看板 / 草稿箱素材共用）。

统一：快照文件发现、读取、坏文件回退、元信息组装。
各业务模块保留：行归一化（coerce_row）、质量判定（quality_of）、筛选与统计。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)


def latest_file(data_dir: Path, pattern: str) -> Path | None:
    """返回匹配 pattern 的最新文件（按修改时间），无则 None。"""
    if not data_dir.exists():
        return None
    files = sorted(
        data_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def load_snapshot(data_dir: Path, pattern: str,
                  coerce_row: Callable[[dict], dict | None],
                  id_keys: Iterable[str],
                  quality_of: Callable[[list], int] | None = None,
                  minimal_ratio: float = 0.05) -> dict:
    """读取看板/草稿箱快照，带「坏数据回退」：

    - 按修改时间从新到旧遍历；
    - 行通过 coerce_row 归一，主键缺失的行丢弃（id_keys 任中其一存在即可）；
    - 优先返回最新且质量达标（>= 全部快照最大质量 * minimal_ratio）的文件；
      全部异常（读取失败/为空/质量为 0）时回退到最近一份有内容的文件。

    coerce_row: 单行 → 归一化 dict；quality_of: rows → 质量分（默认行数）。
    返回 {rows, total, generated_at, source_file}。
    """
    quality_of = quality_of or len
    if not data_dir.exists():
        return {"rows": [], "total": 0, "generated_at": None, "source_file": None}

    files = sorted(
        data_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime, reverse=True)
    files = [p for p in files if p.stat().st_size > 0]

    loaded = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            log.warning("快照读取失败 %s：%s", path.name, exc)
            continue
        rows = []
        for r in raw:
            if not isinstance(r, dict) or not any(r.get(k) for k in id_keys):
                continue
            try:
                row = coerce_row(r)
            except Exception:
                continue
            if row:
                rows.append(row)
        loaded.append((path, rows))
    if not loaded:
        return {"rows": [], "total": 0, "generated_at": None, "source_file": None}

    best = loaded[0]
    if len(loaded) > 1:
        scores = [quality_of(rows) for _, rows in loaded]
        threshold = max(scores) * minimal_ratio
        for path, rows in loaded:
            if quality_of(rows) >= threshold:
                best = (path, rows)
                break

    path, rows = best
    return {
        "rows": rows,
        "total": len(rows),
        "generated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        "source_file": str(path),
    }
