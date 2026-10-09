"""Local study catalog and learning ledger for Zhihu paid columns."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.paths import DATA_ROOT

DB_RELATIVE = Path("data") / "meta" / "story_studies.sqlite3"
URL_RE = re.compile(r"^/market/paid_column/(\d+)/section/(\d+)/?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEMA = 1
CATALOG_KEYS = {"title", "url", "author", "labels", "likes", "comments", "description"}
RECORD_KEYS = {"url", "title", "read_date", "scope", "coverage", "lessons", "batch_id", "author", "applied_version"}
SOURCE_COLUMNS = {"id", "url", "title", "author", "created_at", "updated_at"}
STUDY_COLUMNS = {
    "id", "source_id", "url", "title", "author", "read_date", "scope", "coverage",
    "lessons", "batch_id", "applied_version", "payload", "payload_sha256", "created_at",
}

CREATE_SOURCES_SQL = """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        author TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
"""
CREATE_STUDIES_SQL = """
    CREATE TABLE studies (
        id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
        url TEXT NOT NULL, title TEXT NOT NULL, author TEXT, read_date TEXT NOT NULL,
        scope TEXT NOT NULL, coverage TEXT NOT NULL, lessons TEXT NOT NULL,
        batch_id TEXT NOT NULL, applied_version TEXT, payload TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(source_id, payload_sha256)
    )
"""
UPSERT_SOURCE_SQL = """
    INSERT INTO sources(url, title, author, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(url) DO UPDATE SET
        title = excluded.title,
        author = COALESCE(excluded.author, sources.author),
        updated_at = excluded.updated_at
"""
INSERT_STUDY_SQL = """
    INSERT OR IGNORE INTO studies(
        source_id, url, title, author, read_date, scope, coverage, lessons,
        batch_id, applied_version, payload, payload_sha256, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def db_path(data_root=DATA_ROOT) -> Path:
    return Path(data_root).expanduser().resolve() / DB_RELATIVE


def canonical_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("url must be a non-empty string")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid url") from exc
    if parsed.scheme != "https" or parsed.hostname != "www.zhihu.com" or parsed.username or parsed.password or port:
        raise ValueError("url must be an https://www.zhihu.com paid-column URL")
    match = URL_RE.fullmatch(parsed.path)
    if not match:
        raise ValueError("url must match https://www.zhihu.com/market/paid_column/<number>/section/<number>")
    path = f"/market/paid_column/{match.group(1)}/section/{match.group(2)}"
    return urlunsplit(("https", "www.zhihu.com", path, "", ""))


def _text(obj, key, limit=None, required=False):
    value = obj.get(key)
    if required and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{key} must be a non-empty string")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{key} must not be empty")
    if limit and isinstance(value, str) and len(value) > limit:
        raise ValueError(f"{key} exceeds {limit} characters")
    return value.strip() if isinstance(value, str) else None


def _load_array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, (str, Path)):
        if isinstance(value, str) and value.lstrip().startswith("["):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("input must be a JSON array") from exc
        else:
            try:
                with open(value, encoding="utf-8") as handle:
                    value = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("input must be a JSON array") from exc
    if not isinstance(value, list):
        raise ValueError("input must be a JSON array")
    return value


def validate_catalog(rows):
    result = []
    for row in _load_array(rows):
        if not isinstance(row, dict):
            raise ValueError("each catalog item must be an object")
        if not set(row) <= CATALOG_KEYS:
            raise ValueError("catalog contains unknown fields")
        result.append({
            "url": canonical_url(row.get("url")),
            "title": _text(row, "title", 160, True),
            "author": _text(row, "author", 160),
        })
    return result


def validate_records(rows):
    result = []
    for row in _load_array(rows):
        if not isinstance(row, dict):
            raise ValueError("each record item must be an object")
        unknown = set(row) - RECORD_KEYS
        if unknown:
            raise ValueError("record contains unknown fields: " + ", ".join(sorted(unknown)))
        read_date = _text(row, "read_date", required=True)
        try:
            if not DATE_RE.fullmatch(read_date):
                raise ValueError
            date.fromisoformat(read_date)
        except ValueError as exc:
            raise ValueError("read_date must be YYYY-MM-DD") from exc
        scope = row.get("scope")
        if not isinstance(scope, str) or scope not in {"full", "partial", "opening"}:
            raise ValueError("scope must be full, partial, or opening")
        lessons = row.get("lessons")
        if not isinstance(lessons, list) or not 1 <= len(lessons) <= 6:
            raise ValueError("lessons must be an array containing 1 to 6 strings")
        clean_lessons = []
        for lesson in lessons:
            if not isinstance(lesson, str) or not lesson.strip() or len(lesson.strip()) > 500:
                raise ValueError("each lesson must be a non-empty string of at most 500 characters")
            clean_lessons.append(lesson.strip())
        result.append({
            "url": canonical_url(row.get("url")), "title": _text(row, "title", 160, True),
            "read_date": read_date, "scope": scope, "coverage": _text(row, "coverage", 240, True),
            "lessons": clean_lessons, "batch_id": _text(row, "batch_id", 80, True),
            "author": _text(row, "author", 160), "applied_version": _text(row, "applied_version", 30),
        })
    return result


def _has_unique_index(conn, table, columns):
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if index[2]:
            index_columns = [row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})")]
            if index_columns == columns:
                return True
    return False


