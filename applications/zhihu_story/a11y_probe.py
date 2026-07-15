"""Read-only Windows UI Automation diagnostics for the active Edge window."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
from typing import Any


TARGET_ACTION_NAMES = ("写回答", "稍后答", "已加草稿", "编辑回答")
INTERACTION_KEYWORDS = ("赞同", "点赞", "评论", "收藏")
HOT_LABEL_KEYWORDS = ("飙升", "热门", "火爆")
QUESTION_CONTROL_TYPES = {
    "TextControl",
    "HyperlinkControl",
    "ListItemControl",
    "ButtonControl",
}
MAX_TREE_NODES = 10000
QUESTION_URL_RE = re.compile(r"^https://www\.zhihu\.com/question/[^/?#]+/?$")
ACTION_STATE_NAMES = {"稍后答": "new", "已加草稿": "drafted"}
ANSWER_ACTION_NAMES = {"写回答": "write", "编辑回答": "edit"}
RECOMMEND_METRICS_RE = re.compile(
    r"^(?P<views>[\d,.]+\s*[万亿]?)\s*浏览\s*[·•]\s*"
    r"(?P<answers>[\d,.]+\s*[万亿]?)\s*回答\s*[·•]\s*"
    r"(?P<followers>[\d,.]+\s*[万亿]?)\s*关注\s*[·•]\s*"
    r"(?P<asked_at>.+)$"
)


def _one_line(value: Any) -> str:
    """Keep UIA text readable without letting a single element break the report."""
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", "").split())


def _safe_property(control: Any, name: str, default: Any = "") -> Any:
    try:
        return getattr(control, name)
    except Exception:
        return default


def _rectangle(control: Any) -> tuple[int, int, int, int] | None:
    rectangle = _safe_property(control, "BoundingRectangle", None)
    if rectangle is None:
        return None
    try:
        values = (
            int(rectangle.left),
            int(rectangle.top),
            int(rectangle.right),
            int(rectangle.bottom),
        )
    except Exception:
        return None
    return values


def _read_value(control: Any, automation: Any) -> str:
    """Read exposed values only; no UIA pattern that mutates a control is used."""
    try:
        pattern = control.GetPattern(automation.PatternId.ValuePattern)
        if pattern:
            return _one_line(pattern.Value)
    except Exception:
        pass

    try:
        legacy = control.GetLegacyIAccessiblePattern()
        if legacy:
            return _one_line(legacy.Value)
    except Exception:
        pass
    return ""


def _iter_tree(root: Any, max_nodes: int) -> Iterator[tuple[Any, int]]:
    """Yield the UIA subtree without invoking, focusing, or otherwise acting on it."""
    stack = [(root, 0)]
    visited = 0
    while stack and visited < max_nodes:
        control, depth = stack.pop()
        visited += 1
        yield control, depth
        try:
            children = control.GetChildren() or []
        except Exception:
            children = []
        for child in reversed(children):
            stack.append((child, depth + 1))


def _control_record(control: Any, depth: int, automation: Any) -> dict[str, Any]:
    return {
        "depth": depth,
        "type": _one_line(_safe_property(control, "ControlTypeName")),
        "name": _one_line(_safe_property(control, "Name")),
        "value": _read_value(control, automation),
        "automation_id": _one_line(_safe_property(control, "AutomationId")),
        "class_name": _one_line(_safe_property(control, "ClassName")),
        "rect": _rectangle(control),
    }


def _is_visible(record: dict[str, Any]) -> bool:
    rect = record["rect"]
    return bool(rect and rect[2] > rect[0] and rect[3] > rect[1])


def _matches_target(record: dict[str, Any], target: str) -> bool:
    return target in record["name"] or target in record["value"]


def _format_record(record: dict[str, Any]) -> str:
    indent = "  " * record["depth"]
    fields = [
        f"[{record['type'] or 'UnknownControl'}]",
        f"name={json.dumps(record['name'], ensure_ascii=False)}",
    ]
    if record["value"]:
        fields.append(f"value={json.dumps(record['value'], ensure_ascii=False)}")
    if record["automation_id"]:
        fields.append(
            f"automation_id={json.dumps(record['automation_id'], ensure_ascii=False)}"
        )
    if record["class_name"]:
        fields.append(
            f"class={json.dumps(record['class_name'], ensure_ascii=False)}"
        )
    fields.append(f"rect={record['rect']}")
    return indent + " ".join(fields)


def _format_focus_record(record: dict[str, Any]) -> str:
    return (
        f"[{record['type'] or 'UnknownControl'}] "
        f"name={json.dumps(record['name'], ensure_ascii=False)} "
        f"value={json.dumps(record['value'], ensure_ascii=False)} "
        f"rect={record['rect']}"
    )


def probe_foreground_edge(
    log_dir: str | Path = "logs",
    source_url: str | None = None,
    max_nodes: int = MAX_TREE_NODES,
) -> dict[str, Any]:
    """Dump the foreground Edge window's UIA tree and a small decision-oriented summary.

    This function deliberately never invokes a UIA pattern, clicks a control, or
    navigates. It only reads properties exposed by the local accessibility tree.
    """
    try:
        import uiautomation as automation
    except ImportError as exc:
        raise RuntimeError(
            f"uiautomation 导入失败（当前 Python：{sys.executable}；原因：{exc}）。"
            "请使用 feature_wx 环境运行，或执行 "
            "C:\\Users\\10162\\miniconda3\\envs\\feature_wx\\python.exe "
            "-m pip install uiautomation"
        ) from exc

    window = automation.GetForegroundControl()
    window_name = _one_line(_safe_property(window, "Name"))
    window_class = _one_line(_safe_property(window, "ClassName"))
    is_edge = "edge" in window_name.lower() or "chrome_widgetwin" in window_class.lower()
    if not is_edge:
        raise RuntimeError(
            "当前前台窗口不是 Microsoft Edge；请先将自动化 Edge 窗口置于前台后重试。"
        )

    records = [
        _control_record(control, depth, automation)
        for control, depth in _iter_tree(window, max_nodes)
    ]
    truncated = len(records) >= max_nodes

    actions = {
        target: [record for record in records if _matches_target(record, target)]
        for target in TARGET_ACTION_NAMES
    }
    question_titles = [
        record for record in records
        if (
            _is_visible(record)
            and record["type"] in QUESTION_CONTROL_TYPES
            and 6 <= len(record["name"]) <= 160
            and ("？" in record["name"] or "?" in record["name"])
        )
    ]
    hot_labels = [
        record for record in records
        if any(keyword in record["name"] for keyword in HOT_LABEL_KEYWORDS)
    ]
    interactions = [
        record for record in records
        if any(
            keyword in f"{record['name']} {record['value']}"
            for keyword in INTERACTION_KEYWORDS
        )
    ]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(log_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"a11y_probe_{stamp}.txt"

    lines = [
        "AutoQuill UI Automation read-only probe",
        f"time: {datetime.now().isoformat(timespec='seconds')}",
        f"source_url: {source_url or '(current foreground Edge page)'}",
        f"window_name: {window_name}",
        f"window_class: {window_class}",
        f"window_rect: {_rectangle(window)}",
        f"node_count: {len(records)}",
        f"truncated_at_{max_nodes}: {truncated}",
        "",
        "[FOCUS SUMMARY]",
        "answer action controls:",
    ]
    for target, matches in actions.items():
        lines.append(f"  {target}: {'FOUND' if matches else 'NOT_FOUND'}")
        for record in matches:
            lines.append(f"    {_format_focus_record(record)}")

    lines.append("question title candidates:")
    if question_titles:
        lines.extend(f"  {_format_focus_record(record)}" for record in question_titles)
    else:
        lines.append("  NOT_FOUND")

    lines.append("hot label candidates:")
    if hot_labels:
        lines.extend(f"  {_format_focus_record(record)}" for record in hot_labels)
    else:
        lines.append("  NOT_FOUND")

    lines.append("interaction text / values (raw):")
    if interactions:
        lines.extend(f"  {_format_focus_record(record)}" for record in interactions)
    else:
        lines.append("  NOT_FOUND")

    lines.extend(("", "[FULL UIA TREE]"))
    lines.extend(_format_record(record) for record in records)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "path": str(output_path),
        "node_count": len(records),
        "truncated": truncated,
        "actions": {target: len(matches) for target, matches in actions.items()},
        "question_titles": len(question_titles),
        "interactions": len(interactions),
    }


def _read_json_field(line: str, field: str) -> str:
    marker = f"{field}="
    start = line.find(marker)
    if start < 0:
        return ""
    try:
        value, _ = json.JSONDecoder().raw_decode(line[start + len(marker):])
    except (ValueError, json.JSONDecodeError):
        return ""
    return _one_line(value)


def _parse_tree_record(line: str) -> dict[str, Any] | None:
    """Parse one line from this module's stable [FULL UIA TREE] report format."""
    stripped = line.lstrip(" ")
    if not stripped.startswith("["):
        return None
    type_end = stripped.find("]")
    if type_end <= 1:
        return None

    rect_match = re.search(
        r"\brect=\((-?\d+), (-?\d+), (-?\d+), (-?\d+)\)$",
        stripped,
    )
    if not rect_match:
        return None

    return {
        "depth": (len(line) - len(stripped)) // 2,
        "type": stripped[1:type_end],
        "name": _read_json_field(stripped, "name"),
        "value": _read_json_field(stripped, "value"),
        "automation_id": _read_json_field(stripped, "automation_id"),
        "class_name": _read_json_field(stripped, "class"),
        "rect": tuple(int(value) for value in rect_match.groups()),
    }


