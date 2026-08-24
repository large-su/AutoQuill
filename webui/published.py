"""已发布内容数据看板数据层。

负责：读取知乎创作中心已发布内容快照（data/published_answers_*.json）、
从创作中心完整滚动抓取并落盘、数值/日期归一化、筛选搜索排序、汇总。
零 UI 依赖；由 webui/server.py 的 /api/dashboard 系列端点调用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from core import paths
from webui import _snapshot as _snap

log = logging.getLogger(__name__)

# 用户数据目录：源码态=项目根/data，安装版=%APPDATA%/AutoQuill/data
# （不能用 __file__ 相对路径——安装版会把快照写进 _internal 安装目录）
_DATA_DIR = Path(paths.data("data"))
_URL = "https://www.zhihu.com/creator/manage/creation/answer"

# 抽卡片 JS：每张 .CreationManage-CreationCard 提取 id/标题/发布/正文/互动
_EXTRACT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('.CreationManage-CreationCard').forEach(card => {
    const a = card.querySelector('a[href*="/answer/"]');
    const href = a ? a.href : '';
    const m = href.match(/answer\/(\d+)/);
    const titleEl = card.querySelector('.CreationCardTitle-wrapper span');
    const pubEl = card.querySelector('[data-tooltip]');
    const textEl = card.querySelector('.CreationCardContent-text span');
    const metrics = {};
    card.querySelectorAll('div').forEach(d => {
      const t = (d.innerText || '').replace(/\s+/g, '').trim();
      if (/^(阅读|赞同|评论|收藏|喜欢)$/.test(t) && !metrics[t]) {
        const parent = d.parentElement;
        let valEl = null;
        if (parent) {
          const kids = Array.from(parent.querySelectorAll(':scope > div, :scope > span'));
          const idx = kids.indexOf(d);
          valEl = kids[idx - 1] || kids[idx + 1] || parent.querySelector('div');
        }
        metrics[t] = valEl ? (valEl.innerText || '').replace(/[\u200b-\u200d\ufeff]/g, '').trim() : '';
      }
    });
    // 兜底：整卡文本按「值+标签 / 标签+值」抓取，兼容知乎页面结构小版本差异
    if (Object.keys(metrics).length < 5) {
      const text = (card.innerText || '').replace(/\s+/g, '').replace(/[\u200b-\u200d\ufeff]/g, '');
      const grab = (label) => {
        const hit = text.match(new RegExp('(\\d+(?:\\.\\d+)?(?:万|千|w|k|W|K)?)' + label))
                 || text.match(new RegExp(label + '(\\d+(?:\\.\\d+)?(?:万|千|w|k|W|K)?)'));
        return hit ? hit[1] : '';
      };
      ['阅读', '赞同', '评论', '收藏', '喜欢'].forEach((l) => { if (!metrics[l]) metrics[l] = grab(l); });
    }
    out.push({
      aid: m ? m[1] : '',
      url: href,
      title: titleEl ? (titleEl.innerText || '').trim() : '',
      publish: pubEl ? (pubEl.getAttribute('data-tooltip') || '').replace(/^发布于\s*/, '') : '',
      content: textEl ? (textEl.innerText || '').trim() : '',
      metrics: metrics,
    });
  });
  return out;
}
"""


def _to_number(v):
    """把互动值解析为整数：'30'->30，'1.1 万'->11000，'2.3 千'->2300。"""
    if v is None:
        return 0
    s = str(v).replace(" ", "").strip()
    if not s:
        return 0
    m = re.match(r"^([\d.]+)(万|千|[wWkK]?)$", s)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return 0
    n = float(m.group(1))
    unit = m.group(2)
    if unit in ("万", "w", "W"):
        n *= 10000
    elif unit in ("千", "k", "K"):
        n *= 1000
    return int(n)


# 近期 tooltip 形如 "08-22 14:47"（无年份），较早形如 "2022-01-02 13:21"。
# 无年份的按最近 2 年推断：取当前年；若未来于今天则归属前一年。
_FULL_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_MMDD = re.compile(r"^(\d{2})-(\d{2})")


def _norm_date(s):
    """归一化为 YYYY-MM-DD（用于排序/筛选），无法识别返回 ''。"""
    s = (s or "").strip()
    m = _FULL_DATE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _MMDD.match(s)
    if m:
        now = datetime.now()
        y = now.year
        cand = f"{y}-{m.group(1)}-{m.group(2)}"
        try:
            if datetime.strptime(cand, "%Y-%m-%d") > now.replace(hour=23, minute=59):
                y -= 1
                cand = f"{y}-{m.group(1)}-{m.group(2)}"
        except ValueError:
            pass
        return cand
    return ""


