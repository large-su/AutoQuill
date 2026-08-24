"""草稿箱素材管理 — 数据层。

负责：抓取知乎创作中心草稿箱（回答草稿）快照 data/drafts_*.json、
预览/筛选/统计、以及「从知乎删除草稿」（批量，确认后调用）。
零 UI 依赖；由 webui/server.py 的 /api/drafts 系列端点调用。
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from webui import _snapshot as _snap
from webui.published import _norm_date

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"


# 抽草稿卡 JS：复用 CreationManage-CreationCard（草稿卡与已发布卡同构）
_EXTRACT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('.CreationManage-CreationCard').forEach(card => {
    const editA = card.querySelector('a[href*="/question/"][href*="#write"]');
    const qm = editA ? editA.href.match(/question\/(\d+)/) : null;
    // 知乎改版（2026-08）：标题在 .CreationCardTitle-wrapper 下的 div 里，
    // 时间为普通 div 文本「编辑于 …」（无 data-tooltip），正文直接写在
    // .CreationCardContent-text 里（无 span）。
    const titleEl = card.querySelector('.CreationCardTitle-wrapper');
    const timeEl = Array.from(card.querySelectorAll('div'))
      .find(d => /^编辑于/.test((d.innerText || '').trim()));
    const textEl = card.querySelector('.CreationCardContent-text');
    out.push({
      qid: qm ? qm[1] : '',
      url: editA ? editA.href : '',
      title: titleEl ? (titleEl.innerText || '').trim() : '',
      updated: timeEl ? (timeEl.innerText || '').trim() : '',
      content: textEl ? (textEl.innerText || '').trim() : '',
    });
  });
  return out;
}
"""


