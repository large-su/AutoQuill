# -*- coding: utf-8 -*-
"""版本 × 发布文章 × 知乎反馈 复盘工具（P2 固化，每周一键）。

数据流（全部本地）：
  logs/autoquill_*.log                → 每轮生成事件（时间/文件/标题/格式/重试/废稿）
  data/published_answers_*.json（最新）→ 发布文章的阅读/赞/评/藏（按标题归一化匹配）
  git log                             → 版本时间线（提交时刻 == 当时运行版本）
  结果                                  → 按版本聚合：发布率 / 格式合规 / 重试 / 废稿 / 互动中位

用法：
  python tools/version_feedback_report.py            # 控制台输出摘要
  python tools/version_feedback_report.py --write    # 另写 docs/REVIEW-<今日>.md
  python tools/version_feedback_report.py --days 45  # 只看最近 N 天（默认 60）
"""
import argparse
import datetime
import glob
import io
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
_RE_FILE = re.compile(r"使用已有文件：(.+?\.md)")
_RE_DRAFT = re.compile(r"草稿已保存，完成：「(.+)」\s*$")
_RE_FMT = re.compile(r"格式检测：(\d+)/10 ([✓✗]合规)")
_RE_RETRY = re.compile(r"故事格式不合规.*第 (\d+)/3 次重试")
_RE_DEAD = re.compile(r"多次尝试均未通过格式校验|标记废稿")
_RE_PUB = re.compile(r"✓ 发布成功")
_RE_SCORE = re.compile(r"总分=(\d+)")


def parse_logs(logs_dir="logs"):
    """日志 → 事件列表（每篇生成一个事件，含标题/格式分/重试/废稿）。"""
    events = []
    for path in sorted(glob.glob(os.path.join(logs_dir, "autoquill_*.log"))):
        cur = None
        try:
            lines = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for rline in lines:
                m = _RE_TIME.match(rline)
                if not m:
                    continue
                t = m.group(1)
                fm = _RE_FILE.search(rline)
                if fm:
                    cur = {"log": os.path.basename(path), "time": t,
                           "file": os.path.basename(fm.group(1)),
                           "draft": None, "fmt": None, "retries": 0,
                           "dead": False, "published": False,
                           "score": None}
                    events.append(cur)
                if cur is None:
                    continue
                dm = _RE_DRAFT.search(rline)
                if dm and cur["draft"] is None:
                    cur["draft"] = dm.group(1).strip()
                fmt = _RE_FMT.search(rline)
                if fmt:
                    cur["fmt"] = int(fmt.group(1))
                if _RE_RETRY.search(rline):
                    cur["retries"] += 1
                if _RE_DEAD.search(rline):
                    cur["dead"] = True
                if _RE_PUB.search(rline):
                    cur["published"] = True
                sc = _RE_SCORE.search(rline)
                if sc and cur["score"] is None:
                    cur["score"] = int(sc.group(1))
    return events


def git_timeline(since="2026-06-01", repo=None):
    """git log → [(epoch, subject)]；git 不可用时返回 []。"""
    try:
        out = subprocess.run(
            ["git", "log", "--pretty=%H|%ct|%s",
             "--since=%s" % since, "--date=iso"],
            cwd=repo or os.getcwd(), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=15, check=False)
        if out.returncode != 0:
            return []
        rows = []
        for line in out.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[1].isdigit():
                rows.append((int(parts[1]), parts[2]))
        return rows
    except Exception:
        return []


def version_label(epoch, timeline):
    """时间点归属的版本：取 <= 时刻的最新提交；提交主题若含版本号取
    版本号，否则用日期+首词作开发版标签。timeline 为空时按日期兜底。"""
    # git log 默认新→旧；统一按升序扫描（取最新匹配提交即当时版本）
    ordered = sorted(timeline)
    best = None
    for e, subj in ordered:
        if e <= epoch:
            best = subj
        else:
            break
    if best:
        m = re.search(r"[vV]?\d+\.\d+(\.\d+)?", best)
        if m:
            return m.group(0).upper()
        return best[:24]
    d = datetime.date.fromtimestamp(epoch)
    return "%02d-%02d dev" % (d.month, d.day)