# 题材规则（标题 + 正文开头命中即归入；未命中为「其他」）。
# 与前端历史规则保持一致，改这里后 /api/dashboard 返回的 genre 同步生效。
GENRE_RULES = [
    ("双男主/耽美", re.compile(r"双男主|耽美|同性|bl|攻受", re.I)),
    ("甜文", re.compile(r"甜|齁甜|高甜|甜宠", re.I)),
    ("虐文/火葬场", re.compile(r"虐|追妻|火葬场|渣|破镜重圆|悔", re.I)),
    ("古言", re.compile(r"古言|古代|古风|宫斗|宅斗|穿越|嫡|王爷|太子", re.I)),
    ("悬疑/灵异", re.compile(r"悬疑|灵异|恐怖|鬼|惊悚|凶杀|连环杀|细思恐极", re.I)),
    ("重生", re.compile(r"重生|重来一世|再来一世|重新活", re.I)),
    ("仙侠/玄幻", re.compile(r"仙侠|玄幻|修仙|修真|御剑|师父|掌门", re.I)),
    ("现言/霸总", re.compile(r"现言|霸总|总裁|豪门|恋爱|男友|老公|青梅", re.I)),
    ("科幻/脑洞", re.compile(r"科幻|末日|未来|机器|外星|ai|丧尸|平行", re.I)),
    ("微小说", re.compile(r"微小说|百字|100字|十个字|七个字|一个词|一句话", re.I)),
]


def genre_of(r):
    """按标题 + 正文开头识别题材，返回规则名或「其他」。"""
    text = ((r.get("title") or "") + " " + (r.get("content") or "")).strip()[:120]
    for name, pattern in GENRE_RULES:
        if pattern.search(text):
            return name
    return "其他"


def _normalize_row(r):
    m = r.get("metrics") or {}
    row = {
        "aid": str(r.get("aid") or ""),
        "url": r.get("url") or "",
        "title": (r.get("title") or "").strip(),
        "publish": (r.get("publish") or "").strip(),
        "publish_date": _norm_date(r.get("publish")),
        "content": (r.get("content") or "").strip(),
        "reads": _to_number(m.get("阅读")),
        "likes": _to_number(m.get("赞同")),
        "comments": _to_number(m.get("评论")),
        "collects": _to_number(m.get("收藏")),
        "favors": _to_number(m.get("喜欢")),
    }
    row["genre"] = genre_of(row)
    return row


def _latest_file():
    """返回最新的 published_answers_*.json（按修改时间），无则 None。"""
    return _snap.latest_file(_DATA_DIR, "published_answers_*.json")


def _coerce_row(r):
    """兼容两种快照行格式：
    - 原始行：含 metrics 字典（老格式）→ 走 _normalize_row
    - 已归一化行：扁平字段 reads/likes/...（当前 scrape 落盘格式）→ 直接补齐 genre
    """
    if isinstance(r.get("metrics"), dict):
        return _normalize_row(r)
    if r.get("genre") is None:
        r = dict(r)
        r["genre"] = genre_of(r)
    return r


def _engagement(rows):
    """快照互动合计，用于判断快照是否「有真实数据」（全 0 视为异常）。"""
    return sum(
        r["likes"] + r["reads"] + r["comments"] + r["collects"] + r["favors"]
        for r in rows)


def load() -> dict:
    """读取看板快照。若最新快照互动数据异常（如页面改版导致全 0），
    自动回退到最近一份有真实互动的快照，避免看板被清空。

    返回 {rows(已归一化), total, generated_at, source_file}。
    """
    return _snap.load_snapshot(
        _DATA_DIR, "published_answers_*.json",
        coerce_row=_coerce_row, id_keys=("aid",),
        quality_of=_engagement)