def _load_report_web_records(report_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep only descendants of Edge's RootWebArea, excluding browser chrome."""
    lines = Path(report_path).read_text(encoding="utf-8").splitlines()
    try:
        tree_start = lines.index("[FULL UIA TREE]") + 1
    except ValueError as exc:
        raise ValueError("报告中缺少 [FULL UIA TREE] 区段") from exc

    records = [
        record
        for line in lines[tree_start:]
        if (record := _parse_tree_record(line)) is not None
    ]
    root_index = next(
        (
            index for index, record in enumerate(records)
            if record["type"] == "DocumentControl"
            and record["automation_id"] == "RootWebArea"
        ),
        None,
    )
    if root_index is None:
        raise ValueError("报告中未找到网页 RootWebArea")

    root = records[root_index]
    descendants = []
    for record in records[root_index + 1:]:
        if record["depth"] <= root["depth"]:
            break
        descendants.append(record)
    return root, descendants


def _rect_center_y(rect: tuple[int, int, int, int]) -> float:
    return (rect[1] + rect[3]) / 2


def _subtree_end(records: list[dict[str, Any]], start_index: int) -> int:
    """Return the exclusive end index of one indentation-based report subtree."""
    owner_depth = records[start_index]["depth"]
    for index in range(start_index + 1, len(records)):
        if records[index]["depth"] <= owner_depth:
            return index
    return len(records)


def _subtree_records(
    records: list[dict[str, Any]], start_index: int
) -> list[dict[str, Any]]:
    return records[start_index + 1:_subtree_end(records, start_index)]


def _is_in_viewport(record: dict[str, Any], viewport: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = record["rect"]
    if right <= left or bottom <= top:
        return False
    center_y = _rect_center_y(record["rect"])
    center_x = (left + right) / 2
    return viewport[0] <= center_x <= viewport[2] and viewport[1] <= center_y <= viewport[3]


def _normalize_ui_text(text: str) -> str:
    return " ".join(text.replace("\u200b", "").replace("\ufeff", "").split())


def _extract_button_count(
    records: list[dict[str, Any]], button_index: int
) -> int | None:
    """Get a count from a button's own name or from its visible text child."""
    button_name = _normalize_ui_text(records[button_index]["name"])
    direct_match = re.search(r"(\d+(?:\.\d+)?\s*[万亿]?)", button_name)
    if direct_match:
        return _parse_metric_number(direct_match.group(1))

    for record in _subtree_records(records, button_index):
        if record["type"] != "TextControl":
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)?\s*[万亿]?)", _normalize_ui_text(record["name"]))
        if match:
            return _parse_metric_number(match.group(1))
    return None