def load_snapshot_latest(data_dir="data"):
    """最新 published_answers_*.json → rows（兼容新旧两种列格式）。"""
    files = sorted(glob.glob(os.path.join(data_dir, "published_answers_*.json")),
                   key=os.path.getmtime, reverse=True)
    if not files:
        return []
    try:
        raw = json.load(open(files[0], encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for r in raw or []:
        if isinstance(r.get("metrics"), dict):
            m = r["metrics"] or {}
            rows.append({
                "url": r.get("url") or "", "title": r.get("title") or "",
                "publish_date": (r.get("publish_date")
                                 or _norm_publish(r.get("publish"))),
                "reads": _num(m.get("阅读")), "likes": _num(m.get("赞同")),
                "comments": _num(m.get("评论")),
                "collects": _num(m.get("收藏")),
                "favors": _num(m.get("喜欢")),
            })
        else:
            rows.append({
                "url": r.get("url") or "", "title": r.get("title") or "",
                "publish_date": (r.get("publish_date")
                                 or _norm_publish(r.get("publish"))),
                "reads": _num(r.get("reads")), "likes": _num(r.get("likes")),
                "comments": _num(r.get("comments")),
                "collects": _num(r.get("collects")),
                "favors": _num(r.get("favors")),
            })
    return rows


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _norm_publish(s):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s or "")
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    m = re.match(r"^(\d{2})-(\d{2})", s or "")
    if m:
        now = datetime.date.today()
        y = now.year
        cand = "%d-%s-%s" % (y, m.group(1), m.group(2))
        try:
            if datetime.datetime.strptime(cand, "%Y-%m-%d").date() > now:
                y -= 1
                cand = "%d-%s-%s" % (y, m.group(1), m.group(2))
        except ValueError:
            pass
        return cand
    return ""


def norm_title(t):
    t = re.sub(r"\.{2,}|\.\.\.|…", "", t or "")
    t = re.sub(r"[\s\u3000]+", "", t)
    return t.rstrip("？！?。，,.;；：: ")


def join(events, rows, days=60):
    """事件 × 快照 按标题归一化匹配；返回附 pub 指标的事件列表。"""
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=days)).isoformat()
    by_title = {}
    for r in rows:
        by_title.setdefault(norm_title(r.get("title")), r)
    out = []
    for e in events:
        e = dict(e)
        if e["time"][:10] < cutoff:
            continue
        nt = norm_title(e.get("draft"))
        pub = by_title.get(nt)
        if not pub:
            # 日志标题常被截断（..结尾）：唯一前缀命中兜底，避免歧义
            hits = [k for k in by_title
                    if len(nt) >= 8 and (nt in k or k in nt)]
            if len(hits) == 1:
                pub = by_title[hits[0]]
        e["pub"] = pub
        out.append(e)
    return out


def aggregate(events, timeline=None):
    """按版本聚合；返回排序后的行列表（打印/写 md 共用）。"""
    timeline = timeline if timeline is not None else git_timeline()
    grouped = defaultdict(lambda: {"gen": 0, "pub": 0, "fmt_ok": 0,
                                   "fmt_chk": 0, "retried": 0, "dead": 0,
                                   "reads": [], "likes": [], "cmts": [],
                                   "cols": [], "pubs": []})
    for e in events:
        ver = (e.get("version")
               or version_label(
                   datetime.datetime.strptime(
                       e["time"], "%Y-%m-%d %H:%M:%S").timestamp(),
                   timeline))
        g = grouped[ver]
        g["gen"] += 1
        if e["fmt"] is not None:
            g["fmt_chk"] += 1
            if e["fmt"] >= 6:
                g["fmt_ok"] += 1
        if e["retries"] > 0:
            g["retried"] += 1
        if e["dead"]:
            g["dead"] += 1
        pub = e.get("pub")
        if pub:
            key = (pub.get("publish_date"), norm_title(pub.get("title")))
            if key not in {p[0] for p in g["pubs"]}:
                g["pubs"].append((key, pub))
            g["reads"].append(_num(pub.get("reads")))
            g["likes"].append(_num(pub.get("likes")))
            g["cmts"].append(_num(pub.get("comments")))
            g["cols"].append(_num(pub.get("collects")))
    rows = []
    for k, v in grouped.items():
        v = dict(v)
        v["pub"] = len(v["pubs"])
        v["version"] = k
        rows.append(v)
    rows.sort(key=lambda x: x["gen"], reverse=True)
    return rows


