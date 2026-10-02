"""Local, privacy-preserving provenance for the Evolution view."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from core import paths
from core.version import VERSION


_LOCK = threading.RLock()
_FILES = (
    'story_prompt.py',
    'story_generation.py',
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
    return {'schema_version': 1, 'generations': [], 'drafts': [], 'articles': {}}

def _sha(b):
    return hashlib.sha256(b).hexdigest()

def _load():
    p = _path()
    if not p.exists():
        return (_blank(), [])
    try:
        data = json.loads(p.read_text(encoding='utf8'))
        if not isinstance(data, dict):
            raise ValueError('状态根不是对象')
        for k, v in (('generations', []), ('drafts', []), ('articles', {})):
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

def record_generation(story_file, story_text, settings=None):
    safe = _settings(settings)
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

def record_draft(question_url, story_file, title=''):
    m = re.search('question/(\\d+)', str(question_url or ''))
    qid = m.group(1) if m else ''
    name = Path(str(story_file or '')).name
    with _LOCK:
        d, _ = _load()
        if d is None:
            return None
        g = next((x for x in reversed(d['generations']) if x.get('basename') == name), None)
        if not g:
            return None
        same = [x for x in d['drafts'] if x.get('qid') == qid and x.get('generation_id') == g['id']]
        if same:
            return dict(same[0])
        x = {'id': 'draft:' + uuid.uuid4().hex, 'qid': qid, 'title': str(title or ''), 'generation_id': g['id'], 'public': False, 'observed_at': _now()}
        d['drafts'].append(x)
        _save(d)
        return dict(x)

def record_publication(result):
    result = result or {}
    m = _ANSWER.match(str(result.get('url') or ''))
    if not result.get('ok') or result.get('dry_run') or (not m):
        return None
    aid, qid = (m.group(1), str(result.get('qid') or ''))
    with _LOCK:
        d, _ = _load()
        if d is None:
            return None
        matches = [x for x in d['drafts'] if qid and x.get('qid') == qid]
        draft = matches[0] if len(matches) == 1 else None
        if draft:
            draft['publication_receipt_at'] = _now()
        a = d['articles'].setdefault(aid, {'aid': aid, 'observations': [], 'node_id': None, 'assignment_source': None, 'attribution': 'unassigned'})
        a.update({'url': str(result.get('url') or a.get('url') or ''), 'title': str(result.get('title') or a.get('title') or ''), 'qid': qid, 'published_at': a.get('published_at') or _now()})
        if draft:
            a['generation_id'] = draft['generation_id']
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
        versions = list(dict.fromkeys(str(item.get('version', VERSION)).lstrip('v')
                                     for item in episode_generations))
        version_label = versions[0] if len(versions) == 1 else versions[0] + ' ～ ' + versions[-1]
        n['label'] = f"试用 {i} · v{version_label}"
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
        _save(d)

def _q(v, q):
    v = sorted(v)
    if not v:
        return None
    p = (len(v) - 1) * q
    l = int(p)
    h = min(l + 1, len(v) - 1)
    return v[l] + (v[h] - v[l]) * (p - l)

def contract(horizon=30):
    try:
        horizon = int(horizon)
    except (ValueError, TypeError):
        horizon = 30
    horizon = horizon if horizon in (7, 14, 30, 90) else 30
    with _LOCK:
        d, warnings = _load()
        d = d or _blank()
        nodes = _nodes(d)
        by = {n['id']: n for n in nodes}
        gens = {g['id']: g for g in d['generations']}
        ledger = _ledger_versions()
        for n in nodes:
            n.update(article_ids=[], metrics={'assigned': 0, 'total': 0, 'mature': 0, 'observed': 0, 'missing': 0, 'median': None, 'p25': None, 'mean': None, 'top_share': None, 'status': 'insufficient'})
        articles = []
        for a in _merged(d).values():
            pub = _date(a.get('publish_date'))
            obs = []
            for o in a['observations']:
                x = dict(o)
                od = _date(x.get('observed_at'))
                x['age_days'] = (od - pub).days if od and pub else None
                obs.append(x)
            candidates = [o for o in obs if horizon <= (o.get('age_days') if o.get('age_days') is not None else -1) <= horizon + 3 and o.get('likes') is not None]
            hov = min(candidates, key=lambda o: (abs(o['age_days'] - horizon), -datetime.fromisoformat(o['observed_at'].replace('Z', '+00:00')).timestamp())) if candidates else None
            node = a.get('node_id')
            suggested = None if a.get('assignment_source') == 'manual_clear' else node or gens.get(a.get('generation_id'), {}).get('episode')
            if not suggested and a.get('assignment_source') != 'manual_clear' and a['aid'] in ledger:
                tag = 'v' + ledger[a['aid']].lstrip('v')
                suggested = next((n['id'] for n in nodes if tag in n['releases']), None)
            attribution = a.get('attribution', 'unassigned')
            if a.get('published_at') and (not a.get('observations')):
                attribution = 'draft_match_pending'
            item = {'aid': a['aid'], 'url': a.get('url', ''), 'title': a.get('title', ''), 'publish_date': a.get('publish_date', ''), 'attribution': attribution, 'node_id': node, 'suggested_node_id': suggested if suggested in by else None, 'latest': obs[-1] if obs else {k: None for k in _METRICS}, 'observations': obs, 'horizon_observation': hov, 'age_days': (date.today() - pub).days if pub else None}
            articles.append(item)
            if node in by and item['attribution'] == 'confirmed':
                m = by[node]['metrics']
                by[node]['article_ids'].append(a['aid'])
                m['assigned'] += 1
                if item['age_days'] is not None and item['age_days'] >= horizon:
                    if hov:
                        m['observed'] += 1
                    else:
                        m['missing'] += 1
                    m['mature'] += 1
        article_by_aid = {article['aid']: article for article in articles}
        for n in nodes:
            vals = [article_by_aid[aid]['horizon_observation']['likes'] for aid in n['article_ids'] if article_by_aid[aid]['horizon_observation']]
            m = n['metrics']
            m['total'] = len(vals)
            if vals:
                m.update(median=_q(vals, 0.5), p25=_q(vals, 0.25), mean=sum(vals) / len(vals), top_share=max(vals) / sum(vals) if sum(vals) else None, status='ok' if len(vals) >= 5 else 'insufficient')
        releases = []
        for r in _history():
            x = {k: r.get(k) for k in ('version', 'tag', 'date', 'change_kind', 'writing_code_id', 'writing_files_changed', 'changed_files', 'pending')}
            x.update(commit=r.get('commit') or r.get('source_commit'), summary=r.get('summary') or r.get('summaries') or [], node_id=next((n['id'] for n in nodes if r.get('tag') in n['releases']), None), metrics={'assigned': 0, 'total': 0})
            releases.append(x)
        warnings.append('历史运行配置未知；标题和日期仅供人工参考。')
        return {'schema_version': 1, 'current_version': VERSION, 'objective': '观察不同写作方案在固定成熟窗口的公开表现，不作因果判断。', 'horizon': horizon, 'horizons': [7, 14, 30, 90], 'nodes': nodes, 'releases': releases, 'edges': [{'source': nodes[i - 1]['id'], 'target': n['id'], 'label': '演进'} for i, n in enumerate(nodes) if i], 'articles': articles, 'summary': {'nodes': len(nodes), 'articles': len(articles)}, 'warnings': warnings}


