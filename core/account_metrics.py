"""Append-only, account-level follower observations.

This module deliberately stores only the minimum verified observation fields;
it does not retain a provider profile response or estimate per-story follows.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading

from core.paths import DATA_ROOT
from core.release_windows import ZH_TZ


_LOCK = threading.RLock()
_NAME = "account_metrics.jsonl"
_SOURCE = "zhihu_me"


def _path(data_root=DATA_ROOT) -> Path:
    return Path(data_root) / "data" / "state" / _NAME


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(ZH_TZ).isoformat(timespec="seconds")


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def record_observation(account_id, followers, observed_at, data_root=DATA_ROOT):
    """Append one verified Zhihu follower observation, returning whether added."""
    if not isinstance(account_id, str) or not account_id.strip():
        return False
    count = _count(followers)
    at = _timestamp(observed_at)
    if count is None or at is None:
        return False
    record = {"at": at, "id": account_id.strip(), "count": count, "source": _SOURCE}
    path = _path(data_root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                handle = path.open("r", encoding="utf-8")
            except FileNotFoundError:
                handle = None
            if handle is not None:
                with handle:
                    for line in handle:
                        try:
                            old = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(old, dict) and all(old.get(k) == record[k] for k in record):
                            return False
            payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            with path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
        except OSError:
            return False
    return True


def report(data_root=DATA_ROOT, since=None):
    """Return descriptive account-level changes without exposing account IDs."""
    since_date = None
    if since:
        try:
            from datetime import date
            since_date = date.fromisoformat(str(since))
        except ValueError:
            return {"scope": "account-level follower observations", "status": "unavailable",
                    "account_count": 0, "valid_observation_n": 0, "invalid_n": 0, "unknown_n": 1,
                    "accounts": []}
    valid = {}
    invalid_n = 0
    path = _path(data_root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        try:
            item = json.loads(line)
            at = _timestamp(item.get("at")) if isinstance(item, dict) else None
            count = _count(item.get("count")) if isinstance(item, dict) else None
            ident = item.get("id") if isinstance(item, dict) else None
            if (item.get("source") != _SOURCE or not isinstance(ident, str) or not ident.strip()
                    or at is None or count is None):
                raise ValueError
            parsed_date = datetime.fromisoformat(at).date()
            if since_date and parsed_date < since_date:
                continue
            valid.setdefault(ident.strip(), []).append((at, count))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            invalid_n += 1
    accounts = []
    for observations in valid.values():
        observations.sort(key=lambda item: item[0])
        first_at, first_count = observations[0]
        last_at, last_count = observations[-1]
        accounts.append({
            "earliest_at": first_at, "earliest_date": first_at[:10],
            "latest_at": last_at, "latest_date": last_at[:10],
            "earliest_count": first_count, "latest_count": last_count,
            "net_change": last_count - first_count if len(observations) >= 2 else None,
            "valid_observation_n": len(observations),
            "change": "observed" if len(observations) >= 2 else "unavailable",
        })
    accounts.sort(key=lambda item: (item["earliest_at"], item["latest_at"]))
    return {
        "scope": "account-level follower observations; not attributable to individual stories",
        "status": "ok" if accounts else "unavailable",
        "account_count": len(accounts),
        "valid_observation_n": sum(item["valid_observation_n"] for item in accounts),
        "invalid_n": invalid_n,
        "unknown_n": invalid_n,
        "accounts": accounts,
    }