def _median(xs):
    if not xs:
        return 0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def render_md(rows, days, generated_at, priors_text=""):
    md = []
    md.append("# AutoQuill 版本 × 发布 × 反馈 复盘（%s，近 %d 天）" % (generated_at, days))
    md.append("")
    md.append("| 版本 | 生成 | 发布(率) | 格式合规 | 重试 | 废稿 | 阅读中位 | 赞中位 | 发布代表文章（读/赞） |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for g in rows:
        rate = "%.0f%%" % (g["pub"] / g["gen"] * 100) if g["gen"] else "-"
        reps = "；".join("%s（%d/%d）" % (p[1].get("title", "")[:18],
                                        _num(p[1].get("reads")),
                                        _num(p[1].get("likes")))
                         for p in g["pubs"][:3]) or "-"
        md.append("| %s | %d | %d(%s) | %d/%d | %d | %d | %d | %d | %s |"
                  % (g["version"], g["gen"], g["pub"], rate,
                     g["fmt_ok"], g["fmt_chk"], g["retried"], g["dead"],
                     _median(g["reads"]), _median(g["likes"]), reps))
    if priors_text:
        md.append("")
        md.append("## 题材先验（feedback_loop.summarize）")
        md.append("")
        md.append(priors_text)
    md.append("")
    md.append("> 生成：tools/version_feedback_report.py，数据源 logs/ 与 data/published_answers_*.json")
    return "\n".join(md)


def priors_text():
    try:
        from core import feedback_loop
        s = feedback_loop.summarize(auto_seed=True)
        if not s["n_articles"]:
            return ""
        lines = ["| 题材 | n | 互动分 | 赞/天 | 评/天 | 藏/天 |",
                 "|---|---|---|---|---|---|"]
        for g, info in sorted(s["genres"].items(),
                              key=lambda kv: kv[1]["score"], reverse=True):
            lines.append("| %s | %d | %.3f | %.2f | %.2f | %.2f |"
                         % (g, info["n"], info["score"],
                            info["likes_per_day"],
                            info["comments_per_day"],
                            info["collects_per_day"]))
        return "\n".join(lines)
    except Exception:
        return ""


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--data", default="data")
    ap.add_argument("--write", action="store_true",
                    help="另写 docs/REVIEW-<今日>.md")
    args = ap.parse_args()

    events = parse_logs(args.logs)
    rows = load_snapshot_latest(args.data)
    timeline = git_timeline()
    joined = join(events, rows, days=args.days)
    for e in joined:
        e["version"] = version_label(
            datetime.datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S")
            .timestamp(), timeline)
    agg = aggregate(joined, timeline=timeline)

    print("== 复盘（近 %d 天）==  事件 %d / 快照 %d / 版本 %d" % (
        args.days, len(joined), len(rows), len(agg)))
    for g in agg:
        rate = "%.0f%%" % (g["pub"] / g["gen"] * 100) if g["gen"] else "-"
        print(" %-26s 生成%3d 发布%3d(%5s) 合规%2d/%2d 重试%d 废稿%d | "
              "阅读中位%4d 赞中位%2d" % (
                  g["version"], g["gen"], g["pub"], rate,
                  g["fmt_ok"], g["fmt_chk"], g["retried"], g["dead"],
                  _median(g["reads"]), _median(g["likes"])))
    pt = priors_text()
    if pt:
        print("\n== 题材先验（feedback_loop）==")
        print(pt)

    if args.write:
        gen = datetime.date.today().isoformat()
        doc = "docs/REVIEW-%s.md" % gen
        os.makedirs("docs", exist_ok=True)
        open(doc, "w", encoding="utf-8").write(
            render_md(agg, args.days, gen, priors_text=pt))
        print("\n已写：%s" % doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
