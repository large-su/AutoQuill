"""草稿箱素材管理 — 数据层。

负责：抓取知乎创作中心草稿箱（回答草稿）快照 data/drafts_*.json、
预览/筛选/统计、以及「从知乎删除草稿」（批量，确认后调用）。
零 UI 依赖；由 webui/server.py 的 /api/drafts 系列端点调用。
"""

from __future__ import annotations

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
    const qm = editA ? editA.href.match(/question/(d+)/) : null;
    const titleEl = card.querySelector('.CreationCardTitle-wrapper span');
    const timeEl = card.querySelector('[data-tooltip]');
    const textEl = card.querySelector('.CreationCardContent-text span');
    out.push({
      qid: qm ? qm[1] : '',
      url: editA ? editA.href : '',
      title: titleEl ? (titleEl.innerText || '').trim() : '',
      updated: timeEl ? (timeEl.getAttribute('data-tooltip') || '') : '',
      content: textEl ? (textEl.innerText || '').trim() : '',
    });
  });
  return out;
}
"""


def _normalize_row(r):
    title = (r.get("title") or "").strip()
    content = (r.get("content") or "").strip()
    updated = (r.get("updated") or "").strip()
    # data-tooltip 形如「编辑于 08-22 14:47」/「编辑于 2026-08-22 14:47」
    updated = re.sub(r"^编辑于\s*", "", updated)
    return {
        "qid": str(r.get("qid") or ""),
        "url": r.get("url") or "",
        "title": title,
        "updated": updated,
        "updated_date": _norm_date(updated),
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
