"""Discovered website capabilities and explicit per-website user selections.

Catalogs contain public model labels and option values, never login material.
Discovery and workflow validation are separate pieces of evidence.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from core import paths

_lock = threading.RLock()


def _read(filename):
    try:
        value = json.loads(Path(filename).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(filename, value):
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=target.parent, delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _provider(name):
    from config import WEB_DRIVERS
    if name not in WEB_DRIVERS:
        raise ValueError("未知的网页版模型")


def _catalog_file():
    return paths.data("data", "web_llm_capabilities.json")


def _selection_file():
    return paths.data("config", "web_llm_preferences.json")


def get_provider_settings(name):
    _provider(name)
    with _lock:
        catalog = _read(_catalog_file()).get(name) or {
            "status": "not_checked", "checked_at": None, "models": [],
            "current": {"model": "", "options": {}},
            "changes": {"added": [], "removed": []},
            "message": "请检查状态，刷新网页内可用模型和选项",
        }
        selection = _read(_selection_file()).get(name) or {"model": "", "options": {}}
        return copy.deepcopy({"provider": name, "selection": selection,
                              "catalog": catalog,
                              "validation": catalog.get("validation", {})})


def validate_selection(catalog, model, options):
    if catalog.get("status") != "ready":
        raise ValueError("请先检查状态，刷新网页内可用模型")
    match = next((m for m in catalog.get("models", []) if m["id"] == model), None)
    if not match:
        raise ValueError("所选模型已不可用，请检查状态后重新选择")
    if match.get("selectable") is False:
        raise ValueError(match.get("unsupported_reason") or "该模型不支持当前文字工作流")
    if not isinstance(options, dict):
        raise ValueError("模型选项必须为对象")
    specs = {s["key"]: s for s in match.get("options", [])}
    for key, value in options.items():
        if key not in specs or value not in {c["id"] for c in specs[key]["choices"]}:
            raise ValueError(f"所选模型不支持选项 {key}={value}，请刷新并重新选择")
    return match


def save_provider_selection(name, model, options):
    _provider(name)
    with _lock:
        settings = get_provider_settings(name)
        spec = validate_selection(settings["catalog"], model, options)
        options = {**spec.get("defaults", {}), **options}
        validate_selection(settings["catalog"], model, options)
        selections = _read(_selection_file())
        selections[name] = {"model": model, "options": dict(options)}
        _write(_selection_file(), selections)
        return get_provider_settings(name)


def remember_catalog(name, catalog):
    _provider(name)
    with _lock:
        catalogs = _read(_catalog_file())
        previous = catalogs.get(name) or {}
        old = {m["id"] for m in previous.get("models", [])}
        new = {m["id"] for m in catalog.get("models", [])}
        def option_signature(model):
            return {g["key"]: sorted(c["id"] for c in g.get("choices", []))
                    for g in model.get("options", [])}
        old_models = {m["id"]: m for m in previous.get("models", [])}
        updated = sorted(m["id"] for m in catalog.get("models", [])
                         if m["id"] in old_models and option_signature(m) != option_signature(old_models[m["id"]]))
        catalog = copy.deepcopy(catalog)
        catalog.update(checked_at=time.time(), changes={
            "added": sorted(new - old), "removed": sorted(old - new)})
        if updated:
            catalog["changes"]["updated"] = updated
        # Validation applies to one exact model/options combination and records
        # the tested runtime version. Discovery alone never promotes it.
        catalog["validation"] = previous.get("validation", {})
        catalogs[name] = catalog
        _write(_catalog_file(), catalogs)
        return copy.deepcopy(catalog)


def refresh_capabilities(name, page):
    from web_drivers.site_options import discover_options
    from web_drivers.browser_pool import WorkflowCancelled, WebLoginRequired
    try:
        catalog = discover_options(name, page)
        if not catalog.get("models"):
            raise RuntimeError("未识别到可选择的聊天模型，网页可能已改版")
        catalog.update(status="ready", message="已从当前账号网页刷新")
    except (WorkflowCancelled, WebLoginRequired):
        raise
    except Exception as exc:
        # Preserve last known choices for diagnosis, but do not present them as
        # fresh or accept new selections on a failed refresh.
        catalog = get_provider_settings(name)["catalog"]
        catalog.update(status="error", message=f"模型选项刷新失败：{type(exc).__name__}")
    return remember_catalog(name, catalog)


def apply_selection(driver):
    from web_drivers.site_options import apply_options
    name = driver.provider_name
    selection = get_provider_settings(name)["selection"]
    if selection.get("model"):
        catalog = get_provider_settings(name)["catalog"]
        validate_selection(catalog, selection["model"], selection.get("options", {}))
        # Applying and reading back the actual choice rechecks the website.
        # Do not cycle through other models inside an ongoing conversation.
        actual = apply_options(name, driver._page_instance(), selection)
        if actual.get("model") != selection["model"] or any(
                actual.get("options", {}).get(k) != v
                for k, v in selection.get("options", {}).items()):
            raise RuntimeError(f"{name} 未确认所选模型或参数，已停止发送")
        return actual
    return None


def record_validation(name, workflow, selection, result, evidence):
    """Called only by the real workflow acceptance runner, with saved evidence."""
    from core.version import VERSION
    with _lock:
        catalogs = _read(_catalog_file())
        catalog = catalogs.setdefault(name, {})
        key = json.dumps({"workflow": workflow, "selection": selection},
                         ensure_ascii=False, sort_keys=True)
        catalog.setdefault("validation", {})[key] = {
            "workflow": workflow, "selection": selection, "result": result,
            "version": VERSION, "tested_at": time.time(), "evidence": evidence,
        }
        _write(_catalog_file(), catalogs)
