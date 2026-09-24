# -*- coding: utf-8 -*-
"""已发布故事的效果复盘：发布台账 x 知乎反馈 x 版本时间线 x 正文特征。

与 tools/version_feedback_report.py 的分工：
  · version_feedback_report 看「生成侧」（每轮生成/格式合规/重试/废稿），数据源 logs/；
  · 本工具看「结果侧」（发出去的东西读者买不买账），数据源：
      data/state/published_topics.jsonl   我们发过什么（含版本/稿件路径）
      data/state/story_performance.jsonl  知乎反馈快照（阅读/赞/评/藏，多期观测）
      output/story_*.md                   正文（算篇幅/章节/对话密度等特征）
      git tag 时间线                       发布日期 → 当时跑的版本

口径（重要，别被绝对值骗）：
  · 阅读/天 = 阅读 ÷ 发布到最近观测的天数。不归一化就会被「老文积累时间长」骗：
    2026-08-27 那两篇（3639/3058 阅读）是全表最老的，直接比绝对值等于给老文加分；
  · 只有进过看板快照的回答才有反馈数据：仍在草稿箱、已删、分页没覆盖的条目会
    显示成「未采到」，看榜单前先看这一栏；
  · 样本只有几十条：只看趋势与榜单，不下显著性结论。

用法：
  python tools/published_review.py                # 控制台摘要（默认 60 天）
  python tools/published_review.py --days 90
  python tools/published_review.py --write        # 另写 docs/REVIEW-perf-<今日>.md
  python tools/published_review.py --top 15
"""
import argparse
import datetime
import glob
import io
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "data", "state", "published_topics.jsonl")
PERF = os.path.join(ROOT, "data", "state", "story_performance.jsonl")
OUTDIR = os.path.join(ROOT, "output")

_T = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
_F = re.compile(r"使用已有文件：(.+?\.md)")
_D = re.compile(r"草稿已保存，完成：「(.+?)」")


