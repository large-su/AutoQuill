"""快照数据层公共工具（已发布内容看板 / 草稿箱素材共用）。

统一：快照文件发现、读取、坏文件回退、元信息组装。
各业务模块保留：行归一化（coerce_row）、质量判定（quality_of）、筛选与统计。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
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


def prune_rows(data_dir: Path, pattern: str, key: str, values) -> int:
    """从最新快照里剔除主键命中 values 的行（本地即时同步用，2026-09-19）。

    场景：在知乎上删掉已发布回答/草稿后，本地快照必须同步剔除，用户不必
    再整页重抓一次。返回剔除行数；快照缺失/读取或写回失败返回 0（只记日志，
    由调用方决定是否兜底重抓）。"""
    path = latest_file(data_dir, pattern)
    if not path:
        return 0
    want = {str(v) for v in values if v not in (None, "")}
    if not want:
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as exc:      # noqa: BLE001
        log.warning("快照读取失败，无法本地剔除：%s", exc)
        return 0
    if not isinstance(rows, list):
        return 0
    keep = [r for r in rows
            if not (isinstance(r, dict) and str(r.get(key)) in want)]
    removed = len(rows) - len(keep)
    if removed <= 0:
        return 0
    tmp = None
    try:
        # 写入失败时保留原快照；直接截断原文件会使下一次读取丢失所有行。
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, suffix=".tmp",
                                         delete=False) as f:
            tmp = Path(f.name)
            json.dump(keep, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:      # noqa: BLE001
        log.warning("快照写回失败（本地剔除未生效）：%s", exc)
        return 0
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    log.info("快照本地剔除 %d 行（%s，主键 %s）", removed, path.name, key)
    return removed


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
        if not isinstance(raw, list):
            log.warning("快照格式无效 %s：根节点必须是数组", path.name)
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
        try:
            score = quality_of(rows)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("快照字段无效 %s：%s", path.name, exc)
            continue
        loaded.append((path, rows, score))
    if not loaded:
        return {"rows": [], "total": 0, "generated_at": None, "source_file": None}

    best = loaded[0]
    if len(loaded) > 1:
        scores = [score for _, _, score in loaded]
        threshold = max(scores) * minimal_ratio
        for path, rows, score in loaded:
            if score >= threshold:
                best = (path, rows, score)
                break

    path, rows, _ = best
    return {
        "rows": rows,
        "total": len(rows),
        "generated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        "source_file": str(path),
    }
