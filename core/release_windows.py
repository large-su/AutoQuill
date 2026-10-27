"""Release-time cohorts; inference is separate from generation provenance."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from core import paths

ZH_TZ = timezone(timedelta(hours=8))
_LOCK = threading.RLock()
_URL = 'https://api.github.com/repos/large-su/AutoQuill/releases?per_page=100'
_TAG = re.compile(r'^[vV](\d+(?:\.\d+){1,3})$')


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=ZH_TZ) if dt.tzinfo is None else dt.astimezone(ZH_TZ)
    except (ValueError, TypeError):
        return None


def _path():
    return Path(paths.data('data', 'state', 'release_times.json'))


def _cache():
    try:
        value = json.loads(_path().read_text(encoding='utf8'))
        if not isinstance(value, dict) or not isinstance(value.get('releases'), list):
            raise ValueError('invalid release cache')
        return value
    except (OSError, ValueError):
        return {'releases': [], 'synced_at': None}


def status():
    value = _cache()
    return {'synced_at': value.get('synced_at'), 'count': len(value['releases'])}


def sync_releases():
    """Explicit public metadata refresh; never upload user data or alter metrics."""
    with _LOCK:
        try:
            request = Request(_URL, headers={'Accept': 'application/vnd.github+json',
                                           'User-Agent': 'AutoQuill-release-history'})
            with urlopen(request, timeout=10) as response:
                payload = response.read(4_000_001)
            if len(payload) > 4_000_000:
                raise ValueError('版本响应过大')
            rows = json.loads(payload)
            if not isinstance(rows, list):
                raise ValueError('版本响应格式无效')
            fresh = []
            for row in rows:
                if not isinstance(row, dict) or row.get('draft') or row.get('prerelease'):
                    continue
                tag, published = row.get('tag_name', ''), row.get('published_at')
                if not isinstance(tag, str) or not _TAG.fullmatch(tag) or not timestamp(published):
                    continue
                fresh.append({'version': tag[1:], 'tag': tag,
                              'released_at': timestamp(published).isoformat(),
                              'release_time_source': 'github-release'})
            if not fresh:
                raise ValueError('未返回可用的稳定版本时间')
            merged = {r['tag']: r for r in _cache()['releases']
                      if isinstance(r, dict) and isinstance(r.get('tag'), str)}
            merged.update({r['tag']: r for r in fresh})
            value = {'synced_at': datetime.now(ZH_TZ).isoformat(timespec='seconds'),
                     'releases': list(merged.values())}
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
            os.replace(temp, path)
            return dict(ok=True, message='版本发布时间已同步', **status())
        except (OSError, ValueError, TypeError) as exc:
            return dict(ok=False, message='版本时间同步未完成，保留已有记录：' + str(exc), **status())


def overlay(history):
    """Enrich known history and add metadata-only releases beyond its code baseline."""
    rows = {r.get('tag'): dict(r) for r in history if r.get('tag')}
    for record in _cache()['releases']:
        if not isinstance(record, dict):
            continue
        tag = record.get('tag', '')
        match = _TAG.fullmatch(tag) if isinstance(tag, str) else None
        if (not match or record.get('release_time_source') != 'github-release'
                or not timestamp(record.get('released_at'))):
            continue
        row = rows.setdefault(tag, {'tag': tag, 'version': tag[1:], 'summary': 'GitHub 稳定版',
                                    'provenance': 'github-release'})
        row.update(released_at=record['released_at'], release_time_source='github-release', pending=False)
        row['date'] = timestamp(record['released_at']).date().isoformat()
    return sorted(rows.values(), key=lambda r: (timestamp(r.get('released_at') or r.get('date'))
                                               or datetime.min.replace(tzinfo=ZH_TZ), r['tag']))


def intervals(history):
    result = []
    for row in history:
        start = timestamp(row.get('released_at') or row.get('date'))
        if start:
            result.append(dict(row, released_at=start.isoformat(),
                               release_time_source=row.get('release_time_source') or 'git-date-estimate'))
    result.sort(key=lambda r: (timestamp(r['released_at']), r.get('tag', '')))
    for i, row in enumerate(result):
        row['interval_end'] = result[i + 1]['released_at'] if i + 1 < len(result) else None
    return result


def infer(article, windows, public=True):
    result = {'version': None, 'start': None, 'end': None, 'source': None,
              'precision': article.get('publish_date_precision') or 'day',
              'reason': 'missing_publish_time', 'candidates': []}
    if not public:
        return dict(result, reason='missing_public_snapshot')
    raw = article.get('publish_time') or article.get('publish_date')
    start = timestamp(raw)
    if not start:
        return result
    precision = article.get('publish_date_precision') or ('minute' if len(str(raw)) > 10 else 'day')
    end = start + (timedelta(days=1) if precision == 'day' else
                   timedelta(minutes=1) if precision == 'minute' else timedelta(seconds=1))
    result['precision'] = precision
    # A day-only release date is a range, not evidence of a midnight launch.
    uncertain = [i for i, w in enumerate(windows)
                 if w['release_time_source'] == 'git-date-estimate'
                 and timestamp(w['released_at']) < end
                 and timestamp(w['released_at']) + timedelta(days=1) > start]
    if uncertain:
        indices = {j for i in uncertain for j in (i - 1, i) if j >= 0}
        return dict(result, reason='boundary_ambiguous',
                    candidates=[windows[i].get('version') for i in sorted(indices)])
    matches = [w for w in windows if timestamp(w['released_at']) < end
               and (not w['interval_end'] or timestamp(w['interval_end']) > start)]
    result['candidates'] = [w.get('version') for w in matches]
    if not matches:
        return dict(result, reason='before_history')
    first = matches[0]
    # A partial interval at the history baseline is also ambiguous.
    if len(matches) != 1 or timestamp(first['released_at']) > start:
        return dict(result, reason='boundary_ambiguous')
    return dict(result, version=first.get('version'), start=first['released_at'],
                end=first['interval_end'], source=first['release_time_source'], reason='time_window')