def filter_rows(rows: list[dict], q="", start="", end="", min_likes=0,
                min_reads=0, min_comments=0, min_collects=0, min_favors=0,
                sort="newest", direction="desc") -> list[dict]:
    """筛选 + 搜索 + 排序。

    q 匹配标题/正文；start/end 为 YYYY-MM-DD（含）。
    各 min_* 门槛：>0 时生效，0 表示不限。
    sort: newest | oldest | likes | reads | comments | collects | favors。
    时间类排序固定新→旧/旧→新；指标类排序默认降序，direction 可传 asc。
    次级排序：指标类取发布时间，时间类取赞同，让同值条目顺序稳定。
    """
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        if q and q not in r["title"].lower() and q not in r["content"].lower():
            continue
        if min_likes and r["likes"] < min_likes:
            continue
        if min_reads and r["reads"] < min_reads:
            continue
        if min_comments and r["comments"] < min_comments:
            continue
        if min_collects and r["collects"] < min_collects:
            continue
        if min_favors and r["favors"] < min_favors:
            continue
        d = r["publish_date"]
        if start and d and d < start:
            continue
        if end and d and d > end:
            continue
        out.append(r)
    sort = sort or "newest"
    metric = {
        "likes": "likes", "reads": "reads", "comments": "comments",
        "collects": "collects", "favors": "favors",
    }.get(sort)
    if metric:
        def key(r):
            return (r[metric], r["publish_date"] or "0000-00-00")
        default_dir = "desc"
    else:
        def key(r):
            return (r["publish_date"] or "0000-00-00", r["likes"], r["reads"])
        default_dir = "asc" if sort == "oldest" else "desc"
    direction = (direction or default_dir).lower()
    if direction not in ("asc", "desc"):
        direction = default_dir
    out.sort(key=key, reverse=(direction == "desc"))
    return out


def summarize(rows) -> dict:
    """汇总统计（供看板 KPI 卡使用）。"""
    if not rows:
        return {"total": 0, "liked": 0, "sum_likes": 0, "sum_reads": 0,
                "sum_comments": 0, "sum_collects": 0, "sum_favors": 0,
                "avg_likes": 0, "avg_reads": 0, "liked_ratio": 0,
                "date_min": "", "date_max": ""}
    n = len(rows)
    likes = sum(r["likes"] for r in rows)
    reads = sum(r["reads"] for r in rows)
    liked = sum(1 for r in rows if r["likes"] > 0)
    dates = [r["publish_date"] for r in rows if r["publish_date"]]
    return {
        "total": n,
        "liked": liked,
        "sum_likes": likes,
        "sum_reads": reads,
        "sum_comments": sum(r["comments"] for r in rows),
        "sum_collects": sum(r["collects"] for r in rows),
        "sum_favors": sum(r["favors"] for r in rows),
        "avg_likes": int(likes / n) if n else 0,
        "avg_reads": int(reads / n) if n else 0,
        "liked_ratio": int(liked * 100 / n) if n else 0,
        "date_min": min(dates) if dates else "",
        "date_max": max(dates) if dates else "",
    }


def poor_and_old(rows, before="", max_likes=5, max_reads=100,
                 max_comments=1, max_collects=0, max_favors=0):
    """筛选「时间久远 + 数据不佳」：before(YYYY-MM-DD) 之前发布，且各项均低于阈值。

    默认阈值即「数据不佳」的默认评判：赞同<=5、阅读<=100、评论<=1、收藏<=0、喜欢<=0。
    返回命中行（已归一化）。
    """
    out = []
    for r in rows:
        d = r["publish_date"]
        if before and (not d or d >= before):
            continue
        if r["likes"] > max_likes:
            continue
        if r["reads"] > max_reads:
            continue
        if r["comments"] > max_comments:
            continue
        if r["collects"] > max_collects:
            continue
        if r["favors"] > max_favors:
            continue
        out.append(r)
    # 按时间旧→新排序，便于批量清
    out.sort(key=lambda r: r["publish_date"])
    return out


def prune_aids(aids):
    """从最新快照移除指定 aid（仅本地看板数据，可重抓恢复）。返回移除数量。"""
    path = _latest_file()
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as exc:
        log.warning("读取快照失败，无法移除：%s", exc)
        return 0
    want = {str(a) for a in aids}
    keep = [r for r in rows if str(r.get("aid")) not in want]
    removed = len(rows) - len(keep)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
    log.info("已发布看板本地移除 %d 条（%s）", removed, path)
    return removed


