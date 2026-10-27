"""Append-only, self-verifying published-content snapshots for offline analysis."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core import paths

TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
_NAME = re.compile(r"^snapshot-([0-9a-f]{64})\.json$")
_LEGACY_NAME = re.compile(r"^published_answers_(\d{4}-\d{2}-\d{2})\.json$")
_PRECISIONS = {'day', 'minute', 'second'}
_SOURCES = {'capture', 'legacy-import', 'state-import'}
_COMPLETENESS = {'complete', 'unknown'}


def _root(archive_dir=None):
    return Path(archive_dir) if archive_dir is not None else Path(paths.data('data', 'feedback_archive'))


def _time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=TZ)
        return stamp.astimezone(TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical({k: v for k, v in value.items() if k != 'sha256'}).encode('utf-8')).hexdigest()


def _normalized_time(value):
    stamp = _time(value)
    if stamp is None:
        raise ValueError('无效的采集时间')
    return stamp.isoformat()  # preserve non-zero microseconds exactly


def _valid_observation(value):
    return isinstance(value, dict) and isinstance(value.get('source'), str) and _time(value.get('observed_at')) is not None


def _validate(env, *, path=None):
    if not isinstance(env, dict) or env.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('不支持的归档格式')
    if env.get('precision') not in _PRECISIONS or env.get('source') not in _SOURCES:
        raise ValueError('归档精度或来源无效')
    if env.get('completeness') not in _COMPLETENESS:
        raise ValueError('归档完整性无效')
    stamp = _time(env.get('observed_at'))
    if stamp is None or env.get('observed_at') != stamp.isoformat():
        raise ValueError('归档采集时间无效或未规范化')
    rows = env.get('rows')
    if not isinstance(rows, list) or type(env.get('count')) is not int or env.get('count') != len(rows):
        raise ValueError('归档条目数不匹配')
    for row in rows:
        if (not isinstance(row, dict) or type(row.get('aid')) not in (str, int)
                or not str(row.get('aid') or '').strip()):
            raise ValueError('归档作品缺少有效 aid')
        if env['source'] == 'state-import' and not _valid_observation(row.get('_observation')):
            raise ValueError('状态迁移归档缺少有效原观测')
        if env['source'] == 'state-import' and _time(row['_observation']['observed_at']) != stamp:
            raise ValueError('状态迁移归档的原观测时间不一致')
    legacy = env.get('legacy_file')
    if legacy is not None and (not isinstance(legacy, str) or Path(legacy).name != legacy or not _LEGACY_NAME.fullmatch(legacy)):
        raise ValueError('旧镜像文件名无效')
    if env['source'] == 'legacy-import' and not legacy:
        raise ValueError('旧镜像归档缺少来源文件名')
    digest = env.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or digest != _digest(env):
        raise ValueError('归档校验和不匹配')
    if path is not None:
        match = _NAME.fullmatch(path.name)
        if not match or match.group(1) != digest:
            raise ValueError('归档文件名与校验和不匹配')
        if path.parent.name != stamp.date().isoformat():
            raise ValueError('归档目录日期与采集时间不匹配')
    return env


def _envelope(rows, observed_at, *, source, precision, completeness, legacy_file=None):
    if not isinstance(rows, list):
        raise ValueError('快照条目必须为数组')
    prepared = list(rows or [])
    env = {'schema_version': SCHEMA_VERSION, 'observed_at': _normalized_time(observed_at),
           'precision': str(precision), 'source': str(source), 'completeness': str(completeness),
           'count': len(prepared), 'rows': prepared}
    if legacy_file:
        env['legacy_file'] = Path(str(legacy_file)).name
    env['sha256'] = _digest(env)
    return _validate(env)


def _read(path):
    try:
        return _validate(json.loads(path.read_text(encoding='utf-8')), path=path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return '无效归档 %s：%s' % (path, exc)


def save(rows, observed_at, *, source='capture', precision='second', completeness='complete', legacy_file=None, archive_dir=None):
    """Append one verified envelope. Existing evidence is never overwritten."""
    env = _envelope(rows, observed_at, source=source, precision=precision,
                    completeness=completeness, legacy_file=legacy_file)
    directory = _root(archive_dir) / _time(env['observed_at']).date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('snapshot-' + env['sha256'] + '.json')

    def existing():
        item = _read(target)
        if isinstance(item, str) or _canonical(item) != _canonical(env):
            raise ValueError('同名归档已损坏或与当前内容不一致，未覆盖')
        return {'ok': True, 'path': str(target), 'sha256': env['sha256'], 'count': env['count'], 'idempotent': True}

    if target.exists():
        return existing()
    fd, name = tempfile.mkstemp(prefix='.snapshot-', suffix='.tmp', dir=str(directory))
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(env, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(target))
        except FileExistsError:
            return existing()
        return {'ok': True, 'path': str(target), 'sha256': env['sha256'], 'count': env['count'], 'idempotent': False}
    finally:
        try: temporary.unlink(missing_ok=True)
        except OSError: pass


def _tracking_path(root):
    return root / 'tracking.json'


def _read_tracking(root):
    path = _tracking_path(root)
    if not path.exists():
        return None, []
    try:
        item = json.loads(path.read_text(encoding='utf-8'))
        stamp = _time(item.get('tracking_since')) if isinstance(item, dict) else None
        if not isinstance(item, dict) or item.get('schema_version') != SCHEMA_VERSION or stamp is None or item.get('tracking_since') != stamp.isoformat() or item.get('sha256') != _digest(item):
            raise ValueError('格式、时间或校验和无效')
        return item, []
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, ['无效归档追踪记录 %s：%s' % (path, exc)]


def initialize(observed_at=None, archive_dir=None):
    """Record when daily capture began, even if that first capture later fails."""
    root = _root(archive_dir); root.mkdir(parents=True, exist_ok=True)
    current, warnings = _read_tracking(root)
    if current:
        return {'ok': True, 'tracking_since': current['tracking_since'], 'idempotent': True}
    if warnings:
        raise ValueError(warnings[0])
    item = {'schema_version': SCHEMA_VERSION,
            'tracking_since': _normalized_time(observed_at or datetime.now(TZ).isoformat())}
    item['sha256'] = _digest(item)
    target = _tracking_path(root)
    fd, name = tempfile.mkstemp(prefix='.tracking-', suffix='.tmp', dir=str(root)); temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(item, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(target))
        except FileExistsError:
            current, warnings = _read_tracking(root)
            if warnings or not current:
                raise ValueError(warnings[0] if warnings else '归档追踪记录无效')
            return {'ok': True, 'tracking_since': current['tracking_since'], 'idempotent': True}
        return {'ok': True, 'tracking_since': item['tracking_since'], 'idempotent': False}
    finally:
        try: temporary.unlink(missing_ok=True)
        except OSError: pass


def load(archive_dir=None):
    root = _root(archive_dir); entries, warnings = [], []
    if not root.exists():
        return entries, warnings
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted(directory.iterdir()):
            if not path.is_file() or not path.name.startswith('snapshot-'):
                continue
            if not _NAME.fullmatch(path.name):
                warnings.append('无效归档文件名 %s' % path)
                continue
            item = _read(path)
            if isinstance(item, str): warnings.append(item)
            else: entries.append(item)
    entries.sort(key=lambda item: (item['observed_at'], item['sha256']))
    return entries, warnings


def import_legacy(archive_dir=None, data_dir=None):
    root = _root(archive_dir); entries, warnings = load(root)
    imported = skipped = 0
    existing_files = {entry.get('legacy_file') for entry in entries if entry.get('legacy_file')}
    capture_days = {_time(entry['observed_at']).date().isoformat() for entry in entries if entry.get('source') == 'capture'}
    source = Path(data_dir) if data_dir is not None else Path(paths.data('data'))
    for path in sorted(source.glob('published_answers_*.json')):
        match = _LEGACY_NAME.fullmatch(path.name)
        if not match:
            warnings.append('跳过旧镜像：文件名日期无效 %s' % path.name); skipped += 1; continue
        day = match.group(1)
        try: date.fromisoformat(day)
        except ValueError:
            warnings.append('跳过旧镜像：日期无效 %s' % path.name); skipped += 1; continue
        if path.name in existing_files or day in capture_days:
            skipped += 1; continue
        try:
            rows = json.loads(path.read_text(encoding='utf-8'))
            result = save(rows, day + 'T00:00:00+00:00', source='legacy-import', precision='day', completeness='unknown', legacy_file=path.name, archive_dir=root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            warnings.append('跳过旧镜像 %s：%s' % (path.name, exc)); skipped += 1; continue
        imported += 0 if result.get('idempotent') else 1
    return {'imported': imported, 'skipped': skipped, 'warnings': warnings}


def status(as_of=None, *, entries=None, warnings=None, archive_dir=None):
    root = _root(archive_dir)
    if entries is None or warnings is None:
        entries, loaded_warnings = load(root)
        warnings = list(loaded_warnings if warnings is None else warnings)
    else:
        warnings = list(warnings)
    tracking, tracking_warnings = _read_tracking(root); warnings.extend(tracking_warnings)
    reference = _time(as_of) if as_of is not None else datetime.now(TZ)
    if reference is None:
        raise ValueError('无效的统计截止时间')
    today = reference.date()
    valid_days = sorted({_time(item['observed_at']).date() for item in entries if item.get('source') == 'capture' and item.get('completeness') == 'complete' and _time(item['observed_at']).date() <= today})
    tracking_day = _time(tracking['tracking_since']).date() if tracking else None
    first = tracking_day or (valid_days[0] if valid_days else None)
    last = valid_days[-1] if valid_days else None
    missing = []
    if first and first <= today:
        cursor = first
        # The supported product history is short; malformed ancient dates must
        # not allocate millions of missing-day strings during a UI request.
        cursor = max(cursor, today - timedelta(days=36525))
        while cursor < today:
            if cursor not in valid_days: missing.append(cursor.isoformat())
            cursor += timedelta(days=1)
    consecutive = 0; cursor = last
    while cursor and cursor in valid_days and (not first or cursor >= first):
        consecutive += 1; cursor -= timedelta(days=1)
    cutoff_passed = reference.hour >= 23
    today_status = 'recorded' if today in valid_days else ('missing' if first and first <= today and cutoff_passed else 'pending')
    return {'snapshot_count': len(entries), 'day_count': len({_time(item['observed_at']).date() for item in entries}),
            'first_date': first.isoformat() if first else None, 'tracking_since': tracking.get('tracking_since') if tracking else None,
            'last_success_date': last.isoformat() if last else None, 'consecutive_days': consecutive,
            'missing_dates': missing, 'today_status': today_status, 'invalid_count': len(warnings), 'warnings': warnings,
            'legacy_count': sum(1 for item in entries if item.get('source') == 'legacy-import'),
            'state_import_count': sum(int(item.get('count') or 0) for item in entries if item.get('source') == 'state-import'),
            'archived_dates': sorted({_time(item['observed_at']).date().isoformat() for item in entries}), 'root': str(root)}
