# -*- coding: utf-8 -*-
"""自动化模块的持久化：计划 / 当日排班 / 台账。

三条铁律：
  1. **宽容读**：文件缺失/损坏一律回退默认，绝不让用户改坏一个 JSON 就起不来；
  2. **原子写**：先写 .tmp 再 os.replace，避免断电/中途退出留下半截文件；
  3. **当日排班必须落盘**：重启后「今天的时间轴」要保持稳定（不能每次重排，
     否则用户看到的排班一直变，也无法解释「为什么现在跑」）。
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from automation.model import normalize_plan

log = logging.getLogger(__name__)


def _state_dir() -> Path:
    """数据目录：源码态=项目根/data，安装版=%APPDATA%/AutoQuill/data。"""
    from core import paths
    d = Path(paths.data("data", "state", "automation"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def plan_file() -> Path:
    return _state_dir() / "plan.json"


def ledger_file() -> Path:
    return _state_dir() / "ledger.jsonl"


def day_file(day: str) -> Path:
    return _state_dir() / ("day_%s.json" % day)


def _atomic_write(path: Path, text: str, attempts: int = 3):
    """原子写：先写临时文件再替换；Windows 上偶发被句柄/杀软占用 → 退避重试。

    ★ 临时文件必须用**进程唯一名**（2026-09-29 全量测试抓到）：
      固定名 `.tmp` 在「同一进程内两次写同一文件」或「两个进程同时写」时会撞锁，
      直接 `PermissionError` 把当日排班写丢。这是同一类问题的第三处
      （另两处在 core/progress.py 与 core/update_stage.py，已一并统一）。

    最终仍失败只告警不抛（当日排班/计划丢了可以重建，不该让自动化崩掉）。
    """
    import time as _time
    for attempt in range(max(1, attempts)):
        tmp = path.with_suffix("%s.%d.%d.tmp" % (path.suffix, os.getpid(), attempt))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if attempt + 1 >= max(1, attempts):
                log.warning("写入失败（%s）：%s", path.name, exc)
                return
            _time.sleep(0.2 * (2 ** attempt))


def load_plan() -> dict:
    """读计划（归一化后返回）。"""
    path = plan_file()
    raw = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:      # noqa: BLE001
            log.warning("自动化计划读取失败，已回退默认：%s", exc)
            raw = {}
    return normalize_plan(raw)


def save_plan(plan: dict) -> dict:
    """写计划（先归一化再落盘，返回值即落盘内容）。"""
    plan = normalize_plan(plan)
    _atomic_write(plan_file(), json.dumps(plan, ensure_ascii=False, indent=2))
    return plan


def load_day(day: str) -> dict:
    """读当日排班（不存在返回空骨架）。"""
    path = day_file(day)
    if not path.exists():
        return {"date": day, "plan_hash": "", "schedule": [], "counters": {},
                "rescheduled": 0, "notes": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:          # noqa: BLE001
        log.warning("当日排班读取失败，按空骨架处理：%s", exc)
        return {"date": day, "plan_hash": "", "schedule": [], "counters": {},
                "rescheduled": 0, "notes": ["读取失败：" + str(exc)]}
    data.setdefault("schedule", [])
    data.setdefault("counters", {})
    data.setdefault("notes", [])
    return data


def save_day(day: str, data: dict):
    _atomic_write(day_file(day), json.dumps(data, ensure_ascii=False, indent=2))


def append_ledger(entry: dict):
    """台账追加一行（作业生命周期：计划/开始/结束/跳过/需要人工）。"""
    try:
        with open(ledger_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:          # noqa: BLE001
        log.warning("自动化台账写入失败（不影响执行）：%s", exc)



# 评论回复的运行统计不放这里：那份数据属于「评论回复」这个业务，
# 由 core.checkin 自己管（automation 只编排，不反向提供业务存储）。


def load_ledger(day: str = "", limit: int = 0) -> list:
    """读台账；给了 day 只返回该日期的记录。limit>0 时只取最后 N 条。"""
    path = ledger_file()
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:      # noqa: BLE001
                    continue
                if day and str(row.get("day", "")) != day:
                    continue
                rows.append(row)
    except Exception as exc:          # noqa: BLE001
        log.warning("自动化台账读取失败：%s", exc)
        return []
    return rows[-limit:] if limit and limit > 0 else rows


def done_counts(day: str) -> dict:
    """当天各任务类型「已完成数量」——重启后续跑的依据（用户明确要求）。

    口径（2026-09-28 修订）：
      - **默认按台账累加**：台账是我们下达过什么指令、结果如何的审计记录；
      - **线上可核对的任务类型以线上为准**（publish_drafts）：
        台账里的「失败」并不意味着没发出去——误报会把它漏掉，于是规划器
        以为还差几篇、重复排班。计数合并只在 core.progress.merge_counts 一处，
        本函数不自己判断谁大谁小。

    ★ 这是「已完成数量」的**唯一出口**：planner / scheduler 只调它，
      不直接读台账、更不直接读页面（避免错误耦合）。
    """
    counts = {}
    for row in load_ledger(day):
        if row.get("status") != "done":
            continue
        t = row.get("type")
        n = int(row.get("units") or 1)
        counts[t] = counts.get(t, 0) + n
    # 线上校核：只对「线上有等价物」的任务类型生效；读不到时原样返回台账数字
    try:
        from core import progress as _progress
        snap = _progress.load(day)
        if snap is not None:
            for t in list(counts) + list(_progress.SITE_COUNTED_TYPES):
                merged = _progress.merge_counts(t, counts.get(t, 0), snap, day=day)
                if merged:
                    counts[t] = merged
    except Exception as exc:              # noqa: BLE001 校核失败绝不能影响排班
        log.warning("进度校核参与计数失败（沿用台账数字）：%s", exc)
    return counts


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
