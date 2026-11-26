"""Small, append-only provenance index for published stories."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading

from core import paths


_LOCK = threading.RLock()
_CHAIN_FIELDS = ("channel", "provider", "model", "options", "workflow")
_SENSITIVE_OPTION_PARTS = (
    "api_key", "apikey", "secret", "password", "passwd", "token",
    "prompt", "content", "正文", "提示词",
)


def index_path() -> Path:
    return Path(paths.data("data", "state", "story_models.jsonl"))


def _scalar(value):
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _settings_value(settings, name):
    if isinstance(settings, dict):
        return settings.get(name)
    return getattr(settings, name, None)


def api_chain() -> dict:
    import config

    return {
        "channel": "api",
        "provider": _scalar(getattr(config, "LLM_PROVIDER", None)),
        "model": _scalar(getattr(config, "LLM_API_MODEL", None)),
    }


def web_chain(driver) -> dict:
    selection = getattr(driver, "observed_model_selection", None)
    if not isinstance(selection, dict):
        selection = {}
    options = selection.get("options")
    if isinstance(options, dict):
        options = {
            str(key): value
            for key, value in options.items()
            if isinstance(value, (str, int, float, bool))
            and not any(part in str(key).lower() for part in _SENSITIVE_OPTION_PARTS)
        }
    else:
        options = None
    return {
        "channel": "web",
        "provider": _scalar(getattr(driver, "provider_name", None)),
        "model": _scalar(selection.get("model")),
        "options": options,
    }


def chain_from_settings(settings) -> dict:
    mode = _settings_value(settings, "LLM_MODE")
    if mode == "web":
        chain = {
            "channel": "web",
            "provider": _scalar(
                _settings_value(settings, "WEB_LLM_DRIVER")
                or _settings_value(settings, "WEB_DRIVER_NAME")
            ),
            "model": None,
        }
    elif mode == "api":
        chain = {
            "channel": "api",
            "provider": _scalar(_settings_value(settings, "LLM_PROVIDER")),
            "model": _scalar(_settings_value(settings, "LLM_MODEL_ID")),
        }
    else:
        chain = {"channel": "unknown", "provider": None, "model": None}
    workflow = _scalar(_settings_value(settings, "WORKFLOW_MODE"))
    if workflow is not None:
        chain["workflow"] = workflow
    return chain


def _clean_chain(chain):
    source = chain if isinstance(chain, dict) else {}
    cleaned = {}
    for field in _CHAIN_FIELDS:
        if field == "options":
            value = source.get(field)
            if isinstance(value, dict):
                cleaned[field] = {
                    str(key): item
                    for key, item in value.items()
                    if isinstance(item, (str, int, float, bool))
                    and not any(part in str(key).lower() for part in _SENSITIVE_OPTION_PARTS)
                }
            elif field in source:
                cleaned[field] = None
        elif field in source:
            cleaned[field] = _scalar(source[field])
    return cleaned


def append_publication(title, url, chain) -> bool:
    """Append publication provenance, returning False for an exact duplicate."""
    title = _scalar(title)
    url = _scalar(url)
    safe_chain = _clean_chain(chain)
    path = index_path()
    with _LOCK:
        last_for_url = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        old = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(old, dict) and old.get("url") == url:
                        last_for_url = old
        except FileNotFoundError:
            pass
        if (
            last_for_url is not None
            and last_for_url.get("title") == title
            and _clean_chain(last_for_url.get("chain")) == safe_chain
        ):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "title": title,
            "url": url,
            "chain": safe_chain,
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True
