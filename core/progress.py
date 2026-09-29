# ============================================================
# core/progress.py — 进度事实（线上校核的唯一计数真源）
#
# 背景（2026-09-28 真机事故）：配额原本用台账里 status==done 的 units 累加，
# 而台账是「我们的自述」——发布误报失败时，已发布的内容就从计数里漏掉了，
# 规划器以为还差几篇，于是重复排班、排到时间轴外。
#
# 本模块只做一件事：把**线上读到的真实状态**变成一个不可变快照，并作为
# 计数的唯一出口。它不认识浏览器、不认识 automation，是纯逻辑 + 落盘。
#
# 依赖方向（防错误耦合）：
#     automation/store.py ──读──┐
#                               ├──→ core/progress.py（只依赖 core.paths）
#     webui/site_progress.py ──写┘
#     反向依赖一律禁止：core 绝不 import webui / automation。
#
# 数据分层：
#     事实层  data/state/progress.json      计数只用它
#     动作层  automation/ledger.jsonl       审计「下过什么指令、结果如何」
#     差异层  data/state/reconcile.jsonl    校核发现的偏差（不改写动作层）
# ============================================================

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core import paths

log = logging.getLogger(__name__)

# 线上「已发布回答」接口（创作中心实际使用的正式接口，已真机确认）
PUBLISHED_API = "/api/v4/creators/creations/v2/answer"
# 草稿数接口（比滚动草稿箱便宜得多）
DRAFT_COUNT_API = "/api/v4/answer-drafts/count"