def _rel_to_date(s):
    """把知乎卡片上的相对时间（「5 小时前/昨天/3 天前」）近似成 YYYY-MM-DD。

    遇到过绝对时间（2026-08-22 14:47 / 08-22 14:47）时交给 _norm_date；
    无法识别返回 ''（不影响主流程）。
    """
    s = (s or "").strip()
    if not s or re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return _norm_date(s)
    # 相对时间常带时刻后缀：「昨天 20:21」「3 天前 12:00」
    time_suffix = re.sub(r"\s+\d{1,2}:\d{2}$", "", s)
    s = time_suffix
    from datetime import timedelta
    now = datetime.now()
    m = re.match(r"^(\d+)\s*分钟前$", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"^(\d+)\s*小时前$", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")
    if s == "昨天":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if s == "前天":
        return (now - timedelta(days=2)).strftime("%Y-%m-%d")
    m = re.match(r"^(\d+)\s*天前$", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"^(\d+)\s*周前$", s)
    if m:
        return (now - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"^(\d+)\s*个月前$", s)
    if m:
        return (now - timedelta(days=30 * int(m.group(1)))).strftime("%Y-%m-%d")
    if s in ("刚刚", "几分钟前"):
        return now.strftime("%Y-%m-%d")
    return ""


def _draft_html_text(html_text):
    """服务端草稿 HTML → 纯文本（剥标签 + 还原实体）。

    草稿卡正文只是约 200 字的固定摘要，字数统计与全文查看必须用
    /api/v4/questions/{qid}/draft 返回的完整 HTML。
    """
    if not html_text:
        return ""
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()


def _normalize_row(r):
    title = (r.get("title") or "").strip()
    content = (r.get("content") or "").strip()
    updated = (r.get("updated") or "").strip()
    # 时间文本形如「编辑于 08-22 14:47」（旧版）或「编辑于 5 小时前」（新版）
    updated = re.sub(r"^编辑于\s*", "", updated)
    return {
        "qid": str(r.get("qid") or ""),
        "url": r.get("url") or "",
        "title": title,
        "updated": updated,
        "updated_date": _rel_to_date(updated),
        "content": content,
        "chars": len(re.sub(r"\s+", "", content)),
    }


def _latest_file():
    """返回最新的 drafts_*.json（按修改时间），无则 None。"""
    return _snap.latest_file(_DATA_DIR, "drafts_*.json")


def _coerce_draft_row(r):
    """草稿行归一化：新格式（已含 updated_date）原样，旧格式走 _normalize_row。"""
    if "updated_date" not in r:
        r = _normalize_row(r)
    return r


def _draft_quality(rows):
    """快照质量：草稿总字符数（全空视为异常，回退上一份）。"""
    return sum(r["chars"] or 0 for r in rows)


def load() -> dict:
    """读取最新草稿快照（最新为空/损坏时回退上一份）。"""
    return _snap.load_snapshot(
        _DATA_DIR, "drafts_*.json",
        coerce_row=_coerce_draft_row, id_keys=("qid",),
        quality_of=_draft_quality)


def filter_rows(rows: list[dict], q="", start="", end="", min_chars=0,
                max_chars=0, sort="updated", direction="desc") -> list[dict]:
    """筛选 + 搜索 + 排序。q 匹配标题/正文；start/end 为 YYYY-MM-DD（含）。
    min/max_chars：字数范围（>0 生效）。sort: updated | chars。"""
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        if q and q not in r["title"].lower() and q not in r["content"].lower():
            continue
        if min_chars and r["chars"] < min_chars:
            continue
        if max_chars and r["chars"] > max_chars:
            continue
        d = r["updated_date"]
        if start and d and d < start:
            continue
        if end and d and d > end:
            continue
        out.append(r)
    key = {
        "updated": lambda r: (r["updated_date"] or "0000-00-00",
                              r["chars"]),
        "chars": lambda r: (r["chars"], r["updated_date"] or "0000-00-00"),
    }.get(sort, lambda r: r["updated_date"] or "0000-00-00")
    out.sort(key=key, reverse=(direction == "desc"))
    return out


def summarize(rows) -> dict:
    if not rows:
        return {"total": 0, "sum_chars": 0, "avg_chars": 0,
                "date_min": "", "date_max": ""}
    dates = [r["updated_date"] for r in rows if r["updated_date"]]
    chars = sum(r["chars"] for r in rows)
    return {
        "total": len(rows),
        "sum_chars": chars,
        "avg_chars": int(chars / len(rows)) if rows else 0,
        "date_min": min(dates) if dates else "",
        "date_max": max(dates) if dates else "",
    }


def scrape(progress=None, stop_flag=None):
    """抓取草稿箱（回答草稿）快照落盘。返回归一化 rows；空结果不落盘。"""
    from applications.zhihu_story.browser_adapter import ZhihuBrowser, _check_cancel

    def _count(b):
        return b._safe_evaluate(
            "() => document.querySelectorAll('.CreationManage-CreationCard').length") or 0

    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        if progress:
            progress("打开草稿箱…", None)
        b.page.goto(_DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        prev = _count(b)
        stable = 0
        for i in range(1, 60):
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
            if progress and i % 6 == 0:
                progress(f"已加载 {cur} 个草稿…", None)
        if progress and prev > 0:
            progress(f"已加载 {prev} 个草稿，正在提取…", None)

        raw = b._safe_evaluate(_EXTRACT_JS) or []
        if stop_flag and stop_flag():
            return []
        rows = [_normalize_row(r) for r in raw if r.get("qid")]
        seen, dedup = set(), []
        for r in rows:
            if r["qid"] in seen:
                continue
            seen.add(r["qid"])
            dedup.append(r)

        if not dedup:
            log.warning("草稿箱抓取结果为空（可能未登录或页面结构变化），保留上次快照")
            if progress:
                progress("未抓取到草稿（可能未登录/页面改版），已保留上次快照", None)
            return []

        # ★ 卡片正文只是固定长度摘要（约 200 字），字数统计/全文查看必须
        # 取服务端草稿全文：逐个请求 /api/v4/questions/{qid}/draft
        # （与发布确认同一接口，凭当前登录态；失败时保留卡片摘要不阻断）
        full_ok = 0
        for i, row in enumerate(dedup):
            if stop_flag and stop_flag():
                break
            _check_cancel()
            try:
                draft_html = b.get_draft_content(row["qid"])
            except Exception as exc:
                log.warning("草稿全文获取失败 qid=%s：%s", row["qid"], exc)
                draft_html = ""
            full_text = _draft_html_text(draft_html)
            if full_text:
                row["content"] = _html.unescape(full_text)
                row["chars"] = len(re.sub(r"\s+", "", row["content"]))
                full_ok += 1
            if progress and (i + 1) % 6 == 0:
                progress(f"已获取全文 {i + 1}/{len(dedup)} 个…", None)
        log.info("草稿全文获取完成：%d/%d 篇", full_ok, len(dedup))

        path = _DATA_DIR / f"drafts_{datetime.now():%Y-%m-%d}.json"
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dedup, f, ensure_ascii=False, indent=2)
        if progress:
            progress(f"已抓取 {len(dedup)} 个草稿，已保存", 100)
        log.info("草稿箱抓取完成：%d 个 → %s", len(dedup), path)
        return dedup
    finally:
        b.close()


# 删除草稿：在草稿卡上找删除按钮（按 qid 匹配编辑链接）→ 点确认弹窗
_CLICK_DRAFT_DEL = """() => {
  const clean = s => (s||'').replace(/[\u200b-\u200d\ufeff]/g, '').trim();
  const qid = 'QID';
  const cards = document.querySelectorAll('.CreationManage-CreationCard');
  for (const card of cards) {
    const a = card.querySelector('a[href*="/question/"][href*="#write"]');
    if (a && a.href.indexOf('/question/' + qid) >= 0) {
      const btn = Array.from(card.querySelectorAll('button'))
        .find(b => clean(b.innerText) === '删除');
      if (btn) { btn.click(); return true; }
      return false;
    }
  }
  return false;
}"""

_CLICK_CONF = """() => {
  const clean = s => (s||'').replace(/[\u200b-\u200d\ufeff]/g, '').trim();
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

_GONE_JS = """() => {
  const qid = 'QID';
  return !Array.from(document.querySelectorAll('.CreationManage-CreationCard')).some(card => {
    const a = card.querySelector('a[href*="/question/"][href*="#write"]');
    return a && a.href.indexOf('/question/' + qid) >= 0;
  });
}"""


def delete_drafts(qids, progress=None, stop_flag=None):
    """从知乎草稿箱删除指定回答草稿（不可逆）。★ 由界面显式确认后调用。
    单页处理：定位卡片→点删除→确认弹窗→校验消失。单条失败自动跳过继续。"""
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    b = ZhihuBrowser(headless=True)
    deleted = []
    try:
        b.start()
        if progress:
            progress("打开草稿箱…", None)
        b.page.goto(_DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        for _ in range(4):
            b._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.5)
        skipped = errors = 0
        for i, qid in enumerate(qids, 1):
            if stop_flag and stop_flag():
                log.info("草稿删除任务被中止：已删除 %d 个", len(deleted))
                break
            log.info("草稿删除 %d/%d：qid=%s 开始处理", i, len(qids), qid)
            if progress:
                progress(f"第 {i}/{len(qids)} 个：删除草稿 {qid}…", None)
            try:
                if not b._safe_evaluate(_CLICK_DRAFT_DEL.replace("QID", qid)):
                    skipped += 1
                    log.warning("草稿删除 %d/%d：qid=%s 未找到删除按钮，跳过", i, len(qids), qid)
                    if progress:
                        progress(f"第 {i}/{len(qids)} 个：未找到该草稿，跳过", None)
                    continue
                time.sleep(1.2)
                conf = b._safe_evaluate(_CLICK_CONF)
                time.sleep(1.5)
                gone = bool(b._safe_evaluate(_GONE_JS.replace("QID", qid)))
                if conf or gone:
                    deleted.append(qid)
                    log.info("草稿删除 %d/%d：✓ qid=%s 已删除", i, len(qids), qid)
                    if progress:
                        progress(f"✓ 已删除第 {i}/{len(qids)} 个（{qid}）", None)
                else:
                    skipped += 1
                    log.warning("草稿删除 %d/%d：qid=%s 已点删除但未确认，跳过", i, len(qids), qid)
                    if progress:
                        progress(f"第 {i}/{len(qids)} 个：未确认删除结果，跳过", None)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("草稿删除 %d/%d：qid=%s 处理异常已跳过：%s",
                            i, len(qids), qid, exc)
                if progress:
                    progress(f"第 {i}/{len(qids)} 个：处理异常已跳过", None)
        log.info("草稿删除结束：成功 %d / 跳过 %d / 异常 %d / 共 %d",
                 len(deleted), skipped, errors, len(qids))
        if progress:
            progress(f"完成：共 {len(qids)} 个，已删除 {len(deleted)}，"
                     f"跳过 {skipped}，异常 {errors}", None)
    finally:
        b.close()
    return deleted
