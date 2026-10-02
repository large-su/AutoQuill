"""Build the offline, bundled release evolution manifest."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = WRITING_FILES = {
    "story_prompt.py", "story_generation.py", "config/story.py",
    "applications/zhihu_story/prompts.py", "workflows/workflow_generation.py",
}
TAG_RE = re.compile(r"^v(\d+(?:\.\d+){1,3})$")


def _git(root, *args):
    p = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                       text=True, encoding="utf-8", check=True)
    return p.stdout


def _version_key(tag):
    return tuple(int(x) for x in TAG_RE.match(tag).group(1).split("."))


def _tags(root):
    tags = [x.strip() for x in _git(root, "tag", "--merged", "HEAD", "--list", "v*").splitlines()]
    return sorted((t for t in tags if TAG_RE.match(t)), key=_version_key)


def _files(root, old, tag):
    return [x for x in _git(root, "diff", "--name-only", old, tag).splitlines() if x]


def _fingerprint(root, tag):
    h = hashlib.sha256()
    for path in sorted(WRITING_FILES):
        try:
            data = subprocess.run(["git", "show", f"{tag}:{path}"], cwd=str(root),
                                  capture_output=True, check=True).stdout
            h.update(path.encode()); h.update(b"\0"); h.update(data.replace(b"\r\n", b"\n")); h.update(b"\0")
        except subprocess.CalledProcessError:
            h.update(path.encode()); h.update(b"\0<missing>\0")
    return h.hexdigest()


def current_writing_code_id(root=ROOT):
    """Fingerprint the checked-out source files (working tree helper)."""
    h = hashlib.sha256()
    for path in sorted(SOURCE_FILES):
        fp = Path(root) / path
        data = fp.read_bytes() if fp.is_file() else b"<missing>"
        h.update(path.encode()); h.update(b"\0"); h.update(data.replace(b"\r\n", b"\n")); h.update(b"\0")
    return h.hexdigest()


def refresh_history(root=ROOT, output=None, current_version=None, pending_summary=None):
    """Generate the manifest using only local Git and source files."""
    root = Path(root)
    target = Path(output) if output else root / "core" / "evolution_history.json"
    all_tags = _tags(root)

    # Keep the first published tag in an existing manifest as the durable
    # starting point.  A pending working-tree row must never become that
    # point, since it disappears as soon as the release is tagged.
    existing = None
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"历史文件损坏，无法读取: {target}") from exc
        if not isinstance(existing, dict) or not isinstance(existing.get("releases"), list):
            raise ValueError(f"历史文件损坏，缺少有效 releases: {target}")

    baseline = existing.get("baseline_tag") if existing else None
    if baseline is None and existing:
        for row in existing["releases"]:
            if (isinstance(row, dict) and row.get("provenance") != "working-tree"
                    and row.get("tag")):
                baseline = row["tag"]
                break
    if baseline is None:
        # Initial manifests intentionally retain only the latest window.  The
        # selected first tag then becomes the permanent baseline on disk.
        tags = all_tags[-16:]
        baseline = tags[0] if tags else None
    elif baseline not in all_tags:
        raise ValueError(f"历史基线不可达，拒绝覆盖原文件: {baseline}")
    tags = all_tags[all_tags.index(baseline):] if baseline else []
    releases = []
    baseline_index = all_tags.index(baseline) if baseline else 0
    previous = all_tags[baseline_index - 1] if baseline_index else None
    previous_id = _git(root, "rev-list", "-n", "1", previous).strip() if previous else None
    for tag in tags:
        commit = _git(root, "rev-list", "-n", "1", tag).strip()
        release_date = _git(root, "show", "-s", "--format=%ad", "--date=short", tag).strip()
        base = previous or _git(root, "rev-list", "--max-parents=0", tag).strip()
        subjects = [x for x in _git(root, "log", "--format=%s", f"{base}..{tag}").splitlines() if x]
        paths = _files(root, base, tag)
        writing_changed = sorted(set(paths) & WRITING_FILES)
        kind = "writing" if writing_changed and all(p in WRITING_FILES for p in paths) else ("mixed" if writing_changed else "engineering")
        substantive = [s for s in subjects if not re.match(r"(?:chore|build):?\s*(?:同步|sync).*版本|版本号", s, re.I)]
        summary = "；".join((substantive or subjects or ["release"])[:3])
        releases.append({"version": tag.removeprefix("v"), "tag": tag,
                         "commit": commit, "source_commit": commit, "parent": previous_id,
                         "date": release_date, "summary": summary, "summaries": subjects[:12],
                         "changed_file_count": len(paths), "changed_files": paths[:40],
                         "change_kind": kind, "writing_files_changed": writing_changed,
                         "writing_code_id": _fingerprint(root, tag), "provenance": "git-tag"})
        previous, previous_id = tag, commit
    try:
        if current_version is None:
            if root.resolve() != ROOT.resolve():
                raise ImportError
            from core.version import VERSION
            current = str(VERSION)
        else:
            current = str(current_version)
        if tags and _version_key("v" + current) > _version_key(tags[-1]):
            pending_paths = sorted(set(filter(None,
                _git(root, "diff", "--name-only", tags[-1]).splitlines()
                + _git(root, "ls-files", "--others", "--exclude-standard").splitlines())))
            writing_pending = sorted(set(pending_paths) & WRITING_FILES)
            pending_kind = "writing" if writing_pending and all(p in WRITING_FILES for p in pending_paths) else ("mixed" if writing_pending else "engineering")
            releases.append({"version": current, "tag": "v" + current, "commit": None,
                             "source_commit": None, "parent": previous_id,
                             "date": date.today().isoformat(), "summary": pending_summary or "待发布版本",
                             "summaries": [pending_summary or "待发布版本"],
                             "changed_file_count": len(pending_paths), "changed_files": pending_paths[:40], "change_kind": pending_kind,
                             "writing_files_changed": writing_pending, "writing_code_id": current_writing_code_id(root),
                             "provenance": "working-tree", "pending": True})
    except (ImportError, ValueError):
        pass
    manifest = {"schema_version": 1, "baseline_tag": baseline, "releases": releases}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    refresh_history()