def _clean_question_title(title: str) -> str:
    title = _normalize_ui_text(title)
    for label in HOT_LABEL_KEYWORDS:
        if title.startswith(label):
            return title[len(label):].strip()
    return title


def _extract_question_labels(title: str) -> list[str]:
    normalized = _normalize_ui_text(title)
    return [label for label in HOT_LABEL_KEYWORDS if normalized.startswith(label)]


def _parse_metric_number(value: str) -> int | None:
    value = _normalize_ui_text(value).replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿]?)", value)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "万": 10000, "亿": 100000000}[match.group(2)]
    return int(number * multiplier)


def _parse_recommend_metrics(text: str) -> dict[str, Any] | None:
    raw = _normalize_ui_text(text)
    match = RECOMMEND_METRICS_RE.fullmatch(raw)
    if not match:
        return None
    values = {
        field: _parse_metric_number(match.group(field))
        for field in ("views", "answers", "followers")
    }
    if any(value is None for value in values.values()):
        return None
    return {
        "views": values["views"],
        "answers": values["answers"],
        "followers": values["followers"],
        "asked_at": match.group("asked_at"),
    }


def summarize_recommendation_report(
    report_path: str | Path,
    output_dir: str | Path = "logs",
    row_tolerance: int = 45,
) -> dict[str, Any]:
    """Project a raw recommendation-page UIA report into visible question cards.

    This is intentionally offline: it parses an existing probe report and never
    reads the live browser or invokes any UI element.
    """
    report_path = Path(report_path)
    root, web_records = _load_report_web_records(report_path)
    viewport = root["rect"]

    state_records = [
        record for record in web_records
        if (
            record["type"] == "TextControl"
            and _normalize_ui_text(record["name"]) in ACTION_STATE_NAMES
            and _is_in_viewport(record, viewport)
        )
    ]
    metric_records = [
        {**record, "metrics": metrics}
        for record in web_records
        if (
            record["type"] == "TextControl"
            and _is_in_viewport(record, viewport)
            and (metrics := _parse_recommend_metrics(record["name"])) is not None
        )
    ]
    question_links = [
        record for record in web_records
        if (
            record["type"] == "HyperlinkControl"
            and QUESTION_URL_RE.match(record["value"])
            and _is_in_viewport(record, viewport)
        )
    ]

    cards = []
    seen_urls = set()
    for link in question_links:
        url = link["value"].rstrip("/")
        if url in seen_urls:
            continue
        seen_urls.add(url)

        closest_state = min(
            state_records,
            key=lambda state: abs(
                _rect_center_y(state["rect"]) - _rect_center_y(link["rect"])
            ),
            default=None,
        )
        if (
            closest_state is not None
            and abs(
                _rect_center_y(closest_state["rect"])
                - _rect_center_y(link["rect"])
            ) <= row_tolerance
        ):
            state_name = _normalize_ui_text(closest_state["name"])
            state = ACTION_STATE_NAMES[state_name]
            state_rect = closest_state["rect"]
        else:
            state_name = ""
            state = "unknown"
            state_rect = None

        closest_metrics = min(
            metric_records,
            key=lambda metric: abs(
                _rect_center_y(metric["rect"]) - _rect_center_y(link["rect"])
            ),
            default=None,
        )
        if (
            closest_metrics is not None
            and abs(
                _rect_center_y(closest_metrics["rect"])
                - _rect_center_y(link["rect"])
            ) <= row_tolerance + 20
        ):
            metrics = closest_metrics["metrics"]
            metrics_rect = closest_metrics["rect"]
        else:
            metrics = None
            metrics_rect = None

        labels = _extract_question_labels(link["name"])

        cards.append({
            "title": _clean_question_title(link["name"]),
            "raw_title": _normalize_ui_text(link["name"]),
            "url": url,
            "labels": labels,
            "is_hot": bool(labels),
            "state": state,
            "state_label": state_name,
            "title_rect": link["rect"],
            "state_rect": state_rect,
            "metrics": metrics,
            "metrics_rect": metrics_rect,
        })

    cards.sort(key=lambda card: (card["title_rect"][1], card["title_rect"][0]))
    summary = {
        "source_report": str(report_path),
        "scope": "viewport",
        "viewport_rule": (
            "仅保留标题中心位于 RootWebArea 可见矩形内的知乎问题 "
            "HyperlinkControl；该链接可作为当前屏正常点击目标。"
        ),
        "viewport": viewport,
        "web_node_count": len(web_records),
        "visible_card_count": len(cards),
        "visible_actionable_card_count": len(cards),
        "hot_count": sum(card["is_hot"] for card in cards),
        "new_count": sum(card["state"] == "new" for card in cards),
        "drafted_count": sum(card["state"] == "drafted" for card in cards),
        "unknown_count": sum(card["state"] == "unknown" for card in cards),
        "metrics_detected_count": sum(card["metrics"] is not None for card in cards),
        "cards": cards,
    }

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp_match = re.search(r"(\d{8}_\d{6})", report_path.stem)
    stamp = stamp_match.group(1) if stamp_match else datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"a11y_recommend_summary_{stamp}.json"
    text_path = output_dir / f"a11y_recommend_summary_{stamp}.txt"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "AutoQuill UIA recommendation semantic summary",
        f"source_report: {report_path}",
        f"scope: {summary['scope']}",
        f"viewport_rule: {summary['viewport_rule']}",
        f"viewport: {viewport}",
        f"web_node_count: {summary['web_node_count']}",
        f"visible_actionable_cards: {summary['visible_actionable_card_count']}",
        f"hot: {summary['hot_count']}",
        f"new: {summary['new_count']}",
        f"drafted: {summary['drafted_count']}",
        f"unknown: {summary['unknown_count']}",
        f"metrics_detected: {summary['metrics_detected_count']}",
        "",
        "[CARDS]",
    ]
    for index, card in enumerate(cards, start=1):
        lines.extend((
            f"{index:02d}. [{card['state']}] {card['title']}",
            f"    url: {card['url']}",
            f"    labels: {', '.join(card['labels']) or 'NONE'}",
            f"    title_rect: {card['title_rect']}",
            f"    state_label: {card['state_label'] or 'NOT_FOUND'}",
            f"    state_rect: {card['state_rect']}",
            f"    metrics: {card['metrics'] or 'NOT_FOUND'}",
            f"    metrics_rect: {card['metrics_rect']}",
        ))
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary["json_path"] = str(json_path)
    summary["text_path"] = str(text_path)
    return summary


