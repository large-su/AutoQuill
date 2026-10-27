"""Local, privacy-preserving provenance for the Evolution view."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import threading
import uuid
from html.parser import HTMLParser
from datetime import date, datetime
from pathlib import Path
from core import paths
from core.version import VERSION


_LOCK = threading.RLock()
_RUN_ID = uuid.uuid4().hex
_FILES = (
    'story_prompt.py',
    'story_generation.py',
    'story_scoring.py',
    'core/reference_sample.py',
    'config/story.py',
    'applications/zhihu_story/prompts.py',
    'workflows/workflow_generation.py',
)
_KEYS = (
    'LLM_MODE',
    'LLM_PROVIDER',
    'LLM_MODEL_ID',
    'WEB_LLM_DRIVER',
    'AUTHOR_PROFILE',
    'STORY_MATERIAL_MODE',
    'READER_EXPERIENCE_ENABLE',
    'CRAFT_LEARNING_ENABLE',
    'READER_CONTRACT_ENABLE',
    'REFERENCE_NARRATIVE_SAMPLE_ENABLE',
    'SCORE_STORY_HEAD_CHARS',
    'SCORE_STORY_MIDDLE_CHARS',
    'SCORE_STORY_TAIL_CHARS',
    'WORKFLOW_MODE',
    'QUESTION_SELECT_MODE',
    'QUESTION_SOURCE',
    'ENABLE_STORY_FILTER',
    'ENABLE_FORMAT_RETRY',
    'MIN_ANSWER_LENGTH',
    'MAX_TOPIC_RETRY',
    'LLM_API_TEMPERATURE',
    'LLM_API_MAX_TOKENS',
)
_METRICS = {
    'reads': '阅读',
    'likes': '赞同',
    'comments': '评论',
    'collects': '收藏',
    'favors': '喜欢',
}
_ANSWER = re.compile('^https?://(?:www\\.)?zhihu\\.com/(?:question/\\d+/)?answer/(\\d+)(?:[/?#].*)?$')


def _path():
    return Path(paths.data('data', 'state', 'evolution.json'))

def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def _blank():
    return {'schema_version': 1, 'generations': [], 'drafts': [], 'articles': {}, 'refreshes': []}

def _sha(b):
    return hashlib.sha256(b).hexdigest()


def content_fingerprint(html):
    """Hash rendered story text, ignoring markup/whitespace, never storing prose."""
    if not isinstance(html, str) or not html.strip():
        return None
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
        def handle_data(self, value):
            self.parts.append(value)
    parser = Text()
    parser.feed(html)
    plain = re.sub(r'[\s\u200b-\u200d\ufeff]+', '', ''.join(parser.parts))
    return 'text-v1:' + _sha(plain.encode('utf-8')) if plain else None

def _load():
    p = _path()
    if not p.exists():
        return (_blank(), [])
    try:
        data = json.loads(p.read_text(encoding='utf8'))
        if not isinstance(data, dict):
            raise ValueError('状态根不是对象')
        for k, v in (('generations', []), ('drafts', []), ('articles', {}), ('refreshes', []),
                     ('publication_receipts', []), ('assignments', [])):
            data.setdefault(k, v)
        valid_generations = isinstance(data['generations'], list)
        valid_drafts = isinstance(data['drafts'], list)
        valid_articles = isinstance(data['articles'], dict)
        valid_generation_items = valid_generations and all(
            isinstance(x, dict) for x in data['generations']
        )
        valid_draft_items = valid_drafts and all(
            isinstance(x, dict) for x in data['drafts']
        )
        valid_article_items = valid_articles and all(
            isinstance(k, str) and isinstance(v, dict)
            for k, v in data['articles'].items()
        )
        if not all((
            valid_generations,
            valid_drafts,
            valid_articles,
            valid_generation_items,
            valid_draft_items,
            valid_article_items,
        )):
            raise ValueError('状态字段类型无效')
        if data.get('schema_version', 1) != 1:
            raise ValueError('不支持的演进状态版本')
        for key in ('publication_receipts', 'assignments'):
            if not isinstance(data[key], list) or any(not isinstance(x, dict) for x in data[key]):
                raise ValueError('归属审计记录字段无效')
        if not isinstance(data['refreshes'], list) or any(
                not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or _date(item.get('started_at')) is None
                for item in data['refreshes']):
            raise ValueError('刷新记录字段无效')
        generation_fields = ('id', 'episode', 'fingerprint', 'basename', 'content_hash', 'observed_at')
        if any(not all(isinstance(item.get(key), str) for key in generation_fields)
               for item in data['generations']):
            raise ValueError('稿件记录缺少有效标识')
        for aid, article in data['articles'].items():
            observations = article.get('observations', [])
            if article.get('aid') != aid or not isinstance(observations, list):
                raise ValueError('作品记录字段无效')
            if any(not isinstance(item, dict) or _date(item.get('observed_at')) is None
                   for item in observations):
                raise ValueError('观测记录字段无效')
        return (data, [])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return (None, [f'演进状态无法解析，未覆盖原文件：{exc}'])

def _save(data):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_suffix('.tmp')
    t.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf8')
    os.replace(t, p)

def _history():
    try:
        history_path = Path(paths.program('core', 'evolution_history.json'))
        history = json.loads(history_path.read_text(encoding='utf8'))
        releases = history.get('releases', []) if isinstance(history, dict) else []
        releases = [dict(row) for row in releases if isinstance(row, dict)] if isinstance(releases, list) else []
        # 当前提交不能将自己的 SHA 写回受控文件；安装包已有独立的构建来源记录。
        try:
            build_info = json.loads(Path(paths.program('build_info.json')).read_text(encoding='utf8'))
            for row in releases:
                if row.get('pending') and row.get('version') == build_info.get('version'):
                    row['commit'] = build_info.get('source_commit')
                    row['provenance'] = 'build-info'
        except (OSError, ValueError, AttributeError):
            pass
        return releases
    except (OSError, ValueError, json.JSONDecodeError):
        return []

def _code_id():
    h = hashlib.sha256()
    found = False
    for rel in sorted(_FILES):
        p = Path(paths.program(*rel.split('/')))
        # This module did not exist before v5.6; do not change old code IDs
        # merely by adding a new file to the provenance schema.
        if rel == 'core/reference_sample.py' and not p.is_file():
            continue
        data = p.read_bytes().replace(b'\r\n', b'\n') if p.is_file() else b'<missing>'
        h.update(rel.encode() + b'\x00' + data + b'\x00')
        found = found or p.is_file()
    if found:
        return h.hexdigest()
    history = _history()
    return str(history[-1].get('writing_code_id') or '') if history else ''

def _settings(settings):
    if settings is None:
        try:
            import config
            from config import story
            src = {k: getattr(config, k, None) for k in _KEYS}
            for k in _KEYS:
                src[k] = getattr(story, k, src.get(k))
            src['WEB_LLM_DRIVER'] = getattr(config, 'WEB_LLM_DRIVER', getattr(config, 'WEB_DRIVER_NAME', None))
        except Exception:
            src = {}
    elif isinstance(settings, dict):
        src = settings
    else:
        src = {k: getattr(settings, k, None) for k in _KEYS}
    return {k: src[k] for k in _KEYS if k in src and isinstance(src[k], (str, int, float, bool, type(None)))}

def record_generation(story_file, story_text, settings=None, workflow_mode=None):
    safe = _settings(settings)
    if workflow_mode in ('classic', 'clean'):
        safe['WORKFLOW_MODE'] = workflow_mode
    code = _code_id()
    fingerprint = code + ':' + _sha(json.dumps(safe, sort_keys=True, ensure_ascii=False).encode())
    name = Path(str(story_file or '')).name
    content = _sha(str(story_text or '').encode())
    with _LOCK:
        d, _ = _load()
        if d is None:
            return None
        old = next(
            (
                g for g in d['generations']
                if g.get('basename') == name and g.get('content_hash') == content
            ),
            None,
        )
        if old:
            return {'id': old['id'], 'event': 'file_save', 'fingerprint': old['fingerprint']}
        prev = d['generations'][-1] if d['generations'] else None
        episode = prev['episode'] if prev and prev.get('fingerprint') == fingerprint else 'runtime:' + uuid.uuid4().hex
        g = {
            'id': 'generation:' + _sha((name + '\x00' + content).encode())[:20],
            'basename': name,
            'content_hash': content,
            'fingerprint': fingerprint,
            'writing_code_id': code or None,
            'episode': episode,
            'settings': safe,
            'version': VERSION,
            'observed_at': _now(),
            'event': 'file_save',
        }
        d['generations'].append(g)
        _save(d)
        return {'id': g['id'], 'event': 'file_save', 'fingerprint': fingerprint}

def record_draft(question_url, story_file, title='', *, story_text=None, rendered_html=None):
    m = re.search('question/(\\d+)', str(question_url or ''))
    qid = m.group(1) if m else ''
    name = Path(str(story_file or '')).name
    with _LOCK:
        d, _ = _load()
        if d is None:
            return None
        candidates = [x for x in d['generations'] if x.get('basename') == name]
        if story_text is not None:
            candidates = [x for x in candidates if x['content_hash'] == _sha(str(story_text).encode())]
        g = candidates[-1] if candidates else None
        if not g:
            return None
        content_hash = content_fingerprint(rendered_html) if story_text is not None else None
        same = [x for x in d['drafts'] if x.get('qid') == qid and x.get('generation_id') == g['id']
                and x.get('content_fingerprint') == content_hash]
        if same:
            return dict(same[0])
        x = {'id': 'draft:' + uuid.uuid4().hex, 'qid': qid, 'title': str(title or ''), 'generation_id': g['id'], 'public': False, 'observed_at': _now(),
             'content_fingerprint': content_hash}
        d['drafts'].append(x)
        _save(d)
        return dict(x)

def record_publication(result):
    result = result or {}
    m = _ANSWER.match(str(result.get('url') or ''))
    if not result.get('ok') or result.get('dry_run'):
        return None
    qid = str(result.get('qid') or '')
    url_qid = re.search(r'/question/(\d+)/answer/', str(result.get('url') or ''))
    qid_conflict = bool(url_qid and qid and url_qid.group(1) != qid)
    qid = qid or (url_qid.group(1) if url_qid else '')
    with _LOCK:
        d, _ = _load()
        if d is None:
            return None
        receipt = {k: result.get(k) for k in ('url', 'qid', 'draft_content_fingerprint')}
        receipt['id'] = 'receipt:' + _sha(json.dumps(receipt, sort_keys=True).encode())[:24]
        receipts = d.setdefault('publication_receipts', [])
        if not any(x.get('id') == receipt['id'] for x in receipts):
            receipts.append(dict(receipt, observed_at=_now(), aid=m.group(1) if m else None))
        if not m:
            _save(d)
            return None
        aid = m.group(1)
        matches = [x for x in d['drafts'] if qid and x.get('qid') == qid]
        draft = matches[0] if len(matches) == 1 else None
        digest = result.get('draft_content_fingerprint')
        verified = [x for x in matches if digest and x.get('content_fingerprint') == digest]
        generations = {x.get('generation_id') for x in verified}
        if len(generations) == 1 and not qid_conflict:
            draft = verified[-1]
        if draft:
            draft['publication_receipt_at'] = _now()
        a = d['articles'].setdefault(aid, {'aid': aid, 'observations': [], 'node_id': None, 'assignment_source': None, 'attribution': 'unassigned'})
        a.update({'url': str(result.get('url') or a.get('url') or ''), 'title': str(result.get('title') or a.get('title') or ''), 'qid': qid, 'published_at': a.get('published_at') or _now()})
        if draft and not qid_conflict:
            if a.get('generation_id') and a['generation_id'] != draft['generation_id']:
                a['provenance_conflict'] = True
            else:
                a['generation_id'] = draft['generation_id']
        if qid_conflict:
            a['provenance_conflict'] = True
        previous = a.get('publication_evidence') if isinstance(a.get('publication_evidence'), dict) else {}
        if digest and previous.get('content_fingerprint') and digest != previous['content_fingerprint']:
            a['provenance_conflict'] = True
        if draft and len(generations) == 1 and not qid_conflict:
            a['publication_evidence'] = {'receipt_id': receipt['id'], 'draft_id': draft['id'],
                                         'generation_id': draft['generation_id'],
                                         'content_fingerprint': digest, 'verified_at': _now()}
        _save(d)
        return dict(a)

def _number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) and v >= 0 else None
    m = re.match('^(\\d+(?:\\.\\d+)?)(万|千|[wWkK])?$', str(v or '').replace(' ', '').replace(',', ''))
    if not m:
        return None
    n = float(m.group(1))
    u = m.group(2)
    n *= 10000 if u in ('万', 'w', 'W') else 1000 if u in ('千', 'k', 'K') else 1
    if not math.isfinite(n):
        return None
    return int(n) if n.is_integer() else n

def _date(v):
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00')).date()
    except ValueError:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None

def _publish(v, observed):
    s = str(v or '').strip()
    if re.match('^\\d{4}-\\d\\d-\\d\\d', s):
        return s[:10]
    m = re.match('^(\\d\\d)-(\\d\\d)', s)
    o = _date(observed)
    if not m or not o:
        return ''
    try:
        d = date(o.year, int(m.group(1)), int(m.group(2)))
        return (date(o.year - 1, d.month, d.day) if d > o else d).isoformat()
    except ValueError:
        return ''

def ingest_snapshot(rows, observed_at=None):
    observed_at = observed_at or _now()
    with _LOCK:
        d, _ = _load()
        if d is None:
            return 0
        count = 0
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            aid = str(raw.get('aid') or '')
            if not aid:
                continue
            a = d['articles'].setdefault(aid, {'aid': aid, 'observations': [], 'node_id': None, 'assignment_source': None, 'attribution': 'unassigned'})
            for k in ('url', 'title'):
                if raw.get(k) is not None:
                    a[k] = raw[k]
            pub = _publish(raw.get('publish_date') or raw.get('publish'), observed_at)
            if pub:
                a['publish_date'] = pub
                a['publish_date_precision'] = 'day'
            a['public_seen'] = True
            for draft in d['drafts']:
                if a.get('generation_id') and draft.get('generation_id') == a['generation_id']:
                    draft['public'] = True
            source = raw.get('metrics') if isinstance(raw.get('metrics'), dict) else raw
            obs = {'observed_at': observed_at, 'source': 'fresh', 'date_precision': 'second'}
            for k, label in _METRICS.items():
                obs[k] = _number(source.get(k, source.get(label)))
            old = next((x for x in a['observations'] if x.get('observed_at') == observed_at and x.get('source') == 'fresh'), None)
            if old:
                old.update(obs)
            else:
                a['observations'].append(obs)
            count += 1
        _save(d)
        return count


def start_refresh(source='manual', refresh_key=''):
    """记录一次实际刷新尝试；状态随观测留在本机，重启后仍可查。"""
    with _LOCK:
        data, warnings = _load()
        if data is None:
            raise ValueError(warnings[0])
        node = {'id': 'refresh:' + uuid.uuid4().hex, 'source': str(source),
                'key': str(refresh_key), 'started_at': _now(), 'status': 'running',
                'run_id': _RUN_ID, 'count': 0, 'observed_at': None,
                'source_file': None, 'message': ''}
        data['refreshes'].append(node)
        _save(data)
        return dict(node)


def finish_refresh(node_id, *, status, count=0, observed_at=None,
                   source_file=None, message=''):
    with _LOCK:
        data, warnings = _load()
        if data is None:
            raise ValueError(warnings[0])
        node = next((r for r in data['refreshes'] if r['id'] == node_id), None)
        if node is None:
            raise ValueError('未知刷新节点')
        node.update(status=status, count=int(count), observed_at=observed_at,
                    source_file=Path(source_file).name if source_file else None,
                    message=str(message)[:500], finished_at=_now())
        _save(data)
        return dict(node)


def _refresh_payload(data, limit=20):
    nodes = [dict(row) for row in data.get('refreshes', [])]
    for node in nodes:
        if node.get('status') == 'running' and node.get('run_id') != _RUN_ID:
            node['status'] = 'interrupted'
            node['message'] = '上次进程退出前未确认完成，未补造观测'
        node.pop('run_id', None)
    good = [n for n in nodes if n.get('status') == 'done']
    return {'history': list(reversed(nodes[-limit:])),
            'latest': nodes[-1] if nodes else None,
            'last_success': good[-1] if good else None,
            'total': len(nodes)}


def refresh_status(limit=20):
    with _LOCK:
        data, warnings = _load()
        return dict(_refresh_payload(data or _blank(), limit), warnings=warnings)


def find_refresh(refresh_key, source='automation'):
    """查找同一任务已确认完成的采集，用于提交观测后崩溃的恢复。"""
    with _LOCK:
        data, _ = _load()
        for node in reversed((data or _blank())['refreshes']):
            if (node.get('status') == 'done' and node.get('key') == refresh_key
                    and node.get('source') == source):
                return {k: v for k, v in node.items() if k != 'run_id'}
    return None

def _legacy():
    out = {}
    root = Path(paths.data('data'))
    for p in sorted(root.glob('published_answers_*.json')):
        m = re.search('(\\d{4}-\\d\\d-\\d\\d)', p.name)
        if not m:
            continue
        stamp = m.group(1) + 'T00:00:00+00:00'
        try:
            rows = json.loads(p.read_text(encoding='utf8'))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            aid = str(raw.get('aid') or '')
            if not aid:
                continue
            flat = not isinstance(raw.get('metrics'), dict)
            source = raw if flat else raw['metrics']
            a = out.setdefault(aid, {'aid': aid, 'observations': []})
            for k in ('url', 'title'):
                if raw.get(k) is not None:
                    a[k] = raw[k]
            pub = _publish(raw.get('publish_date') or raw.get('publish'), stamp)
            if pub:
                a['publish_date'] = pub
                a['publish_date_precision'] = 'day'
            obs = {'observed_at': stamp, 'source': 'legacy', 'date_precision': 'day', 'quality': 'legacy-normalized' if flat else 'raw'}
            for k, label in _METRICS.items():
                v = _number(source.get(k, source.get(label)))
                obs[k] = None if flat and v == 0 else v
            a['observations'].append(obs)
    return out

def _nodes(d):
    out = []
    prev = None
    for r in _history():
        code = str(r.get('writing_code_id') or '')
        if prev and code and prev['writing_code_id'] == code:
            prev['releases'].append(str(r.get('tag') or ''))
            continue
        prev = {'id': 'history:' + str(r.get('tag') or 'unknown'), 'label': '历史方案 ' + str(r.get('tag') or 'unknown'), 'source': 'history', 'start_date': r.get('date'), 'releases': [str(r.get('tag') or '')], 'writing_code_id': code or None, 'description': '历史运行配置未知', 'current': False}
        out.append(prev)
    runtime = []
    for g in d['generations']:
        if g.get('episode') and (not any((x['id'] == g['episode'] for x in runtime))):
            runtime.append({'id': g['episode'], 'label': '', 'source': 'runtime', 'start_date': g.get('observed_at', '')[:10], 'releases': [], 'writing_code_id': g.get('writing_code_id'), 'description': '在文件保存时记录的配置快照', 'current': False})
    for i, n in enumerate(runtime, 1):
        episode_generations = [x for x in d['generations'] if x.get('episode') == n['id']]
        g = episode_generations[0]
        versions = list(dict.fromkeys(str(item.get('version')).lstrip('v')
                                     for item in episode_generations if item.get('version')))
        version_label = ('v' + versions[0] if len(versions) == 1 else
                         'v' + versions[0] + ' ～ v' + versions[-1] if versions else '版本未知')
        n['label'] = f"试用 {i} · {version_label}"
        n['settings'] = _settings(g.get('settings', {}))
        n['releases'] = ['v' + version for version in versions]
    if runtime:
        runtime[-1]['current'] = True
    return out + runtime

def _merged(d):
    out = _legacy()
    for aid, s in d['articles'].items():
        a = out.setdefault(aid, {'aid': aid, 'observations': []})
        a.update({k: v for k, v in s.items() if k != 'observations' and v is not None})
        a['observations'].extend(s.get('observations', []))
    for a in out.values():
        a['observations'] = sorted({(x.get('observed_at'), x.get('source')): x for x in a['observations']}.values(), key=lambda x: x.get('observed_at', ''))
        fresh_mirrors = {(_date(o.get('observed_at')), tuple(_number(o.get(k)) for k in _METRICS))
                         for o in a['observations'] if o.get('source') == 'fresh'}
        # 每日看板文件是同一次采集的副本。已有同日同指标的精确观测时，
        # 不再把文件名推导出的午夜当成另一个真实采集点。
        a['observations'] = [o for o in a['observations']
                             if o.get('source') != 'legacy'
                             or (_date(o.get('observed_at')), tuple(_number(o.get(k)) for k in _METRICS)) not in fresh_mirrors]
    return out

def _ledger_versions():
    """Only an aid explicitly recorded with a version is usable as a hint."""
    result = {}
    path = Path(paths.data('data', 'state', 'published_topics.jsonl'))
    try:
        lines = path.read_text(encoding='utf8').splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        aid = str(row.get('aid') or '')
        version = str(row.get('version') or '')
        if aid and version:
            result[aid] = version
            continue
        # A canonical answer URL is also an exact aid; titles/dates never are.
        match = _ANSWER.match(str(row.get('url') or ''))
        if match and version:
            result[match.group(1)] = version
    return result

def assign_article(aid, node_id):
    with _LOCK:
        d, w = _load()
        if d is None:
            raise ValueError(w[0])
        article = _merged(d).get(str(aid))
        valid = {n['id'] for n in _nodes(d)}
        if not article:
            raise ValueError('未知文章')
        if node_id is not None and (not article.get('observations')):
            raise ValueError('文章尚未在公开看板快照中确认')
        if node_id is not None and node_id not in valid:
            raise ValueError('未知方案')
        a = d['articles'].setdefault(str(aid), {'aid': str(aid), 'observations': []})
        # 确认时留存已有观测，之后清理看板快照也不会从方案中删掉低表现样本。
        a['observations'] = [dict(observation) for observation in article.get('observations', [])]
        for k in ('url', 'title', 'publish_date', 'publish_date_precision'):
            if article.get(k):
                a[k] = article[k]
        a.update({'node_id': node_id, 'assignment_source': 'manual' if node_id else 'manual_clear', 'attribution': 'confirmed' if node_id else 'unassigned'})
        d.setdefault('assignments', []).append({'aid': str(aid), 'node_id': node_id,
                                               'observed_at': _now(), 'source': a['assignment_source']})
        _save(d)


def _resolve_attribution(article, data, generations, nodes, public):
    """Resolve only recorded evidence; read queries never rewrite historic state."""
    generation = generations.get(article.get('generation_id'), {})
    candidate = generation.get('episode')
    if candidate not in nodes:
        candidate = None
    source = article.get('assignment_source')
    if source == 'manual_clear':
        return None, 'unassigned', source, 'manual_clear', False
    if not public:
        return None, 'draft_match_pending', source, 'missing_public_snapshot', False
    if source == 'manual' and article.get('node_id') in nodes:
        node = article['node_id']
        return node, 'confirmed', source, 'manual', bool(generation.get('version') and node == candidate)
    if article.get('provenance_conflict'):
        return None, 'unassigned', None, 'conflicting_receipt', False
    if not generation or not candidate:
        return None, 'unassigned', None, 'missing_generation', False
    evidence = article.get('publication_evidence') if isinstance(article.get('publication_evidence'), dict) else {}
    draft = next((x for x in data['drafts'] if x.get('id') == evidence.get('draft_id')), None)
    receipt = next((x for x in data.get('publication_receipts', [])
                    if x.get('id') == evidence.get('receipt_id')), None)
    digest = evidence.get('content_fingerprint')
    verified = (draft and receipt and isinstance(digest, str)
                and re.fullmatch(r'text-v1:[0-9a-f]{64}', digest)
                and evidence.get('generation_id') == generation['id'] == draft.get('generation_id')
                and digest == draft.get('content_fingerprint') == receipt.get('draft_content_fingerprint')
                and str(receipt.get('aid')) == article['aid'])
    if not verified:
        return None, 'unassigned', None, 'missing_content_evidence', False
    return candidate, 'confirmed', 'verified_chain', 'verified_chain', bool(generation.get('version'))

def _q(v, q):
    v = sorted(v)
    if not v:
        return None
    p = (len(v) - 1) * q
    l = int(p)
    h = min(l + 1, len(v) - 1)
    return v[l] + (v[h] - v[l]) * (p - l)

def _window_observation(observations, horizon, metric):
    candidates = [o for o in observations
                  if o.get('age_days') is not None
                  and horizon <= o['age_days'] <= horizon + 3
                  and o.get(metric) is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda o: (
        o['age_days'] - horizon, o.get('source') != 'fresh',
        -datetime.fromisoformat(o['observed_at'].replace('Z', '+00:00')).timestamp()))


def _feedback_statistics(articles, horizon, metric):
    mature = [a for a in articles if a['age_days'] is not None and a['age_days'] >= horizon]
    by_metric = {}
    for key in _METRICS:
        values = [a['horizon_observations'][key][key] for a in mature
                  if a['horizon_observations'][key] is not None]
        by_metric[key] = {
            'valid_n': len(values), 'missing': len(mature) - len(values),
            'sum': sum(values) if values else None,
            'median': _q(values, .5), 'p25': _q(values, .25),
            'mean': sum(values) / len(values) if values else None,
            'top_share': max(values) / sum(values) if values and sum(values) else None,
        }
    selected = by_metric[metric]
    return dict(selected, assigned=len(articles), total=selected['valid_n'],
                mature=len(mature), observed=selected['valid_n'],
                immature=sum(a['age_days'] is not None and a['age_days'] < horizon for a in articles),
                unknown_publish=sum(a['age_days'] is None for a in articles),
                metric=metric, by_metric=by_metric,
                status='ok' if selected['valid_n'] >= 5 else 'insufficient')


def _observation_change(observations):
    """只相减两次实际观测；负数保留，缺一个值则为 None。"""
    fresh_dates = {_date(o['observed_at']) for o in observations if o.get('source') == 'fresh'}
    observations = [o for o in observations
                    if o.get('source') != 'legacy' or _date(o['observed_at']) not in fresh_dates]
    if len(observations) < 2:
        return None
    before, after = observations[-2:]
    return {'from_at': before['observed_at'], 'to_at': after['observed_at'],
            'from_precision': before.get('date_precision'),
            'to_precision': after.get('date_precision'),
            'metrics': {k: after[k] - before[k]
                        if before.get(k) is not None and after.get(k) is not None else None
                        for k in _METRICS}}


def contract(horizon=30, metric='likes', since='', until='', aid='', node_id='', as_of=None, version=''):
    try:
        horizon = int(horizon)
    except (ValueError, TypeError):
        horizon = 30
    horizon = horizon if horizon in (7, 14, 30, 90) else 30
    metric = metric if metric in _METRICS else 'likes'
    today = _date(as_of) if as_of is not None else date.today()
    if today is None:
        raise ValueError('无效的查询截止日期')
    start, end = _date(since) if since else None, _date(until) if until else None
    if (since and start is None) or (until and end is None) or (start and end and start > end):
        raise ValueError('无效的作品发布日期范围')
    filter_version = str(version or '').removeprefix('v')
    if filter_version and not re.fullmatch(r'\d+(?:\.\d+){1,3}', filter_version):
        raise ValueError('无效的生成版本')
    with _LOCK:
        d, warnings = _load()
        d = d or _blank()
        nodes = _nodes(d)
        by = {n['id']: n for n in nodes}
        gens = {g['id']: g for g in d['generations']}
        ledger = _ledger_versions()
        articles = []
        for a in _merged(d).values():
            pub = _date(a.get('publish_date'))
            if aid and a['aid'] != str(aid):
                continue
            if (start and (pub is None or pub < start)) or (end and (pub is None or pub > end)):
                continue
            obs = []
            for o in a['observations']:
                x = dict(o)
                for key in _METRICS:
                    x[key] = _number(x.get(key))
                od = _date(x.get('observed_at'))
                if od is None or od > today:
                    continue
                x['age_days'] = (od - pub).days if od and pub else None
                obs.append(x)
            hovs = {k: _window_observation(obs, horizon, k) for k in _METRICS}
            hov = hovs[metric]
            generation = gens.get(a.get('generation_id'), {})
            node, attribution, assignment_source, reason, version_confirmed = _resolve_attribution(a, d, gens, by, bool(obs))
            suggested = None if a.get('assignment_source') == 'manual_clear' else node or gens.get(a.get('generation_id'), {}).get('episode')
            if not suggested and a.get('assignment_source') != 'manual_clear' and a['aid'] in ledger:
                tag = 'v' + ledger[a['aid']].lstrip('v')
                suggested = next((n['id'] for n in nodes if tag in n['releases']), None)
            if node_id and node != node_id:
                continue
            if filter_version and (not version_confirmed or str(generation.get('version') or '').lstrip('v') != filter_version):
                continue
            age = (today - pub).days if pub else None
            windows = {str(h): {k: _window_observation(obs, h, k) for k in _METRICS}
                       for h in (7, 14, 30)}
            window_deltas = {}
            for low, high in ((7, 14), (14, 30)):
                window_deltas[f'{low}_{high}'] = {}
                for k in _METRICS:
                    before, after = windows[str(low)][k], windows[str(high)][k]
                    window_deltas[f'{low}_{high}'][k] = (
                        {'value': after[k] - before[k], 'from_at': before['observed_at'],
                         'to_at': after['observed_at'], 'from_age_days': before['age_days'],
                         'to_age_days': after['age_days']}
                        if before is not None and after is not None else None)
            item = {'aid': a['aid'], 'url': a.get('url', ''), 'title': a.get('title', ''),
                    'publish_date': a.get('publish_date', ''), 'published_at': a.get('published_at'),
                    'publish_date_precision': a.get('publish_date_precision'),
                    'attribution': attribution, 'assignment_source': assignment_source,
                    'attribution_reason': reason, 'version_confirmed': version_confirmed,
                    'node_id': node, 'suggested_node_id': suggested if suggested in by else None,
                    'generation_id': a.get('generation_id'),
                    'generation_version': generation.get('version'),
                    'ledger_version_hint': ledger.get(a['aid']),
                    'publication_evidence': a.get('publication_evidence'),
                    'generation_episode': generation.get('episode'),
                    'latest': obs[-1] if obs else {k: None for k in _METRICS},
                    'observations': obs, 'horizon_observation': hov,
                    'horizon_observations': hovs, 'age_days': age, 'windows': windows,
                    'window_deltas': window_deltas, 'latest_change': _observation_change(obs),
                    'window_status': 'unknown_publish' if age is None else 'immature' if age < horizon
                                     else 'observed' if hov else 'missing'}
            articles.append(item)
        for n in nodes:
            assigned = [a for a in articles if a['node_id'] == n['id'] and a['attribution'] == 'confirmed']
            candidates = [a for a in articles if a['suggested_node_id'] == n['id']
                          and a['attribution'] != 'confirmed' and a['observations']
                          and a['attribution_reason'] == 'missing_content_evidence']
            n['article_ids'] = [a['aid'] for a in assigned]
            n['metrics'] = _feedback_statistics(assigned, horizon, metric)
            n['candidate_article_ids'] = [a['aid'] for a in candidates]
            n['candidate_metrics'] = _feedback_statistics(candidates, horizon, metric)
            n['publication_counts'] = {
                'total': len(assigned), 'dated': sum(bool(a['publish_date']) for a in assigned),
                'by_date': {day: sum(a['publish_date'] == day for a in assigned)
                            for day in sorted({a['publish_date'] for a in assigned if a['publish_date']})}}
        releases = []
        for r in _history():
            x = {k: r.get(k) for k in ('version', 'tag', 'date', 'change_kind', 'writing_code_id', 'writing_files_changed', 'changed_files', 'pending')}
            version = str(r.get('version') or r.get('tag') or '').lstrip('v')
            assigned = [a for a in articles if a['version_confirmed']
                        and str(a.get('generation_version') or '').lstrip('v') == version]
            candidates = [a for a in articles if not a['version_confirmed']
                          and a['attribution_reason'] == 'missing_content_evidence' and a['observations']
                          and str(a.get('generation_version') or '').lstrip('v') == version]
            x.update(commit=r.get('commit') or r.get('source_commit'),
                     summary=r.get('summary') or r.get('summaries') or [],
                     node_id=next((n['id'] for n in nodes if r.get('tag') in n['releases']), None),
                     article_ids=[a['aid'] for a in assigned],
                     metrics=_feedback_statistics(assigned, horizon, metric),
                     candidate_article_ids=[a['aid'] for a in candidates],
                     candidate_metrics=_feedback_statistics(candidates, horizon, metric))
            releases.append(x)
        public = [a for a in articles if a['observations']]
        latest = max((o['observed_at'] for a in public for o in a['observations']), default=None)
        reasons = {reason: sum(a['attribution_reason'] == reason for a in public)
                   for reason in {a['attribution_reason'] for a in public}}
        quality = {'public_articles': len(public),
                   'confirmed': sum(a['attribution'] == 'confirmed' for a in public),
                   'candidates': reasons.get('missing_content_evidence', 0),
                   'conflicts': reasons.get('conflicting_receipt', 0),
                   'manual_cleared': reasons.get('manual_clear', 0),
                   'unknown': reasons.get('missing_generation', 0),
                   'latest_observed_at': latest,
                   'stale_days': (today - _date(latest)).days if latest else None,
                   'missing_answer_receipts': sum(not r.get('aid') for r in d.get('publication_receipts', [])),
                   'by_reason': reasons, 'window_metrics': _feedback_statistics(public, horizon, metric),
                   'latest_metrics': {key: {'valid_n': sum(a['latest'].get(key) is not None for a in public),
                                            'missing': sum(a['latest'].get(key) is None for a in public)}
                                      for key in _METRICS},
                   'unavailable': {'followers': '尚无可核验的账号新增关注观测，问题关注数不是账号关注数',
                                   'per_run_hour': '尚无完整的版本运行暴露时间，不能计算每小时回报'}}
        warnings.append('旧记录缺正文一致性证据时仅列候选；台账运行版本不作为生成版本。')
        return {'schema_version': 1, 'current_version': VERSION,
                'objective': '比较逐作品发布年龄窗口的真实反馈；保留缺失和归属证据，不作因果判断。',
                'horizon': horizon, 'horizons': [7, 14, 30, 90],
                'metric': metric, 'metrics_available': dict(_METRICS),
                'query': {'since': since, 'until': until, 'aid': aid, 'node_id': node_id, 'version': filter_version,
                          'as_of': today.isoformat(), 'window_slack_days': 3},
                'refreshes': _refresh_payload(d), 'quality': quality, 'nodes': nodes, 'releases': releases,
                'edges': [{'source': nodes[i - 1]['id'], 'target': n['id'], 'label': '演进'}
                          for i, n in enumerate(nodes) if i],
                'articles': articles,
                'summary': {'nodes': len(nodes), 'articles': len(articles),
                            'confirmed': sum(a['attribution'] == 'confirmed' for a in articles),
                            'unassigned': sum(a['attribution'] != 'confirmed' for a in articles)},
                'warnings': warnings}


