"""Real non-publishing acceptance for clean and parallel web generation.

The caller must set ``AQ_DATA_DIR`` before importing this module's production
dependencies.  Outputs live under the supplied isolated acceptance directory.
"""
from __future__ import annotations

import json
from pathlib import Path
import time


def _write(path: Path, value) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _materials(runtime: Path, count: int = 2) -> list[dict]:
    from applications.zhihu_story.author_profiler import iter_collected_stories

    selected = []
    seen = set()
    for source in iter_collected_stories(str(runtime / "data" / "collected_stories.jsonl")):
        title, answer = (source.get("title") or "").strip(), (source.get("answer") or "").strip()
        if title and len(answer) >= 500 and title not in seen:
            selected.append({"index": len(selected) + 1, "title": title, "answer": answer,
                             "url": source.get("url") or ""})
            seen.add(title)
        if len(selected) >= count:
            return selected
    raise RuntimeError(f"隔离素材不足：需要 {count} 篇不同题目、每篇至少500字")


def _ledger_driver(driver, ledger: list[dict]):
    """Observe real cleanup calls without changing their behavior."""
    original = driver.delete_current_session

    def delete():
        before = {"owned": bool(driver._session_owned),
                  "pending": list(driver._pending_sessions)}
        deleted = original()
        ledger.append({"driver": id(driver), "before": before, "deleted": bool(deleted),
                       "owned_after": bool(driver._session_owned),
                       "pending_after": list(driver._pending_sessions)})
        return deleted

    driver.delete_current_session = delete
    return driver


def _recorded_generate(driver, raw: list[dict]):
    original = driver.generate

    def generate(prompt, *args, **kwargs):
        response = original(prompt, *args, **kwargs)
        raw.append({"prompt_chars": len(str(prompt)), "response": response})
        return response

    driver.generate = generate
    return driver


def _cleanup_ok(drivers: list, ledger: list[dict]) -> bool:
    attempted = [item for item in ledger if item["before"]["owned"] or item["before"]["pending"]]
    return bool(attempted) and all(not d._session_owned and not d._pending_sessions for d in drivers)


def run_generation_workflows(out: Path, runtime: Path, only=None) -> dict:
    """Run selected real web generation workflows in the already-isolated runtime.

    ``only`` accepts ``clean`` and/or ``parallel``.  This function never writes
    a Zhihu draft or sends a comment; it only creates local acceptance files.
    """
    allowed = ("clean", "parallel")
    selected = list(allowed if only is None else only)
    invalid = [name for name in selected if name not in allowed]
    if invalid:
        raise ValueError(f"未知生成验收项：{invalid}")
    out, runtime = Path(out), Path(runtime)
    out.mkdir(parents=True, exist_ok=True)
    result = {"started_at": time.time(), "included_checks": selected, "items": {}}

    if "clean" in selected:
        result["items"]["clean"] = _run_clean(out, runtime)
    if "parallel" in selected:
        result["items"]["parallel"] = _run_parallel(out, runtime)
    result["ended_at"] = time.time()
    result["all_passed"] = bool(selected) and all(item["passed"] for item in result["items"].values())
    _write(out / "generation-result.json", result)
    return result


def _run_clean(out: Path, runtime: Path) -> dict:
    from workflows.zhihu import ZhihuWorkflow
    import web_drivers

    started, raw, ledger = time.time(), [], []
    driver = _recorded_generate(_ledger_driver(web_drivers.create_driver(), ledger), raw)
    original_get = web_drivers.get_driver
    evidence = out / "clean.json"
    try:
        web_drivers.get_driver = lambda: driver
        material = _materials(runtime, 1)[0]
        workflow = ZhihuWorkflow()
        story, audit = workflow.generate_clean_with_retry(material["title"], material["answer"])
        if story:
            (out / "clean.md").write_text(story, encoding="utf-8")
        audit_path = out / "audit.json"
        _write(audit_path, audit or {})
        passed = bool(story and audit and audit.get("passed") and audit.get("llm_detail"))
        payload = {"material": {"title": material["title"], "answer_chars": len(material["answer"])},
                   "story_chars": len(story or ""), "audit": audit, "raw": raw,
                   "clean_file": str(out / "clean.md") if story else None,
                   "audit_file": str(audit_path)}
    except Exception as exc:
        passed = False
        payload = {"error": type(exc).__name__, "message": str(exc), "raw": raw}
    finally:
        try:
            driver.delete_current_session()
            driver.close_session()
        except Exception as exc:
            payload["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        web_drivers.get_driver = original_get
    cleanup = _cleanup_ok([driver], ledger)
    payload["cleanup_ledger"] = ledger
    return {"passed": bool(passed and cleanup), "schema": "story+passed audit.llm_detail",
            "elapsed": time.time() - started, "cleanup_confirmed": cleanup,
            "evidence": _write(evidence, payload)}


def _run_parallel(out: Path, runtime: Path) -> dict:
    from workflows.zhihu import ZhihuWorkflow
    from core.story_text import validate_story_format
    from config import WEB_DRIVERS, WEB_DRIVER_NAME
    import web_drivers

    started, created, ledger = time.time(), [], []
    original_create = web_drivers.create_driver

    def create():
        driver = _ledger_driver(original_create(), ledger)
        created.append(driver)
        return driver

    evidence = out / "parallel.json"
    try:
        web_drivers.create_driver = create
        materials = _materials(runtime, 2)
        config = dict(WEB_DRIVERS[WEB_DRIVER_NAME])
        config.update(parallel_tabs=2, scan_interval=1)
        workflow = ZhihuWorkflow()
        workflow._batch_generate_web_parallel(materials, config)
        assessed = []
        for material in materials:
            story = material.get("story") or ""
            score, valid, detail = validate_story_format(story) if story else (0, False, {})
            assessed.append({"index": material["index"], "title": material["title"],
                             "chars": len(story), "format_score": score,
                             "format_valid": valid, "format_detail": detail,
                             "md_path": material.get("md_path")})
        passed = len(created) == 2 and all(item["chars"] >= 500 and item["format_valid"] for item in assessed)
        payload = {"materials": assessed, "drivers_created": len(created)}
    except Exception as exc:
        passed = False
        payload = {"error": type(exc).__name__, "message": str(exc), "drivers_created": len(created)}
    finally:
        web_drivers.create_driver = original_create
    cleanup = _cleanup_ok(created, ledger)
    payload["cleanup_ledger"] = ledger
    return {"passed": bool(passed and cleanup), "schema": "two format-valid stories >=500 chars",
            "elapsed": time.time() - started, "cleanup_confirmed": cleanup,
            "evidence": _write(evidence, payload)}