def _find_answer_item_indexes(records: list[dict[str, Any]]) -> list[int]:
    indexes = []
    for index, record in enumerate(records):
        if record["type"] != "GroupControl":
            continue
        class_name = record["class_name"]
        is_list_answer = class_name == "List-item"
        is_single_answer = "QuestionAnswer-content" in class_name
        if not (is_list_answer or is_single_answer):
            continue
        subtree = _subtree_records(records, index)
        if any(
            "AnswerItem-authorInfo" in child["class_name"]
            for child in subtree
        ) and any(
            child["automation_id"] == "content" for child in subtree
        ):
            indexes.append(index)
    return indexes


def _join_answer_body(records: list[dict[str, Any]], content_index: int) -> tuple[str, int]:
    """Rebuild answer text from leaf text controls in one RichContent subtree."""
    content_end = _subtree_end(records, content_index)
    text_nodes = []
    for index in range(content_index + 1, content_end):
        record = records[index]
        if record["type"] != "TextControl":
            continue
        text = _normalize_ui_text(record["name"])
        # The content root covers the whole answer, including offscreen text.
        # Only reject empty nodes or invalid geometry, not document text below viewport.
        left, top, right, bottom = record["rect"]
        if not text or right <= left or bottom <= top:
            continue
        if index + 1 < content_end and records[index + 1]["depth"] > record["depth"]:
            continue
        text_nodes.append(record)

    body_parts = []
    previous = None
    for record in text_nodes:
        if previous is not None:
            same_line = abs(record["rect"][1] - previous["rect"][1]) <= 6
            if not same_line:
                body_parts.append("\n")
        body_parts.append(_normalize_ui_text(record["name"]))
        previous = record
    return "".join(body_parts).strip(), len(text_nodes)