def delete_zhihu(aids, progress=None, stop_flag=None):
    """从知乎创作中心删除指定答案（不可逆）。★ 仅由界面显式确认后调用。

    对每个 aid：直接打开该回答页（/answer/{aid}）→ 点「设置」→ 下拉里点「删除」
    → 确认弹窗里点「确定/删除」。不依赖创作中心列表滚动，已实测答案页「设置」
    下拉里有「删除」。返回成功删除的 aid 列表。
    """
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    deleted = []
    _CLICK_SET = """() => {
      const clean = s => (s||'').replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
      const btn = Array.from(document.querySelectorAll('button'))
        .find(e => clean(e.innerText) === '设置' && e.offsetParent);
      if (btn) { btn.click(); return true; }
      return false;
    }"""
    _CLICK_DEL = """() => {
      const clean = s => (s||'').replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
      const btn = Array.from(document.querySelectorAll('button'))
        .find(e => clean(e.innerText) === '删除' && e.offsetParent);
      if (btn) { btn.click(); return true; }
      return false;
    }"""
    _CLICK_CONF = """() => {
      const clean = s => (s||'').replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
      const modals = Array.from(document.querySelectorAll(
        '[class*="Modal"], [class*="Dialog"], [class*="modal"], [role="dialog"]'));
      const pool = modals.length
        ? modals.flatMap(m => Array.from(m.querySelectorAll('button')))
        : Array.from(document.querySelectorAll('button'));
      const t = pool.find(e => {
        const c = clean(e.innerText);
        return (c === '删除' || c === '确定' || c === '确认' || c === '是'
                || c.startsWith('删除') || c.startsWith('确认') || c.startsWith('确定'))
               && e.offsetParent;
      });
      if (t) { t.click(); return true; }
      return false;
    }"""
    try:
        b.start()
        skipped = unknown = errors = 0

        _DEAD_JS = ("() => /被删除|已删除|内容不存在|该回答已被删除/"
                    ".test(document.body.innerText || '')")

        def _try_one(aid):
            """尝试删除单条。返回 (outcome, detail)：
            deleted / skip_deleted / skip_menu / unconfirmed / error"""
            try:
                # 页面导航偶发 ERR_ABORTED/超时：重试一次
                try:
                    b.page.goto(f"https://www.zhihu.com/answer/{aid}",
                                wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:  # noqa: BLE001
                    time.sleep(2.0)
                    log.warning("删除任务 %d/%d：%s 页面导航异常（%s），重试一次",
                                i, len(aids), aid, exc)
                    b.page.goto(f"https://www.zhihu.com/answer/{aid}",
                                wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.5)
                # 已删除/不存在的页面：快速识别并跳过，不再浪费时间找按钮
                if b._safe_evaluate(_DEAD_JS):
                    return "skip_deleted", "页面显示已删除/不存在"
                if not b._safe_evaluate(_CLICK_SET):
                    return "skip_menu", "未找到「设置」按钮"
                time.sleep(1.2)
                if not b._safe_evaluate(_CLICK_DEL):
                    # 菜单可能未展开：再次点「设置」并等待后重找
                    time.sleep(0.6)
                    if not b._safe_evaluate(_CLICK_SET):
                        return "skip_menu", "「设置」菜单未展开"
                    time.sleep(1.0)
                    if not b._safe_evaluate(_CLICK_DEL):
                        return "skip_menu", "未找到「删除」"
                conf = b._safe_evaluate(_CLICK_CONF)
                time.sleep(1.5)
                gone = bool(b._safe_evaluate(_DEAD_JS))
                if conf or gone:
                    return "deleted", ""
                return "unconfirmed", "已点「删除」但未确认到结果"
            except Exception as exc:  # noqa: BLE001
                return "error", str(exc)

        for i, aid in enumerate(aids, 1):
            if stop_flag and stop_flag():
                log.info("删除任务被中止：已删除 %d 条", len(deleted))
                break
            log.info("删除任务 %d/%d：回答 %s 开始处理", i, len(aids), aid)
            if progress:
                progress(f"第 {i}/{len(aids)} 条：打开回答 {aid}…", None)
            outcome, detail = _try_one(aid)
            # 「未找到删除」往往只是菜单渲染慢：整条重试一次
            if outcome == "skip_menu":
                log.info("删除任务 %d/%d：%s %s，整条重试一次", i, len(aids), aid, detail)
                if progress:
                    progress(f"第 {i}/{len(aids)} 条：{detail}，重试一次…", None)
                outcome, detail = _try_one(aid)
            if outcome == "deleted":
                deleted.append(aid)
                log.info("删除任务 %d/%d：✓ %s 已删除", i, len(aids), aid)
                if progress:
                    progress(f"✓ 已删除第 {i}/{len(aids)} 条（{aid}）", None)
            elif outcome == "skip_deleted":
                skipped += 1
                log.info("删除任务 %d/%d：%s 已删除/不存在，跳过", i, len(aids), aid)
                if progress:
                    progress(f"第 {i}/{len(aids)} 条：已删除/不存在，跳过", None)
            elif outcome == "skip_menu":
                skipped += 1
                log.warning("删除任务 %d/%d：%s %s，跳过", i, len(aids), aid, detail)
                if progress:
                    progress(f"第 {i}/{len(aids)} 条：{detail}，跳过", None)
            elif outcome == "unconfirmed":
                unknown += 1
                log.warning("删除任务 %d/%d：%s %s，请人工核对", i, len(aids), aid, detail)
                if progress:
                    progress(f"第 {i}/{len(aids)} 条：{detail}，请人工核对", None)
            else:
                errors += 1
                log.warning("删除任务 %d/%d：%s 处理异常已跳过：%s",
                            i, len(aids), aid, detail)
                if progress:
                    progress(f"第 {i}/{len(aids)} 条：处理异常已跳过（{detail[:50]}）", None)
        log.info("删除任务结束：成功 %d / 跳过 %d / 未确认 %d / 异常 %d / 共 %d",
                 len(deleted), skipped, unknown, errors, len(aids))
        if progress:
            progress(f"完成：共 {len(aids)} 条，已删除 {len(deleted)}，"
                     f"跳过 {skipped}，未确认 {unknown}，异常 {errors}", None)
    finally:
        b.close()
    return deleted


def scrape(progress=None, stop_flag=None):
    """从创作中心完整滚动抓取已发布回答，落盘到 data/published_answers_YYYY-MM-DD.json。

    返回归一化后的 rows（同 load）。progress(text, pct) 可选；stop_flag() 返回
    True 时中止。需要已登录的知乎会话（复用 browser_adapter 登录态）。
    """
    from applications.zhihu_story.browser_adapter import ZhihuBrowser, _check_cancel

    def _count(b):
        return b._safe_evaluate(
            "() => document.querySelectorAll('.CreationManage-CreationCard').length") or 0

    # 进度基准：以上次快照条数估算本次收录进度（无快照时用不定状态）
    prev_total = 0
    prev_path = _latest_file()
    if prev_path:
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev_total = len([r for r in json.load(f) if r.get("aid")])
        except Exception:
            prev_total = 0

    def _pct(cur):
        if not prev_total:
            return None
        return min(99, int(cur * 100 / prev_total))

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        if progress:
            progress("打开创作中心内容管理页…", None)
        b.page.goto(_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)

        prev = _count(b)
        stable = 0
        for i in range(1, 100):
            if stop_flag and stop_flag():
                break
            _check_cancel()
            b._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.8)
            cur = _count(b)
            if cur == prev:
                stable += 1
            else:
                stable = 0
            prev = cur
            if stable >= 3:
                break
            if cur >= 790:
                break
            if progress and i % 8 == 0:
                progress(f"已加载 {cur} 条…", _pct(cur))
        if progress and prev > 0:
            progress(f"已加载 {prev} 条（滚动完成），正在提取数据…", None)

        raw = b._safe_evaluate(_EXTRACT_JS) or []
        if stop_flag and stop_flag():
            return []
        rows = [_normalize_row(r) for r in raw if r.get("aid")]
        # 去重
        seen, dedup = set(), []
        for r in rows:
            if r["aid"] in seen:
                continue
            seen.add(r["aid"])
            dedup.append(r)
        # 质量防护：空结果或互动数据明显异常时（未登录 / 页面改版 / 被风控），
        # 不覆盖上次快照，避免看板数据被清零。
        prev_eng = 0
        if prev_path and prev_total:
            try:
                with open(prev_path, encoding="utf-8") as f:
                    for r in json.load(f):
                        rr = _coerce_row(r)
                        prev_eng += (rr["reads"] + rr["likes"] + rr["comments"]
                                     + rr["collects"] + rr["favors"])
            except Exception:
                prev_eng = 0
        new_eng = sum(
            r["likes"] + r["reads"] + r["comments"] + r["collects"] + r["favors"]
            for r in dedup)
        broken = (not dedup) or (new_eng == 0 and len(dedup) > 10) \
                  or (prev_eng > 0 and new_eng < prev_eng * 0.2)
        if broken:
            log.warning(
                "抓取结果质量异常（新 %d 条 / 互动合计 %d，上次 %d），"
                "跳过落盘并保留上次快照",
                len(dedup), new_eng, prev_eng)
            if progress:
                progress("抓取到的互动数据异常（可能未登录/页面改版），已保留上次快照", None)
            return []

        path = _DATA_DIR / f"published_answers_{datetime.now():%Y-%m-%d}.json"
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dedup, f, ensure_ascii=False, indent=2)
        if progress:
            progress(f"已抓取 {len(dedup)} 条，已保存", 100)
        log.info("已发布内容抓取完成：%d 条 → %s", len(dedup), path)
        return dedup
    finally:
        b.close()
