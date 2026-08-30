# ============================================================
# core/feedback_loop.py — 发布数据反馈闭环（实现版）
#
# 数据流：
#   发布时   record_story_published(url, title, meta)
#              → topic_ledger.record 合流（version/aid/genre/story_file/session_id）
#   看板抓取 attach_snapshot_rows(rows) / attach_performance(...)
#              → data/state/story_performance.jsonl（每条=一篇的一次观测）
#   回填     seed_from_snapshots()（一次性把历史 published_answers_*.json 入账）
#   消费     summarize() → 题材级互动先验；topic_genre_multiplier() → 选题加权
#
# 评分口径沿用 config.story.READER_SCORE_*：
#   篇目分 = 衰减 * (赞1.0/天 + 评论3.0/天 + 收藏2.5/天 + 喜欢2.0/天)
#   衰减 = (REF_AGE_DAYS / (REF_AGE_DAYS + 发布天数)) ^ DECAY_EXPONENT
# 任何读/写/解析失败都只告警，绝不阻断主流程（发布/抓取照常）。
# ============================================================

import datetime
import json
import logging
import pathlib
import re
import statistics
import time

from core import paths, topic_ledger

log = logging.getLogger(__name__)

_PERF_NAME = "story_performance.jsonl"
_SEED_MIN_N = 2          # 题材先验最少观测数，不足回落全局中位
_CACHE_TTL_SECONDS = 600.0

_cache = {"mtime_ns": None, "at": 0.0, "data": None}


def _perf_path():
    return pathlib.Path(paths.data("data", "state", _PERF_NAME))


# ============================================================
# 发布落账
# ============================================================

def record_story_published(url, title="", meta=None):
    """一篇故事成功发布（写入草稿）时调用。

    与 topic_ledger.record 合流，避免双写：落账字段含
    version/aid/genre/story_file/session_id（meta 有值才写）。
    """
    return topic_ledger.record(url, title, meta)


# ============================================================
# 表现观测（时间序列）
# ============================================================

def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _norm_date_field(s):
    """把快照的 publish / publish_date 归一成 YYYY-MM-DD；无法解析返回 ''。"""
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{2})-(\d{2})", s)
    if m:
        now = datetime.date.today()
        y = now.year
        cand = f"{y}-{m.group(1)}-{m.group(2)}"
        try:
            if datetime.datetime.strptime(cand, "%Y-%m-%d").date() > now:
                y -= 1
                cand = f"{y}-{m.group(1)}-{m.group(2)}"
        except ValueError:
            pass
        return cand
    return ""


def attach_performance(url, likes=None, reads=None, comments=None,
                       collects=None, favors=None, aid=None, title="",
                       publish_date="", observed=None, genre=None):
    """追加一次互动观测（幂等：同 url+同观测日+同指标不重复写）。

    返回 True 表示本次实际写入新观测；命中去重（跳过）或 url 缺失
    返回 False。写失败仅告警不抛出。
    """
    if not url:
        return False
    obs = observed or datetime.date.today().isoformat()
    rec = {"url": str(url), "aid": aid or None}
    if title:
        rec["title"] = str(title)
    if publish_date:
        rec["publish_date"] = str(publish_date)
    if genre:
        rec["genre"] = str(genre)
    rec.update({
        "observed": obs,
        "reads": _num(reads),
        "likes": _num(likes),
        "comments": _num(comments),
        "collects": _num(collects),
        "favors": _num(favors),
    })
    fp = _perf_path()
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        if fp.exists():
            key = (rec["url"], rec["observed"],
                   rec["reads"], rec["likes"], rec["comments"],
                   rec["collects"], rec["favors"])
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    old = json.loads(line)
                except ValueError:
                    continue
                if (old.get("url") == key[0] and old.get("observed") == key[1]
                        and _num(old.get("reads")) == key[2]
                        and _num(old.get("likes")) == key[3]
                        and _num(old.get("comments")) == key[4]
                        and _num(old.get("collects")) == key[5]
                        and _num(old.get("favors")) == key[6]):
                    return False  # 已存在，幂等跳过（不算新观测）
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        log.warning("表现台账写入失败（不影响主流程）", exc_info=True)
        return False


def _genre_of_record(r):
    from core.detectors import classify_genre
    return classify_genre((r.get("title") or "") + " "
                          + (r.get("content") or ""))


