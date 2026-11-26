"""Read-only descriptive growth baseline for published answer snapshots."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Allow ``python tools/growth_report.py`` as well as module imports.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.paths import DATA_ROOT
from core import account_metrics

METRICS = ("reads", "likes", "comments", "collects", "favors")
LABELS = {"阅读": "reads", "赞同": "likes", "评论": "comments", "收藏": "collects", "喜欢": "favors"}
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
NUM_RE = re.compile(r"^([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)(万|千|[wk])?$", re.I)


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) and value >= 0 else None
        except OverflowError:
            return None
    text = str(value).strip().replace(" ", "")
    m = NUM_RE.match(text)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    n *= {"万": 10000, "千": 1000, "w": 10000, "k": 1000}.get(unit, 1)
    if not math.isfinite(n):
        return None
    return int(n) if n.is_integer() else n


def _pub_date(row):
    for key in ("publish_date", "publish"):
        value = row.get(key)
        if isinstance(value, str):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
            if m:
                try:
                    return date.fromisoformat(m.group(1))
                except ValueError:
                    pass
    return None


def _observation_date(obj, path):
    value = obj.get("generated_at") if isinstance(obj, dict) else None
    m = DATE_RE.search(str(value or ""))
    if not m:
        m = DATE_RE.search(path.name)
    try:
        return date.fromisoformat(m.group(1)) if m else None
    except ValueError:
        return None


def _rows(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
        return obj["rows"]
    return []


def _identity(row):
    aid = str(row.get("aid") or "").strip()
    if aid:
        return aid
    url = str(row.get("url") or "").strip()
    match = re.search(r"/answer/(\d+)", url)
    return match.group(1) if match else (url or None)


def _metric_value(container, key):
    """Return whether *key* was supplied and its unparsed value."""
    if key in container:
        return True, container[key]
    for label, name in LABELS.items():
        if name == key and label in container:
            return True, container[label]
    return False, None


def _parsed_metrics(row, *, source="snapshot"):
    nested = row.get("metrics") if isinstance(row.get("metrics"), dict) else None
    out = {}
    ambiguous_legacy_zero_fields = 0
    for key in METRICS:
        # A nested metrics mapping records the raw field, including a real zero.
        supplied, value = _metric_value(nested, key) if nested is not None else _metric_value(row, key)
        number = _number(value) if supplied else None
        # Published snapshots were normalized before being persisted, so a flat
        # zero cannot tell a missing/bad source value from a measured zero.
        if nested is None and source != "fresh" and number == 0:
            ambiguous_legacy_zero_fields += 1
            number = None
        out[key] = number
    return out, ambiguous_legacy_zero_fields


def _metrics(row):
    """Compatibility helper returning only the metric values."""
    return _parsed_metrics(row)[0]


def _state_path(root):
    return Path(root) / "data" / "state" / "evolution.json"


def _state_observation_date(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def load_observations(root, *, include_snapshot_dates=False):
    root = Path(root)
    dirs = [root / "data"] if (root / "data").is_dir() else [root]
    files = sorted({p for d in dirs for p in d.glob("published_answers_*.json") if p.is_file()})
    observations, source_files, source_errors = [], [], []
    ambiguous_legacy_zero_fields = 0
    snapshot_dates = set()
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as exc:
            source_errors.append({"file": path.name, "error": "invalid_json" if isinstance(exc, ValueError) else "read_error"})
            continue
        if not (isinstance(obj, list) or
                (isinstance(obj, dict) and isinstance(obj.get("rows"), list))):
            source_errors.append({"file": path.name, "error": "invalid_schema"})
            continue
        od = _observation_date(obj, path)
        if not od:
            source_errors.append({"file": path.name, "error": "missing_observation_date"})
            continue
        source_files.append(path.name)
        snapshot_dates.add(od)
        # Last row wins for an id on a given observation date.
        dedup = {}
        for row in _rows(obj):
            if not isinstance(row, dict):
                continue
            ident = _identity(row)
            if ident:
                metrics, ambiguous = _parsed_metrics(row)
                ambiguous_legacy_zero_fields += ambiguous
                dedup[ident] = {"key": ident, "obs_date": od, "publish_date": _pub_date(row),
                                "title": str(row.get("title") or ""), "metrics": metrics,
                                "source": "legacy_snapshot", "observed_at": od.isoformat()}
        observations.extend(dedup.values())
    state_path = _state_path(root)
    if state_path.exists():
        state_name = "state/evolution.json"
        try:
            with state_path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
            articles = state.get("articles") if isinstance(state, dict) else None
            if not isinstance(articles, dict) or state.get("schema_version") != 1:
                raise ValueError("invalid articles")
        except (OSError, ValueError, json.JSONDecodeError):
            source_errors.append({"file": state_name, "error": "invalid_schema"})
        else:
            source_files.append(state_name)
            for aid, article in articles.items():
                if not isinstance(article, dict) or not isinstance(article.get("observations"), list):
                    source_errors.append({"file": state_name, "error": "invalid_schema"})
                    continue
                ident = _identity(article) or str(aid).strip()
                if not ident:
                    continue
                publish_date = _pub_date(article)
                for item in article["observations"]:
                    if not isinstance(item, dict):
                        source_errors.append({"file": state_name, "error": "invalid_schema"})
                        continue
                    od = _state_observation_date(item.get("observed_at"))
                    if not od:
                        source_errors.append({"file": state_name, "error": "invalid_schema"})
                        continue
                    source = "fresh" if item.get("source") == "fresh" else "legacy_state"
                    metrics, ambiguous = _parsed_metrics(item, source=source)
                    ambiguous_legacy_zero_fields += ambiguous
                    observations.append({"key": ident, "obs_date": od, "publish_date": publish_date,
                                         "title": str(article.get("title") or ""),
                                         "metrics": metrics, "source": source,
                                         "observed_at": item.get("observed_at")})
    result = (observations, sorted(set(source_files)), source_errors,
              ambiguous_legacy_zero_fields)
    if include_snapshot_dates:
        return result + (snapshot_dates,)
    return result


def _attribution_context(root):
    """Read recorded attribution metadata without changing evolution state."""
    state_path = _state_path(root)
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return {}
    # Older/fixture state files may omit optional evolution collections.
    data = dict(data)
    for key in ("generations", "drafts", "publication_receipts"):
        if not isinstance(data.get(key), list):
            data[key] = []
    if not isinstance(data.get("articles"), dict):
        return {}
    try:
        from core import evolution
        generations = {g.get("id"): g for g in data.get("generations", []) if isinstance(g, dict) and g.get("id")}
        nodes = {n.get("id"): n for n in evolution._nodes(data) if isinstance(n, dict) and n.get("id")}
    except (AttributeError, TypeError, ValueError):
        return {}
    out = {}
    for aid, article in (data.get("articles") or {}).items():
        if not isinstance(article, dict):
            continue
        key = _identity(article) or str(aid).strip()
        if not key:
            continue
        generation = generations.get(article.get("generation_id"), {})
        try:
            node, attribution, source, reason, version_confirmed = evolution._resolve_attribution(
                article, data, generations, nodes, bool(article.get("observations")))
        except (AttributeError, KeyError, TypeError, ValueError):
            node, attribution, source, reason, version_confirmed = (None, "unassigned", None, "unavailable", False)
        settings = generation.get("settings") if isinstance(generation.get("settings"), dict) else {}
        workflow = settings.get("WORKFLOW_MODE") or settings.get("workflow_mode")
        chain = generation.get("model_chain") if isinstance(generation.get("model_chain"), dict) else {}

        def chain_value(name):
            # Only scalar provenance fields are reportable; never expose options or
            # other provider configuration objects.
            value = chain.get(name)
            return str(value) if isinstance(value, (str, int, float, bool)) and str(value) else None

        confirmed = attribution == "confirmed"
        verified = source == "verified_chain"
        options = chain.get("options") if isinstance(chain.get("options"), dict) else {}
        model_mode = options.get("mode") if isinstance(options.get("mode"), str) else None
        out[key] = {
            "title": str(article.get("title") or ""),
            "confirmed": confirmed,
            "verified_chain": verified,
            "version": str(generation.get("version") or "").lstrip("vV") or None if verified and version_confirmed else None,
            "workflow": str(workflow) if workflow and verified else None,
            "episode": str(generation.get("episode") or "") or None if verified else None,
            "node": str(node) if node and confirmed else None,
            "channel": chain_value("channel") if verified else None,
            "provider": chain_value("provider") if verified else None,
            "model": chain_value("model") if verified else None,
            "model_mode": model_mode if verified else None,
            "reason": reason,
        }
    return out


def _group_stats(rows):
    return {"n": len(rows), "stats": _sample_stats(rows)}


def _grouped_reports(rows):
    """Aggregate fixed samples by inferred genre and recorded attribution."""
    try:
        from core.detectors import classify_genre
    except ImportError:
        classify_genre = lambda text: "其他"
    genre = {}
    attribution = {}
    dimensions = {"version": {}, "workflow": {}, "episode": {}, "scheme": {},
                  "channel": {}, "provider": {}, "model": {}, "model_mode": {}}
    cohorts = {}
    for row in rows:
        title = str(row.get("title") or "").strip()
        genre_key = classify_genre(title) if title else "unknown"
        genre.setdefault(genre_key, []).append(row)
        info = row.get("attribution") or {}
        state = "confirmed" if info.get("confirmed") else "unknown"
        attribution.setdefault(state, []).append(row)
        for dimension in dimensions:
            source_dimension = "node" if dimension == "scheme" else dimension
            value = info.get(source_dimension) if info.get("confirmed") else None
            dimensions[dimension].setdefault(value or "unknown", []).append(row)
        labels = {
            "genre": genre_key,
            "attribution": state,
            "workflow": info.get("workflow") or "unknown",
            "scheme": info.get("node") or "unknown",
            "channel": info.get("channel") or "unknown",
            "provider": info.get("provider") or "unknown",
            "model": info.get("model") or "unknown",
            "model_mode": info.get("model_mode") or "unknown",
        }
        cohort_key = json.dumps(labels, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cohorts.setdefault(cohort_key, []).append(row)
    return {
        "genre": {key: dict(_group_stats(items), label_basis="title_rule_inferred" if key != "unknown" else "unknown")
                  for key, items in sorted(genre.items())},
        "attribution": {
            **{key: _group_stats(items) for key, items in sorted(attribution.items())},
            "by_dimension": {
                dimension: {str(key): _group_stats(items) for key, items in sorted(groups.items(), key=lambda item: str(item[0]))}
                for dimension, groups in dimensions.items()
            },
        },
        "cohorts": {key: dict(labels=json.loads(key), **_group_stats(items))
                    for key, items in sorted(cohorts.items())},
    }


def _summary(values):
    if not values:
        return {"valid_n": 0, "sum": None, "median": None, "p25": None, "mean": None, "zero_share": None, "top_share": None}
    vals = sorted(values)
    total = sum(vals)
    return {"valid_n": len(vals), "sum": total, "median": statistics.median(vals),
            "p25": vals[max(0, math.ceil(len(vals) * .25) - 1)], "mean": total / len(vals),
            "zero_share": sum(v == 0 for v in vals) / len(vals),
            "top_share": max(vals) / total if total else None}


def _sample_stats(rows):
    result = {metric: _summary([r["metrics"][metric] for r in rows if r["metrics"].get(metric) is not None]) for metric in METRICS}
    for metric in ("likes", "comments", "collects", "favors"):
        eligible = [(r["metrics"]["reads"], r["metrics"][metric]) for r in rows
                    if r["metrics"].get("reads") is not None
                    and r["metrics"].get(metric) is not None
                    and r["metrics"]["reads"] > 0]
        result[f"{metric}_per_1000_reads"] = _rate_summary(eligible)
    return result


def _rate_summary(eligible):
    rates = [value * 1000 / reads for reads, value in eligible]
    effective_reads = sum(r for r, _ in eligible) if eligible else None
    return {"eligible_n": len(eligible), "mean": statistics.mean(rates) if rates else None,
            "median": statistics.median(rates) if rates else None,
            "p25": sorted(rates)[max(0, math.ceil(len(rates) * .25) - 1)] if rates else None,
            "pooled": 1000 * sum(v for _, v in eligible) / effective_reads if eligible else None,
            "effective_reads": effective_reads,
            "reads_sum": effective_reads,
            "mean_definition": "篇均转化率；pooled为有效样本总互动/总阅读"}


def _comparison_snapshot(observations, target):
    """Return the one published snapshot on *target*, without state history."""
    rows = {}
    for row in observations:
        if row.get("source") == "legacy_snapshot" and row.get("obs_date") == target:
            rows[row["key"]] = row
    return rows


def _comparison_metric(before, after, metric):
    before_values = {key: row["metrics"].get(metric) for key, row in before.items()}
    after_values = {key: row["metrics"].get(metric) for key, row in after.items()}
    retained_keys = before.keys() & after.keys()
    matched = [key for key in retained_keys
               if before_values[key] is not None and after_values[key] is not None]
    absent_after = before.keys() - after.keys()
    new_after = after.keys() - before.keys()

    def total(values):
        return {"valid_n": len(values), "sum": sum(values) if values else None}

    retained_before = [before_values[key] for key in matched]
    retained_after = [after_values[key] for key in matched]
    return {
        "before_total": total([v for v in before_values.values() if v is not None]),
        "after_total": total([v for v in after_values.values() if v is not None]),
        "retained_before": total(retained_before),
        "retained_after": total(retained_after),
        "delta": (sum(retained_after) - sum(retained_before) if matched else None),
        "matched_n": len(matched),
        "retained_before_missing_n": len(retained_keys) - sum(
            before_values[key] is not None for key in retained_keys),
        "retained_after_missing_n": len(retained_keys) - sum(
            after_values[key] is not None for key in retained_keys),
        "missing_before_total": total([before_values[key] for key in absent_after
                                        if before_values[key] is not None]),
        "new_after_total": total([after_values[key] for key in new_after
                                   if after_values[key] is not None]),
    }


def _corpus_comparison(observations, from_date, to_date):
    before = _comparison_snapshot(observations, from_date)
    after = _comparison_snapshot(observations, to_date)
    return {
        "from": from_date.isoformat(), "to": to_date.isoformat(),
        "before_n": len(before), "after_n": len(after),
        "retained_n": len(before.keys() & after.keys()),
        "absent_from_snapshot_n": len(before.keys() - after.keys()),
        "new_in_snapshot_n": len(after.keys() - before.keys()),
        "metrics": {metric: _comparison_metric(before, after, metric)
                    for metric in METRICS},
        "comparison_scope": "full_snapshots_unfiltered_by_since",
        "interpretation": "absent_from_snapshot 表示该作品不在后一份快照中，不等同于删除、下架或风控。",
    }


def build_report(data_root=DATA_ROOT, since=None, horizon=7,
                 compare_from=None, compare_to=None):
    if horizon not in (7, 14, 30, 90):
        raise ValueError("horizon must be one of 7, 14, 30, 90")
    if (compare_from is None) != (compare_to is None):
        raise ValueError("compare_from and compare_to must be provided together")
    compare_from_date = date.fromisoformat(compare_from) if compare_from is not None else None
    compare_to_date = date.fromisoformat(compare_to) if compare_to is not None else None
    if compare_from_date and compare_to_date and compare_from_date > compare_to_date:
        raise ValueError("compare_from must not be later than compare_to")
    (observations, source_files, source_errors, ambiguous_legacy_zero_fields,
     snapshot_dates) = load_observations(data_root, include_snapshot_dates=True)
    attribution_context = _attribution_context(data_root)
    if compare_from_date and compare_from_date not in snapshot_dates:
        raise ValueError(f"no published snapshot for compare_from: {compare_from_date.isoformat()}")
    if compare_to_date and compare_to_date not in snapshot_dates:
        raise ValueError(f"no published snapshot for compare_to: {compare_to_date.isoformat()}")
    cutoff = max((r["obs_date"] for r in observations), default=None)
    since_date = date.fromisoformat(since) if since else None
    by_key = {}
    # A snapshot may be copied more than once.  Only one observation per
    # work and observation day participates in either sample.
    unique = {}
    for row in observations:
        if since_date and row["publish_date"] and row["publish_date"] < since_date:
            continue
        slot = (row["key"], row["obs_date"])
        existing = unique.get(slot)
        if existing is None:
            unique[slot] = row
            continue
        # Raw fresh observations supersede a normalized snapshot for the whole
        # observation.  In particular, missing raw values must remain missing
        # rather than being filled with snapshot zeroes.  Repeated fresh reads
        # for a day use the newest timestamp.
        is_fresh = row.get("source") == "fresh"
        existing_fresh = existing.get("source") == "fresh"
        if is_fresh and (not existing_fresh or str(row.get("observed_at") or "") >= str(existing.get("observed_at") or "")):
            unique[slot] = row
        elif not existing_fresh and not is_fresh:
            unique[slot] = row
    for row in unique.values():
        context = attribution_context.get(row["key"], {})
        if context.get("title") and not row.get("title"):
            row["title"] = context["title"]
        row["attribution"] = context
        by_key.setdefault(row["key"], []).append(row)
    latest, fixed = [], []
    unknown = immature = missing = 0
    for rows in by_key.values():
        rows.sort(key=lambda r: r["obs_date"])
        pub = next((r["publish_date"] for r in rows if r["publish_date"]), None)
        if not pub:
            unknown += 1
            continue
        target = pub + timedelta(days=horizon)
        newest = rows[-1]
        if newest["obs_date"] >= target:
            latest.append(newest)
        else:
            immature += 1
        candidates = [r for r in rows if target <= r["obs_date"] <= target + timedelta(days=3)]
        if candidates:
            fixed.append(min(candidates, key=lambda r: (r["obs_date"] - target, r["obs_date"])))
        elif newest["obs_date"] >= target:
            missing += 1
    report = {"source_files": source_files, "source_errors": source_errors,
            "source_quality": {
                "legacy_snapshot": "flat normalized snapshot zeroes are treated as missing because they cannot distinguish missing or invalid source values from measured zeroes",
                "fresh_state": "fresh evolution observations preserve raw zeroes; missing raw values remain missing",
                "ambiguous_legacy_zero_fields": ambiguous_legacy_zero_fields,
            },
            "observation_cutoff_date": cutoff.isoformat() if cutoff else None,
            "observation_count": len(unique), "total_articles": len(by_key), "since": since,
            "horizon": horizon, "latest": {"n": len(latest), "stats": _sample_stats(latest),
                                             "groups": _grouped_reports(latest)},
            "fixed_horizon": {"n": len(fixed), "stats": _sample_stats(fixed),
                               "groups": _grouped_reports(fixed)},
            "unknown_publish_date": unknown, "immature": immature, "missing": missing,
            "all_public_context": "描述性全量公开快照上下文，不能将历史账号全部作品归因于 AutoQuill 或本版本。",
            "follower_growth": account_metrics.report(data_root, since=since),
            "per_run_hour": "unavailable"}
    if compare_from_date:
        report["corpus_comparison"] = _corpus_comparison(
            observations, compare_from_date, compare_to_date)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="published snapshot growth baseline")
    parser.add_argument("--data-root", default=DATA_ROOT)
    parser.add_argument("--since")
    parser.add_argument("--horizon", type=int, choices=(7, 14, 30, 90), default=7)
    parser.add_argument("--compare-from", dest="compare_from")
    parser.add_argument("--compare-to", dest="compare_to")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.since:
        try:
            date.fromisoformat(args.since)
        except ValueError:
            parser.error("--since must be YYYY-MM-DD")
    for name in ("compare_from", "compare_to"):
        value = getattr(args, name)
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                parser.error(f"--{name.replace('_', '-')} must be YYYY-MM-DD")
    try:
        report = build_report(args.data_root, args.since, args.horizon,
                              args.compare_from, args.compare_to)
    except ValueError as exc:
        parser.error(str(exc))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        # Piped JSON has the same UTF-8 contract as --output on every platform.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
