# ============================================================
# core/topic_ledger.py — 已发布问题台账（跨轮选题去重）
#
# 背景：select_topic 的 avoid 集合只在单次运行内生效；换一天再跑，
# 推荐页又会推出同一个热题——生成一整篇后才发现「此问题下已答过」，
# 白白浪费一轮生成（发布端 check_answerable 只能兜底跳过）。
#
# 本模块把每次成功发布的问题 (url, title, date) 追加到
# data/state/published_topics.jsonl；选题时读入并入 avoid 基线。
# 读取按天修剪（默认保留 90 天），损坏行跳过，绝不因台账故障
# 阻断主流程。
# ============================================================
import datetime
import json
import logging
import pathlib

from core import paths

log = logging.getLogger(__name__)

_MAX_AGE_DAYS = 90


def _ledger_path():
    return pathlib.Path(paths.data("data", "state",
                                   "published_topics.jsonl"))


def load_seen_urls(max_age_days=_MAX_AGE_DAYS):
    """返回近期已发布问题的 url 集合；文件不存在/损坏时返回空集。"""
    fp = _ledger_path()
    if not fp.exists():
        return set()
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=max_age_days)).isoformat()
    seen = set()
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # 损坏行跳过
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("date") or "") >= cutoff:
                    url = rec.get("url")
                    if url:
                        seen.add(url)
    except OSError:
        log.debug("台账读取失败，按空集处理", exc_info=True)
    return seen


def record(url, title=""):
    """追加一条发布记录；写失败仅告警不抛出（不影响发布结果）。"""
    if not url:
        return
    rec = {"url": url, "title": title,
           "date": datetime.date.today().isoformat()}
    fp = _ledger_path()
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("已发布台账写入失败（不影响本次发布）", exc_info=True)