def attach_snapshot_rows(rows, observed=None):
    """把一份看板快照（两种列格式均兼容）逐条入表现台账。

    返回写入条数；解析/写失败静默跳过单条。
    """
    n = 0
    for r in rows or []:
        try:
            if isinstance(r.get("metrics"), dict):
                m = r["metrics"] or {}
                rec = {
                    "url": r.get("url") or "",
                    "aid": str(r.get("aid") or "") or None,
                    "title": r.get("title") or "",
                    "publish_date": _norm_date_field(r.get("publish")),
                    "reads": _num(m.get("阅读")),
                    "likes": _num(m.get("赞同")),
                    "comments": _num(m.get("评论")),
                    "collects": _num(m.get("收藏")),
                    "favors": _num(m.get("喜欢")),
                }
            else:
                rec = {
                    "url": r.get("url") or "",
                    "aid": str(r.get("aid") or "") or None,
                    "title": r.get("title") or "",
                    "publish_date": (r.get("publish_date")
                                     or _norm_date_field(r.get("publish"))),
                    "reads": r.get("reads"),
                    "likes": r.get("likes"),
                    "comments": r.get("comments"),
                    "collects": r.get("collects"),
                    "favors": r.get("favors"),
                }
            rec["genre"] = r.get("genre") or _genre_of_record(r)
            if attach_performance(observed=observed, **rec):
                n += 1
        except Exception:
            log.debug("快照单条入账失败，跳过", exc_info=True)
            continue
    return n