def _validate_schema(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
        raise ValueError("unsupported story studies schema")
    for table, expected_columns in {"sources": SOURCE_COLUMNS, "studies": STUDY_COLUMNS}.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns != expected_columns:
            raise ValueError("invalid story studies schema")
    source_unique = _has_unique_index(conn, "sources", ["url"])
    study_unique = _has_unique_index(conn, "studies", ["source_id", "payload_sha256"])
    if not source_unique or not study_unique:
        raise ValueError("invalid story studies schema")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_studies_hash'").fetchone():
        raise ValueError("invalid story studies schema")


def _connect(path, readonly=False):
    if readonly and not path.is_file():
        return None
    conn = None
    try:
        database = path.as_uri() + "?mode=ro" if readonly else str(path)
        conn = sqlite3.connect(database, uri=readonly)
        conn.row_factory = sqlite3.Row
        _validate_schema(conn)
        return conn
    except ValueError:
        if conn is not None:
            conn.close()
        raise
    except (sqlite3.DatabaseError, OSError) as exc:
        if conn is not None:
            conn.close()
        raise ValueError(f"invalid story studies database: {path}") from exc


def _init_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        if has_tables:
            _validate_schema(conn)
            conn.commit()
            return conn
        if version != 0:
            raise ValueError("unsupported story studies schema")
        conn.execute(f"PRAGMA user_version={SCHEMA}")
        conn.execute(CREATE_SOURCES_SQL)
        conn.execute(CREATE_STUDIES_SQL)
        conn.execute("CREATE INDEX idx_studies_hash ON studies(payload_sha256)")
        conn.commit()
        return conn
    except ValueError:
        if conn is not None:
            conn.rollback()
            conn.close()
        raise
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            conn.rollback()
            conn.close()
        raise ValueError(f"invalid story studies database: {path}") from exc


def _upsert_source(conn, item, now):
    conn.execute(UPSERT_SOURCE_SQL, (item["url"], item["title"], item["author"], now, now))
    return conn.execute("SELECT id FROM sources WHERE url = ?", (item["url"],)).fetchone()[0]


def catalog(rows, data_root=DATA_ROOT):
    items = validate_catalog(rows)
    path = db_path(data_root)
    conn = _init_db(path)
    now = date.today().isoformat()
    try:
        with conn:
            for item in items:
                _upsert_source(conn, item, now)
    finally:
        conn.close()
    return {"count": len(items), "path": str(path)}


def record(rows, data_root=DATA_ROOT):
    items = validate_records(rows)
    path = db_path(data_root)
    conn = _init_db(path)
    inserted = 0
    try:
        with conn:
            for item in items:
                now = date.today().isoformat()
                source_id = _upsert_source(conn, item, now)
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                values = (
                    source_id, item["url"], item["title"], item["author"], item["read_date"],
                    item["scope"], item["coverage"],
                    json.dumps(item["lessons"], ensure_ascii=False), item["batch_id"],
                    item["applied_version"], payload, digest, now,
                )
                inserted += conn.execute(INSERT_STUDY_SQL, values).rowcount
    finally:
        conn.close()
    return {"count": inserted, "path": str(path)}


def _status(conn, source_id):
    scopes = [row[0] for row in conn.execute("SELECT scope FROM studies WHERE source_id = ?", (source_id,))]
    if "full" in scopes:
        return "complete"
    return "partial" if scopes else "unread"


def listing(data_root=DATA_ROOT, status="all"):
    if not isinstance(status, str) or status not in {"all", "unread", "partial", "complete"}:
        raise ValueError("invalid status")
    path = db_path(data_root)
    conn = _connect(path, readonly=True)
    if conn is None:
        return {
            "schema_version": SCHEMA,
            "path": str(path),
            "counts": {"all": 0, "unread": 0, "partial": 0, "complete": 0},
            "sources": [],
        }
    try:
        sources = []
        for source in conn.execute("SELECT * FROM sources ORDER BY id"):
            source_id = source["id"]
            study_count = conn.execute("SELECT COUNT(*) FROM studies WHERE source_id = ?", (source_id,)).fetchone()[0]
            latest = conn.execute("SELECT MAX(read_date) FROM studies WHERE source_id = ?", (source_id,)).fetchone()[0]
            sources.append({
                "title": source["title"],
                "url": source["url"],
                "status": _status(conn, source_id),
                "study_count": study_count,
                "latest_read_date": latest,
            })
        counts = {
            kind: sum(
                1 for source in sources
                if kind == "all" or source["status"] == kind
            )
            for kind in ("all", "unread", "partial", "complete")
        }
        filtered = [source for source in sources if status == "all" or source["status"] == status]
        return {"schema_version": SCHEMA, "path": str(path), "counts": counts, "sources": filtered}
    finally:
        conn.close()


def history(url, data_root=DATA_ROOT):
    canonical = canonical_url(url)
    path = db_path(data_root)
    conn = _connect(path, readonly=True)
    if conn is None:
        return {"schema_version": SCHEMA, "path": str(path), "url": canonical, "studies": []}
    try:
        rows = conn.execute("""SELECT title, author, read_date, scope, coverage, lessons, batch_id, applied_version
                               FROM studies WHERE url = ? ORDER BY read_date, id""", (canonical,)).fetchall()
        studies = [{**dict(row), "lessons": json.loads(row["lessons"])} for row in rows]
        return {"schema_version": SCHEMA, "path": str(path), "url": canonical, "studies": studies}
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="local story study ledger")
    parser.add_argument("--data-root", default=DATA_ROOT)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("catalog", "record"):
        command = subcommands.add_parser(name)
        command.add_argument("--input", required=True)
    command = subcommands.add_parser("list")
    command.add_argument("--status", choices=("all", "unread", "partial", "complete"), default="all")
    command = subcommands.add_parser("history")
    command.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            result = catalog(args.input, args.data_root)
        elif args.command == "record":
            result = record(args.input, args.data_root)
        elif args.command == "list":
            result = listing(args.data_root, args.status)
        else:
            result = history(args.url, args.data_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.DatabaseError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