def _key(text):
    """标题归一化：去空白与标点，避免全半角/问号差异导致匹配失败。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text or "")


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _date(s):
    try:
        return datetime.date(*[int(x) for x in str(s).split("-")[:3]])
    except Exception:
        return None


# ============================================================
# 数据装载
# ============================================================

def load_ledger(days=0):
    """发布台账（去重：同一天同一题只算一次）。"""
    rows, seen = [], set()
    if not os.path.exists(LEDGER):
        return rows
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat() if days else ""
    for line in io.open(LEDGER, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if cutoff and str(rec.get("date") or "") < cutoff:
            continue
        k = (rec.get("date"), _key(rec.get("title")))
        if k in seen:
            continue
        seen.add(k)
        rows.append(rec)
    return rows


def load_perf():
    """知乎反馈：每个回答保留最新一期观测，同时留下全部观测序列。"""
    latest, series = {}, defaultdict(dict)
    if not os.path.exists(PERF):
        return latest, series
    for line in io.open(PERF, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        aid = str(rec.get("aid") or "")
        if not aid or aid == "1":          # aid=1 是历史测试写入的样本行
            continue
        series[aid][str(rec.get("observed") or "")] = rec
        cur = latest.get(aid)
        if cur is None or str(rec.get("observed") or "") >= str(cur.get("observed") or ""):
            latest[aid] = rec
    return latest, series


def load_story_files():
    """日志：题目 -> 稿件文件（稿件名本身不含题目，只有日志能对上）。"""
    mapping = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "logs", "autoquill_*.log"))):
        cur = None
        try:
            handle = io.open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle as f:
            for line in f:
                m = _T.match(line)
                if not m:
                    continue
                fm = _F.search(line)
                if fm:
                    cur = {"time": m.group(1), "file": os.path.basename(fm.group(1))}
                    continue
                dm = _D.search(line)
                if dm and cur and "title" not in cur:
                    cur["title"] = dm.group(1)
                    mapping.setdefault(_key(cur["title"]), cur)
    return mapping


def version_timeline():
    """git tag -> 日期（发布日期落在哪个 tag 之后就算哪个版本）。"""
    out = []
    try:
        txt = subprocess.run(
            ["git", "tag", "-l", "--format=%(refname:short)|%(creatordate:short)"],
            cwd=ROOT, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return out
    for line in (txt or "").splitlines():
        if "|" not in line:
            continue
        tag, date = line.split("|", 1)
        if _date(date.strip()):
            out.append((date.strip(), tag.strip()))
    out.sort()
    return out


def version_for(date, timeline, recorded=""):
    """发布日期 -> 版本：台账有记录就用记录（最准），否则按 tag 时间线推断。

    统一成 vX.Y.Z 形式：台账里历史写法有 "4.7.0" 与 "v4.7.0" 两种，
    不归一化会把同一版拆成两行。
    """
    if recorded:
        tag = str(recorded).strip()
        return tag if tag[:1] in ("v", "V") else "v" + tag
    got = "未打标签(dev)"
    for tag_date, tag in timeline:
        if tag_date <= (date or ""):
            got = tag
        else:
            break
    return got


# ============================================================
# 正文特征 + 题目分类
# ============================================================

_SURNAME = ("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜谢邹苏潘葛"
            "范彭鲁韦马苗方俞任袁柳唐罗高林梁宋郭董程陆霍温裴")
_NAME_RE = re.compile("[" + _SURNAME + "][\u4e00-\u9fff]{1,2}")
_CHAPTER_RE = re.compile(r"^\s*(?:#{1,4}\s*)?\*{0,2}\s*\d{1,2}\s*\*{0,2}\s*$", re.M)
_QUOTE_RE = re.compile(r"[「“][^」”]{2,}[」”]")


def story_feats(path):
    """正文特征：篇幅/章节数/对话密度/开篇是否出现全名或对话。

    这些特征用来回答「哪一版 prompt 写出来的东西更长/更碎」，
    是复盘里唯一能从本地产物直接量化的部分。
    """
    if not path:
        return {}
    if os.path.isabs(path):
        fp = path
    else:
        # 日志里记的是 output/ 下的文件名；台账里记的是绝对路径（两种都要认）
        cand = [os.path.join(OUTDIR, path), os.path.join(ROOT, path)]
        fp = next((p for p in cand if os.path.exists(p)), cand[0])
    if not os.path.exists(fp):
        return {}
    text = io.open(fp, encoding="utf-8").read()
    body = text.strip()
    chars = len(re.sub(r"\s+", "", body))
    lines = [p.strip() for p in body.split(chr(10)) if p.strip()]
    head = "".join(lines[:4])
    quotes = len(_QUOTE_RE.findall(body))
    return {"chars": chars,
            "chapters": len(_CHAPTER_RE.findall(body)),
            "quote_per_100": round(quotes / max(1.0, chars / 100.0), 2),
            "name_in_head": bool(_NAME_RE.search(head)),
            "quote_in_head": bool(_QUOTE_RE.search(head))}


def qtype(title):
    """题目类型：命题作文/微小说、求推荐/书单、观点/讨论、其他。

    2026-09-23 复盘结论：这一栏的效果差距（阅读/天 1.1 vs 14~28）
    比任何正文特征都大，选题环节比写作环节更值钱。
    """
    t = title or ""
    if re.search(r"为开头|以「|写一个|写一篇|微小说|十个字|100字|写一段", t):
        return "命题作文/微小说"
    if re.search(r"推荐|有哪些|有没有|好看|哪些|求", t):
        return "求推荐/书单"
    if re.search(r"为什么|如何|怎么|什么叫做|评价|体验|是不是", t):
        return "观点/讨论"
    return "其他"


# ============================================================
# 组装
# ============================================================

def build_rows(days=60):
    """台账 x 反馈 x 稿件 x 版本 -> 分析行（含阅读/天等归一化指标）。"""
    ledger = load_ledger(days)
    latest, series = load_perf()
    files = load_story_files()
    timeline = version_timeline()
    by_title = defaultdict(list)
    for rec in latest.values():
        by_title[_key(rec.get("title"))].append(rec)
    obs_all = sorted({o for s in series.values() for o in s if o})
    ref_day = _date(obs_all[-1]) if obs_all else datetime.date.today()
    rows = []
    for rec in ledger:
        k = _key(rec.get("title"))
        cands = sorted(by_title.get(k) or [], key=lambda r: str(r.get("publish_date") or ""))
        perf = cands[-1] if cands else None
        ev = files.get(k) or {}
        pub = str((perf or {}).get("publish_date") or rec.get("date") or "")
        d = _date(pub) or ref_day
        exposed = max(1, (ref_day - d).days + 1)
        reads = _num((perf or {}).get("reads"))
        likes = _num((perf or {}).get("likes"))
        row = {"date": rec.get("date"), "title": rec.get("title"),
               "version": version_for(rec.get("date"), timeline, rec.get("version")),
               "qtype": qtype(rec.get("title")),
               "url": rec.get("url"), "aid": (perf or {}).get("aid"),
               "has_perf": perf is not None,
               "publish_date": pub, "observed": (perf or {}).get("observed"),
               "reads": reads, "likes": likes,
               "comments": _num((perf or {}).get("comments")),
               "collects": _num((perf or {}).get("collects")),
               "favors": _num((perf or {}).get("favors")),
               "days": exposed,
               "rpd": reads / exposed, "lpd": likes / exposed,
               "like_rate": (100.0 * likes / reads) if reads else 0.0,
               "collect_rate": (100.0 * _num((perf or {}).get("collects")) / reads) if reads else 0.0,
               "story": rec.get("story_file") or ev.get("file") or "",
               "series": series.get(str((perf or {}).get("aid") or ""), {})}
        row.update(story_feats(row["story"]))
        rows.append(row)
    return rows, obs_all, latest, ledger


# ============================================================
# 报告
# ============================================================

def _med(vals):
    return statistics.median(vals) if vals else 0.0


def _agg(rows, key_fn, top_n=0):
    """按 key_fn 聚合：n / 阅读合计 / 阅读每天中位 / 赞中位 / 点赞率 / 收藏率。"""
    buckets = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(r)
    out = []
    for k, g in buckets.items():
        reads = sum(x["reads"] for x in g)
        likes = sum(x["likes"] for x in g)
        collects = sum(x["collects"] for x in g)
        out.append({"key": k, "n": len(g), "reads": reads,
                    "rpd_med": _med([x["rpd"] for x in g]),
                    "rpd_mean": statistics.mean([x["rpd"] for x in g]) if g else 0.0,
                    "likes_med": _med([x["likes"] for x in g]),
                    "like_rate": (100.0 * likes / reads) if reads else 0.0,
                    "collect_rate": (100.0 * collects / reads) if reads else 0.0,
                    "chars_med": _med([x.get("chars") or 0 for x in g]),
                    "first_date": min((x.get("date") or "") for x in g)})
    out.sort(key=lambda d: d["first_date"])
    return out[:top_n] if top_n else out


def render(rows, obs_all, latest, top_n=12):
    """复盘报告（markdown 行）。数字全部来自本地数据，不臆造。"""
    perf = [r for r in rows if r["has_perf"]]
    miss = [r for r in rows if not r["has_perf"]]
    head = [r for r in perf if r.get("story")]
    L = []
    L.append("## 1. 数据概览")
    L.append("")
    L.append("- 发布台账（去重后）：**%d** 条；其中有知乎反馈数据的 **%d** 条，"
             "无反馈的 **%d** 条（仍在草稿箱 / 已删 / 看板快照分页未覆盖）" % (
                 len(rows), len(perf), len(miss)))
    L.append("- 反馈观测期：%s（最新一期 %s）" % (
        "、".join(obs_all) or "无", obs_all[-1] if obs_all else "无"))
    L.append("- 台账条目里能对上本地稿件的：**%d/%d**" % (len(head), len(rows)))
    if perf:
        newest = max(r["publish_date"] for r in perf)
        L.append("- 覆盖发布区间：%s ~ %s" % (min(r["publish_date"] for r in perf), newest))
        L.append("- 阅读合计 **%d**，赞同合计 **%d**，收藏合计 **%d**" % (
            sum(r["reads"] for r in perf), sum(r["likes"] for r in perf),
            sum(r["collects"] for r in perf)))
    L.append("")
    L.append("## 2. 按版本（迭代阶段）聚合")
    L.append("")
    L.append("| 版本 | 发布 | 有反馈 | 阅读合计 | 阅读/天中位 | 赞中位 | 点赞率 | 收藏率 | 篇幅中位 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for a in _agg(perf, lambda r: r["version"]):
        L.append("| %s | %d | %d | %d | %.1f | %.1f | %.2f%% | %.2f%% | %.0f |" % (
            a["key"], a["n"], a["n"], a["reads"], a["rpd_med"], a["likes_med"],
            a["like_rate"], a["collect_rate"], a["chars_med"]))
    L.append("")
    L.append("> 阅读/天 = 阅读 ÷（发布日到最近观测日的天数）——不归一化会让「老文」白占便宜。")
    L.append("")
    L.append("## 3. 按题型聚合（选题环节的效果差距）")
    L.append("")
    L.append("| 题型 | n | 阅读/天中位 | 阅读/天均值 | 赞中位 | 阅读合计 |")
    L.append("|---|---|---|---|---|---|")
    for a in sorted(_agg(perf, lambda r: r["qtype"]), key=lambda d: -d["rpd_med"]):
        L.append("| %s | %d | %.1f | %.1f | %.1f | %d |" % (
            a["key"], a["n"], a["rpd_med"], a["rpd_mean"], a["likes_med"], a["reads"]))
    L.append("")
    L.append("## 4. 榜单（按阅读/天，消除新旧差异）")
    L.append("")
    L.append("| 发布 | 题目 | 版本 | 阅读 | 赞 | 评 | 藏 | 阅读/天 | 篇幅 | 稿件 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(perf, key=lambda x: -x["rpd"])[:top_n]:
        L.append("| %s | %s | %s | %d | %d | %d | %d | %.1f | %s | %s |" % (
            r["publish_date"], (r["title"] or "")[:30], r["version"], r["reads"],
            r["likes"], r["comments"], r["collects"], r["rpd"],
            r.get("chars") or "-", os.path.basename(r.get("story") or "") or "-"))
    L.append("")
    L.append("尾部（有阅读但零赞）：")
    L.append("")
    for r in sorted([x for x in perf if x["likes"] == 0], key=lambda x: -x["reads"])[:8]:
        L.append("- %s %s（阅读 %d、藏 %d、%s）" % (
            r["publish_date"], (r["title"] or "")[:32], r["reads"], r["collects"], r["version"]))
    L.append("")
    L.append("## 5. 没有反馈数据的条目（未公开 / 已删 / 快照没覆盖）")
    L.append("")
    for r in miss[:20]:
        L.append("- %s %s" % (r["date"], (r["title"] or "(无题)")[:40]))
    if len(miss) > 20:
        L.append("- …… 其余 %d 条见台账" % (len(miss) - 20))
    L.append("")
    L.append("## 6. 与账号基线的对照")
    L.append("")
    ours = [r["rpd"] for r in perf]
    allr = []
    for rec in latest.values():
        d = _date(rec.get("publish_date"))
        if not d:
            continue
        ref = _date(obs_all[-1]) if obs_all else datetime.date.today()
        expose = max(1, (ref - d).days + 1)
        allr.append(_num(rec.get("reads")) / expose)
    if ours and allr:
        L.append("- 我们发的内容：n=%d，阅读/天中位 **%.1f**、均值 %.1f" % (
            len(ours), _med(ours), statistics.mean(ours)))
        L.append("- 账号全部回答：n=%d，阅读/天中位 %.1f、均值 %.1f" % (
            len(allr), _med(allr), statistics.mean(allr)))
    L.append("")
    return L


def main():
    ap = argparse.ArgumentParser(description="已发布故事的效果复盘（台账 x 反馈 x 版本）")
    ap.add_argument("--days", type=int, default=60, help="只看最近 N 天发布（0 = 全部）")
    ap.add_argument("--top", type=int, default=12, help="榜单条数")
    ap.add_argument("--write", action="store_true", help="另写 docs/REVIEW-perf-<今日>.md")
    args = ap.parse_args()

    rows, obs_all, latest, ledger = build_rows(args.days)
    if not rows:
        print("没有台账数据：data/state/published_topics.jsonl 为空？")
        return 1
    lines = render(rows, obs_all, latest, args.top)
    print(chr(10).join(lines))
    if args.write:
        today = datetime.date.today().isoformat()
        path = os.path.join(ROOT, "docs", "REVIEW-perf-%s.md" % today)
        header = ["# 已发布故事效果复盘（%s，近 %d 天）" % (today, args.days),
                  "",
                  "> 生成：tools/published_review.py（数据源：发布台账 + 知乎反馈快照",
                  "> data/published_answers_*.json / data/state/story_performance.jsonl",
                  "> + output/ 正文 + git tag 时间线）", ""]
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(chr(10).join(header + lines) + chr(10))
        print()
        print("已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())