def _extract_answer_summary(
    records: list[dict[str, Any]],
    answer_index: int,
    viewport: tuple[int, int, int, int],
) -> dict[str, Any]:
    answer_end = _subtree_end(records, answer_index)
    answer_records = records[answer_index:answer_end]
    answer_offset = answer_index

    def find_first(predicate):
        for offset, record in enumerate(answer_records):
            if predicate(record):
                return answer_offset + offset, record
        return None, None

    author_index, author_record = find_first(
        lambda record: (
            record["type"] == "HyperlinkControl"
            and "UserLink-link" in record["class_name"]
            and bool(_normalize_ui_text(record["name"]))
        )
    )
    content_index, _ = find_first(lambda record: record["automation_id"] == "content")
    published_index, published_record = find_first(
        lambda record: (
            record["type"] == "HyperlinkControl"
            and "/answer/" in record["value"]
            and _normalize_ui_text(record["name"]).startswith("发布于")
        )
    )
    _, edited_record = find_first(
        lambda record: _normalize_ui_text(record["name"]).startswith("编辑于")
    )

    interactions = {"likes": None, "comments": None, "collects": None, "hearts": None}
    interaction_rect = None
    for index in range(answer_index, answer_end):
        record = records[index]
        if record["type"] != "ButtonControl" or not _is_in_viewport(record, viewport):
            continue
        name = _normalize_ui_text(record["name"])
        class_name = record["class_name"]
        if "VoteButton" in class_name and "VoteButton--down" not in class_name and name.startswith("赞同"):
            interactions["likes"] = _extract_button_count(records, index)
            interaction_rect = record["rect"]
        elif re.search(r"\d+(?:\.\d+)?\s*[万亿]?\s*条评论$", name):
            interactions["comments"] = _extract_button_count(records, index)
        elif name == "收藏":
            interactions["collects"] = _extract_button_count(records, index)
        elif name == "喜欢":
            interactions["hearts"] = _extract_button_count(records, index)

    body, body_text_node_count = (
        _join_answer_body(records, content_index)
        if content_index is not None else ("", 0)
    )
    return {
        "author": _normalize_ui_text(author_record["name"]) if author_record else "",
        "author_url": author_record["value"] if author_record else "",
        "answer_url": published_record["value"] if published_record else "",
        "published_at": (
            _normalize_ui_text(published_record["name"]).removeprefix("发布于")
            if published_record else ""
        ),
        "edited_at": (
            _normalize_ui_text(edited_record["name"]).removeprefix("编辑于")
            if edited_record else ""
        ),
        "body": body,
        "body_char_count": len(body),
        "body_text_node_count": body_text_node_count,
        "interactions": interactions,
        "interaction_rect": interaction_rect,
        "answer_rect": records[answer_index]["rect"],
    }