def _state_dir() -> Path:
    d = Path(paths.data("data", "state"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def progress_file(day: str = "") -> Path:
    """快照落盘位置：一天一个文件，天然按天隔离。"""
    name = "progress.json" if not day else "progress_%s.json" % day
    return _state_dir() / name


def reconcile_file() -> Path:
    return _state_dir() / "reconcile.jsonl"


def _write_json_atomic(path: Path, payload, attempts=3) -> bool:
    """原子写（先 .tmp 再 replace）：断电/中途退出不留半截文件。

    ★ 临时文件用**进程唯一名**并在被占用时退避重试：固定名 .tmp 在两个进程
      同时写时会撞锁（PermissionError），状态就丢了——真实场景里
      「主程序校核」与「子进程换装」确实会并发（2026-09-29 真机踩到）。
    """
    for attempt in range(max(1, int(attempts))):
        tmp = path.with_suffix("%s.%d.%d.tmp" % (path.suffix, os.getpid(), attempt))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception as exc:            # noqa: BLE001
            try:
                tmp.unlink()
            except OSError:
                pass
            if attempt + 1 >= max(1, int(attempts)):
                log.warning("进度快照写入失败 %s：%s", path.name, exc)
                return False
            time.sleep(0.2 * (2 ** attempt))
    return False


# ------------------------------------------------------------
# 解析：线上原始 JSON → 归一化结构（纯函数，可单测）
# ------------------------------------------------------------

def _row_inner(row):
    """接口返回的条目是**两层**结构：外层带 type/status，真实字段在内层 data。

    真机确认（2026-09-28）：
        {"type": "answer", "status": [], "data": {"id":..., "created_time":...,
         "question_id":..., "title":...}}
    兼容内层缺失的情况（直接把外层当数据用）。
    """
    if not isinstance(row, dict):
        return {}
    inner = row.get("data")
    return inner if isinstance(inner, dict) else row


def parse_published(payload):
    """已发布回答接口 JSON → [(aid, created_time, qid, title)]。

    只挑需要的字段：计数与去重。任何异常输入都返回空列表，绝不抛。
    """
    out = []
    if not isinstance(payload, dict):
        return out
    rows = payload.get("data")
    if not isinstance(rows, list):
        return out
    for row in rows:
        inner = _row_inner(row)
        aid = str(inner.get("id") or "")
        try:
            created = int(inner.get("created_time") or 0)
        except (TypeError, ValueError):
            created = 0
        if not aid and not created:
            continue
        out.append({
            "aid": aid,
            "created_time": created,
            "qid": str(inner.get("question_id") or ""),
            "title": str(inner.get("title") or "")[:80],
        })
    return out


def published_total(payload, default=None):
    """接口的 paging.totals —— 线上自己说的「这个区间发了多少篇」。"""
    if not isinstance(payload, dict):
        return default
    paging = payload.get("paging")
    if not isinstance(paging, dict):
        return default
    try:
        return int(paging.get("totals"))
    except (TypeError, ValueError):
        return default


def parse_draft_count(payload):
    """草稿数接口 JSON → int | None。"""
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("count"))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------
# 快照
# ------------------------------------------------------------

@dataclass(frozen=True)
class ProgressSnapshot:
    """某一时刻的线上事实。不可变：读到的就是读到的，不被后续逻辑改写。"""

    day: str
    published_today: int
    drafts_pending: int
    at: str
    published: tuple = ()          # 今日已发布明细（aid/created_time/qid/title）
    source: str = "site"           # site = 线上读到；ledger = 退回本地
    extra: dict = field(default_factory=dict)   # 预留：赞/评论/收藏/关注

    def to_dict(self):
        return {
            "day": self.day,
            "published_today": int(self.published_today),
            "drafts_pending": int(self.drafts_pending),
            "at": self.at,
            "published": list(self.published),
            "source": self.source,
            "extra": dict(self.extra),
        }

    def valid_for(self, day):
        """只对**同一天**有效：跨天自动失效，不会拿昨天的数算今天。"""
        return bool(day) and str(self.day) == str(day)

    def age_seconds(self, now=None):
        try:
            then = datetime.fromisoformat(self.at)
        except (TypeError, ValueError):
            return None
        return ((now or datetime.now()) - then).total_seconds()


def build_snapshot(*, day, published_payload=None, draft_payload=None,
                   at=None, source="site", extra=None):
    """把两个接口的原始返回组装成快照（纯函数）。

    某一侧读不到时按 0 记，但 source 仍为 site —— 调用方（site_progress）
    负责在**两侧都读不到**时干脆不落盘，避免用 0 覆盖真实数字。
    """
    items = parse_published(published_payload)
    total = published_total(published_payload)
    if total is None:
        total = len(items)
    drafts = parse_draft_count(draft_payload)
    return ProgressSnapshot(
        day=str(day),
        published_today=int(total or 0),
        drafts_pending=int(drafts if drafts is not None else 0),
        at=at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        published=tuple(items),
        source=source,
        extra=dict(extra or {}),
    )


def save(snapshot) -> bool:
    if snapshot is None:
        return False
    return _write_json_atomic(progress_file(snapshot.day), snapshot.to_dict())


def load(day) -> "ProgressSnapshot | None":
    """读当天快照；缺失/损坏/跨天一律返回 None（调用方退回本地计数）。"""
    if not day:
        return None
    path = progress_file(day)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:                # noqa: BLE001
        log.warning("进度快照读取失败 %s：%s", path.name, exc)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        snap = ProgressSnapshot(
            day=str(raw.get("day") or ""),
            published_today=int(raw.get("published_today") or 0),
            drafts_pending=int(raw.get("drafts_pending") or 0),
            at=str(raw.get("at") or ""),
            published=tuple(raw.get("published") or ()),
            source=str(raw.get("source") or "site"),
            extra=dict(raw.get("extra") or {}),
        )
    except (TypeError, ValueError):
        return None
    return snap if snap.valid_for(day) else None


# ------------------------------------------------------------
# 计数合并（唯一出口）：台账只作审计，线上才是事实
# ------------------------------------------------------------

# 明确用「线上事实」计数的任务类型。其余类型（如 full_chain 写的草稿）
# 线上没有等价物，继续用台账。
SITE_COUNTED_TYPES = ("publish_drafts",)


def merge_counts(task_type, ledger_units, snapshot, day=""):
    """台账 units 与线上快照 → 最终计数。

    取 max 的理由：
      · 线上 > 台账：误报的失败被补回来（今天的真实事故）；
      · 线上 < 台账：线上接口滞后/分页没读全时，不至于把已完成的算没；
      · 没有快照：原样返回台账数字，绝不猜。
    """
    ledger_units = int(ledger_units or 0)
    if task_type not in SITE_COUNTED_TYPES or snapshot is None:
        return ledger_units
    if day and not snapshot.valid_for(day):
        return ledger_units
    return max(ledger_units, int(snapshot.published_today or 0))


# ------------------------------------------------------------
# 差异层：记下「原本会算错的地方」，不修改台账
# ------------------------------------------------------------

def log_reconcile(*, day, ledger_units, snapshot, note="", extra=None):
    """把一次「台账 vs 线上」的偏差追加到 reconcile.jsonl（同日同因只记一次）。"""
    if snapshot is None:
        return False
    row = {
        "day": str(day),
        "at": snapshot.at,
        "ledger_units": int(ledger_units or 0),
        "site_published": int(snapshot.published_today or 0),
        "delta": int(snapshot.published_today or 0) - int(ledger_units or 0),
        "drafts_pending": int(snapshot.drafts_pending or 0),
        "note": str(note or "")[:200],
    }
    if extra:
        row["extra"] = extra
    path = reconcile_file()
    try:
        if path.exists():                   # 同日同偏差只记一次，避免刷屏
            with open(path, encoding="utf-8") as f:
                for line in f.readlines()[-50:]:
                    try:
                        old = json.loads(line)
                    except ValueError:
                        continue
                    if (str(old.get("day")) == row["day"]
                            and int(old.get("delta") or 0) == row["delta"]
                            and int(old.get("site_published") or 0)
                            == row["site_published"]):
                        return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.info("进度校核：台账 %d 篇 vs 线上 %d 篇（差 %+d）",
                 row["ledger_units"], row["site_published"], row["delta"])
        return True
    except Exception as exc:                # noqa: BLE001 差异日志不该影响主流程
        log.warning("差异日志写入失败：%s", exc)
        return False


def load_reconcile(day="", limit=50):
    """读差异记录（倒序），供界面展示「哪几次原本会算错」。"""
    path = reconcile_file()
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
                except ValueError:
                    continue
                if day and str(row.get("day")) != str(day):
                    continue
                rows.append(row)
    except OSError:
        return []
    return rows[-max(1, int(limit)):][::-1]