def seed_from_snapshots(data_dir=None, verbose=False):
    """把 data/ 下全部 published_answers_*.json 回填进表现台账（按文件
    时间从新到旧；观测日取文件名日期）。幂等：重复执行不会产生重复观测
    （同 url+同观测日+同指标 跳过）。返回入账条数。
    """
    d = pathlib.Path(data_dir or paths.data("data"))
    files = sorted(d.glob("published_answers_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for fp in files:
        m = re.search(r"published_answers_(\d{4}-\d{2}-\d{2})\.json$",
                      fp.name)
        observed = m.group(1) if m else fp.stat().st_mtime
        if not isinstance(observed, str):
            observed = datetime.date.fromtimestamp(observed).isoformat()
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rows, list):
            continue
        n = attach_snapshot_rows(rows, observed=observed)
        total += n
        if verbose:
            log.info("回填 %s：%d 条", fp.name, n)
    return total


# ============================================================
# 表现台账读取与题材先验
# ============================================================

def _load_performance():
    fp = _perf_path()
    out = []
    if not fp.exists():
        return out
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("url"):
                out.append(rec)
    except OSError:
        log.debug("表现台账读取失败（按空处理）", exc_info=True)
    return out


def _reader_score(rec, as_of):
    """单篇互动分（按发布天数归一 + 90 天衰减）。as_of: date。"""
    from config.story import (READER_SCORE_W_LIKES, READER_SCORE_W_COMMENTS,
                              READER_SCORE_W_COLLECTS, READER_SCORE_W_HEARTS,
                              READER_SCORE_REF_AGE_DAYS,
                              READER_SCORE_DECAY_EXPONENT)
    pd = rec.get("publish_date") or ""
    if not pd:
        return None
    try:
        y, mo, d = (int(x) for x in pd.split("-"))
        pub = datetime.date(y, mo, d)
    except (ValueError, TypeError):
        return None
    age = max(1, (as_of - pub).days)
    ref = READER_SCORE_REF_AGE_DAYS
    decay = (ref / (ref + age)) ** READER_SCORE_DECAY_EXPONENT
    score = (READER_SCORE_W_LIKES * _num(rec.get("likes")) / age
             + READER_SCORE_W_COMMENTS * _num(rec.get("comments")) / age
             + READER_SCORE_W_COLLECTS * _num(rec.get("collects")) / age
             + READER_SCORE_W_HEARTS * _num(rec.get("favors")) / age)
    return decay * score


def summarize(genre=None, as_of=None, auto_seed=True):
    """题材级互动先验：对每篇取最新观测，按题材聚合 互动分中位数。

    返回 {as_of, n_articles, overall:{score,n}, genres:{题材:{n,score,
    likes_per_day,comments_per_day,collects_per_day,boost_1x}}}。
    genre 传 None 返回全部；传题材名只返回该题材信息。
    观测 < _SEED_MIN_N 的题材回落 overall。任何异常回落空结构。
    """
    if not _perf_path().exists() and auto_seed:
        seed_from_snapshots()
    as_of = as_of or datetime.date.today()
    if not isinstance(as_of, datetime.date):
        try:
            y, mo, d = (int(x) for x in str(as_of).split("-"))
            as_of = datetime.date(y, mo, d)
        except (ValueError, TypeError):
            as_of = datetime.date.today()

    by_url = {}
    for rec in _load_performance():
        by_url.setdefault(rec["url"], []).append(rec)
    arts = []
    for url, recs in by_url.items():
        latest = max(recs, key=lambda r: r.get("observed") or "")
        score = _reader_score(latest, as_of)
        if score is None:
            continue
        g = (latest.get("genre") or "").strip() or _genre_of_record(latest)
        arts.append({
            "url": url, "genre": g, "score": score,
            "likes_per_day": _num(latest.get("likes")) / max(1, (
                (as_of - _pubdate(latest)).days if _pubdate(latest) else 1)),
            "comments_per_day": _num(latest.get("comments")) / max(1, (
                (as_of - _pubdate(latest)).days if _pubdate(latest) else 1)),
            "collects_per_day": _num(latest.get("collects")) / max(1, (
                (as_of - _pubdate(latest)).days if _pubdate(latest) else 1)),
        })
    if not arts:
        return {"as_of": as_of.isoformat(), "n_articles": 0,
                "overall": {"score": 0.0, "n": 0}, "genres": {}}

    def med(xs):
        return statistics.median(xs) if xs else 0.0

    overall = med([a["score"] for a in arts])
    genres = {}
    for g in sorted({a["genre"] for a in arts}):
        sub = [a for a in arts if a["genre"] == g]
        sc = med([a["score"] for a in sub])
        if len(sub) >= _SEED_MIN_N or len(sub) == len(arts):
            use = sc
        else:
            use = overall  # 观测不足，回落全局
        genres[g] = {
            "n": len(sub),
            "score": sc,
            "likes_per_day": med([a["likes_per_day"] for a in sub]),
            "comments_per_day": med([a["comments_per_day"] for a in sub]),
            "collects_per_day": med([a["collects_per_day"] for a in sub]),
            "boost_1x": (sc / overall) if overall else 1.0,
            "effective_score": use,
        }
    return {"as_of": as_of.isoformat(), "n_articles": len(arts),
            "overall": {"score": overall, "n": len(arts)},
            "genres": genres}


def _pubdate(rec):
    pd = rec.get("publish_date") or ""
    try:
        y, mo, d = (int(x) for x in pd.split("-"))
        return datetime.date(y, mo, d)
    except (ValueError, TypeError):
        return None


def _cached_summary():
    fp = _perf_path()
    mtime = fp.stat().st_mtime_ns if fp.exists() else None
    now = time.time()
    if (_cache["data"] is not None and _cache["mtime_ns"] == mtime
            and now - _cache["at"] < _CACHE_TTL_SECONDS):
        return _cache["data"]
    data = summarize(auto_seed=True)
    _cache.update(mtime_ns=mtime, at=now, data=data)
    return data


def topic_genre_multiplier(title, weight=None, min_boost=None,
                           max_boost=None):
    """选题打分的题材先验乘数（P0-B）。

    boost = 1 + weight * (题材先验 / 全局中位 - 1)，clamp 到
    [min_boost, max_boost]；无数据 / 未知题材 / 关闭权重 → 1.0（不干预）。
    默认参数来自 config.story（TOPIC_GENRE_*）。
    """
    from config.story import (TOPIC_GENRE_PRIOR_WEIGHT,
                              TOPIC_GENRE_BOOST_MIN, TOPIC_GENRE_BOOST_MAX)
    w = TOPIC_GENRE_PRIOR_WEIGHT if weight is None else weight
    lo = TOPIC_GENRE_BOOST_MIN if min_boost is None else min_boost
    hi = TOPIC_GENRE_BOOST_MAX if max_boost is None else max_boost
    if not w or w <= 0:
        return 1.0
    try:
        s = _cached_summary()
        if not s or not s.get("n_articles"):
            return 1.0
        g = (title or "").strip()
        from core.detectors import classify_genre
        info = s["genres"].get(classify_genre(g))
        overall = (s.get("overall") or {}).get("score") or 0.0
        if not info or not overall:
            return 1.0
        boost = 1.0 + w * (info["score"] / overall - 1.0)
        return max(lo, min(hi, boost))
    except Exception:
        log.debug("题材先验加权不可用（回退 1.0）", exc_info=True)
        return 1.0