def summarize_answer_report(
    report_path: str | Path,
    output_dir: str | Path = "logs",
) -> dict[str, Any]:
    """Project a raw question-page report into question and answer-level data."""
    report_path = Path(report_path)
    root, web_records = _load_report_web_records(report_path)
    viewport = root["rect"]

    titles = [
        record for record in web_records
        if (
            record["type"] == "TextControl"
            and "QuestionHeader-title" in record["class_name"]
            and _normalize_ui_text(record["name"])
            and _is_in_viewport(record, viewport)
        )
    ]
    question_title = _normalize_ui_text(titles[0]["name"]) if titles else ""
    topics = []
    for record in web_records:
        if (
            record["type"] == "HyperlinkControl"
            and "TopicLink" in record["class_name"]
            and _is_in_viewport(record, viewport)
        ):
            topic = _normalize_ui_text(record["name"])
            if topic and topic not in topics:
                topics.append(topic)

    answer_action_controls = [
        record for record in web_records
        if (
            record["type"] == "ButtonControl"
            and _is_in_viewport(record, viewport)
            and _normalize_ui_text(record["name"]) in ANSWER_ACTION_NAMES
        )
    ]
    action_labels = [_normalize_ui_text(record["name"]) for record in answer_action_controls]
    answer_action_label = (
        "编辑回答" if "编辑回答" in action_labels
        else "写回答" if "写回答" in action_labels else ""
    )
    answer_action = ANSWER_ACTION_NAMES.get(answer_action_label, "unknown")
    answer_action_rect = (
        next(
            (record["rect"] for record in answer_action_controls
             if _normalize_ui_text(record["name"]) == answer_action_label),
            None,
        )
    )

    answer_indexes = _find_answer_item_indexes(web_records)
    answer_indexes.sort(key=lambda index: web_records[index]["rect"][1])
    answers = [
        _extract_answer_summary(web_records, index, viewport)
        for index in answer_indexes
    ]
    primary_answer = answers[0] if answers else None

    summary = {
        "source_report": str(report_path),
        "question": {
            "url": root["value"],
            "title": question_title,
            "topics": topics,
            "answer_action": answer_action,
            "answer_action_label": answer_action_label,
            "answer_action_rect": answer_action_rect,
        },
        "interaction_scope": "viewport",
        "interaction_viewport_rule": (
            "仅读取位于 RootWebArea 当前可见矩形内的回答操作按钮。"
        ),
        "body_scope": "document",
        "body_document_rule": (
            "锁定首条回答的 content 子树后，读取其完整正文，允许正文跨出当前屏幕。"
        ),
        "viewport": viewport,
        "web_node_count": len(web_records),
        "loaded_answer_count": len(answers),
        "primary_answer": primary_answer,
        "loaded_answer_overview": [
            {
                key: answer[key]
                for key in (
                    "author", "answer_url", "published_at", "edited_at",
                    "body_char_count", "interactions", "answer_rect",
                )
            }
            for answer in answers
        ],
    }

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp_match = re.search(r"(\d{8}_\d{6})", report_path.stem)
    stamp = stamp_match.group(1) if stamp_match else datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"a11y_answer_summary_{stamp}.json"
    text_path = output_dir / f"a11y_answer_summary_{stamp}.txt"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "AutoQuill UIA answer semantic summary",
        f"source_report: {report_path}",
        f"question_url: {summary['question']['url']}",
        f"question_title: {question_title}",
        f"topics: {', '.join(topics) or 'NONE'}",
        f"answer_action: {answer_action} ({answer_action_label or 'NOT_FOUND'})",
        f"answer_action_rect: {answer_action_rect}",
        f"interaction_scope: {summary['interaction_scope']}",
        f"interaction_viewport_rule: {summary['interaction_viewport_rule']}",
        f"body_scope: {summary['body_scope']}",
        f"body_document_rule: {summary['body_document_rule']}",
        f"loaded_answers: {len(answers)}",
        "",
    ]
    if primary_answer is None:
        lines.append("[PRIMARY ANSWER]\nNOT_FOUND")
    else:
        lines.extend((
            "[PRIMARY ANSWER]",
            f"author: {primary_answer['author'] or 'NOT_FOUND'}",
            f"author_url: {primary_answer['author_url']}",
            f"answer_url: {primary_answer['answer_url']}",
            f"published_at: {primary_answer['published_at']}",
            f"edited_at: {primary_answer['edited_at']}",
            f"interactions: {primary_answer['interactions']}",
            f"interaction_rect: {primary_answer['interaction_rect']}",
            f"body_char_count: {primary_answer['body_char_count']}",
            f"body_text_node_count: {primary_answer['body_text_node_count']}",
            "",
            "[PRIMARY ANSWER BODY]",
            primary_answer["body"],
            "",
        ))
    lines.append("[LOADED ANSWER OVERVIEW]")
    for index, answer in enumerate(summary["loaded_answer_overview"], start=1):
        lines.append(
            f"{index:02d}. likes={answer['interactions']['likes']} "
            f"comments={answer['interactions']['comments']} "
            f"collects={answer['interactions']['collects']} "
            f"body_chars={answer['body_char_count']} "
            f"url={answer['answer_url']}"
        )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary["json_path"] = str(json_path)
    summary["text_path"] = str(text_path)
    return summary


def _read_live_web_records() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read the foreground Edge RootWebArea without invoking any UI control."""
    try:
        import uiautomation as automation
    except ImportError as exc:
        raise RuntimeError(f"uiautomation 不可用：{exc}") from exc

    window = automation.GetForegroundControl()
    window_name = _one_line(_safe_property(window, "Name"))
    window_class = _one_line(_safe_property(window, "ClassName"))
    if "edge" not in window_name.lower() and "chrome_widgetwin" not in window_class.lower():
        raise RuntimeError("当前前台窗口不是 Microsoft Edge")

    root_control = None
    for control, _ in _iter_tree(window, MAX_TREE_NODES):
        if _safe_property(control, "AutomationId") == "RootWebArea":
            root_control = control
            break
    if root_control is None:
        raise RuntimeError("当前 Edge 页面尚未暴露 RootWebArea")

    records = [
        _control_record(control, depth, automation)
        for control, depth in _iter_tree(root_control, MAX_TREE_NODES)
    ]
    if not records:
        raise RuntimeError("RootWebArea 为空")
    return records[0], records[1:]


def _answer_has_expand_marker(records: list[dict[str, Any]], answer_index: int) -> bool:
    for record in _subtree_records(records, answer_index):
        if "展开阅读全文" in _normalize_ui_text(record["name"]):
            return True
    return False


def extract_live_primary_answer(
    min_length: int = 500,
    wait_timeout: float = 4.0,
    poll_interval: float = 0.25,
) -> tuple[str, str, dict[str, Any] | None, str]:
    """Read the rendered first answer through UIA, returning OCR-compatible data.

    The function only reads the local accessibility tree. The caller decides
    whether to use the returned result or fall back to OCR.
    """
    try:
        import uiautomation  # noqa: F401 - verify the optional live dependency first
    except ImportError as exc:
        return "", "", None, f"uiautomation 不可用：{exc}"

    deadline = time.time() + max(0.0, wait_timeout)
    last_reason = "首答 UIA 内容尚未出现"

    while True:
        try:
            root, web_records = _read_live_web_records()
            viewport = root["rect"]
            titles = [
                record for record in web_records
                if (
                    record["type"] == "TextControl"
                    and "QuestionHeader-title" in record["class_name"]
                    and _normalize_ui_text(record["name"])
                    and _is_in_viewport(record, viewport)
                )
            ]
            title = _normalize_ui_text(titles[0]["name"]) if titles else ""

            answer_indexes = _find_answer_item_indexes(web_records)
            answer_indexes.sort(key=lambda index: web_records[index]["rect"][1])
            if not title or not answer_indexes:
                last_reason = "未找到问题标题或首答容器"
            else:
                answer_index = answer_indexes[0]
                primary = _extract_answer_summary(
                    web_records, answer_index, viewport
                )
                if _answer_has_expand_marker(web_records, answer_index):
                    last_reason = "首答仍显示展开阅读全文，正文可能截断"
                elif primary["body_char_count"] < min_length:
                    last_reason = (
                        f"首答正文过短：{primary['body_char_count']} < {min_length}"
                    )
                else:
                    interactions = primary["interactions"]
                    footer = {
                        "likes": interactions["likes"],
                        "comments": interactions["comments"],
                        "collects": interactions["collects"],
                        "hearts": interactions["hearts"],
                        "publish_time": primary["published_at"] or None,
                        "edit_time": primary["edited_at"] or None,
                        "answer_url": primary["answer_url"],
                        "likes_source": "uia_vote_button",
                    }
                    return title, primary["body"], footer, ""
        except Exception as exc:
            last_reason = f"UIA 读取异常：{exc}"

        if time.time() >= deadline:
            return "", "", None, last_reason
        time.sleep(max(0.05, poll_interval))
