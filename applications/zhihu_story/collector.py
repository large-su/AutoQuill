# ============================================================
# applications/zhihu_story/collector.py — 作者故事采集编排
#
# Web 控制台「故事采集」任务入口：给定作者回答列表 URL + 采集
# 数量，在共享浏览器上滚动列表加载更多 → 收集未采集过的答案
# 链接（去重键 footer.answer_url，与既有采集通道一致）→ 逐个
# 打开详情提取全文 → 追加写入 data/collected_stories.jsonl。
# 每次只采新的：重复链接直接跳过（断点续采）。
#
# 架构位置：Layer 3 采集通道 — 作者维度编排（DOM 变体）
# 依赖 browser_adapter 的公开原语（get_author_answer_links /
# get_author_answer / eval_js），不触碰私有实现。
# ============================================================

import json
import logging
import os
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path

STORY_LIB = _data_path("data", "collected_stories.jsonl")

# 作者回答列表页：提取真实昵称（URL 只有 token，昵称在页头）
_AUTHOR_NAME_JS = r"""
() => {
  const el = document.querySelector('.ProfileHeader-name, .AuthorInfo-name, .UserLink-link');
  if (el) {
    const n = (el.textContent || '').replace(/[​-‍⁠﻿]/g, '').replace(/\s+/g, ' ').trim();
    if (n && n.length <= 20) return n;
  }
  const m = (document.title || '').match(/^(.+?)\s*[-_|]\s*知乎/);
  return m ? m[1].trim() : '';
}
"""

# 滚动到底部触发无限滚动加载更多
_SCROLL_LOAD_JS = r"""
() => { window.scrollTo(0, document.body.scrollHeight); return true; }
"""


def _norm_url(url):
    """答案链接规范化（去 hash/query），保证去重键跨通道一致。"""
    if not url:
        return ""
    url = url.split("#")[0]
    return url.split("?")[0]


def iter_collected_stories(out_file=None):
    """逐条读取采集库 JSONL（跳过空行/坏行），yield 每条记录 dict。

    采集库统一读取入口（collector 采集写入、author_profiler 读样本、
    webui storylib/profile-sources 读列表共用同一格式）。"""
    out_file = out_file or STORY_LIB
    if not os.path.exists(out_file):
        return
    with open(out_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_done_urls(out_file):
    """从已有 JSONL 读出已采集的 answer_url（断点续采集合）。"""
    done = set()
    for rec in iter_collected_stories(out_file):
        footer = rec.get("footer") or {}
        url = _norm_url(footer.get("answer_url") or rec.get("answer_url"))
        if url:
            done.add(url)
    return done


def load_author_counts(out_file):
    """统计库中各作者的现有篇数（新作者判定与追加日志用）。"""
    counts = {}
    for rec in iter_collected_stories(out_file):
        author = (rec.get("author") or "").strip()
        if author:
            counts[author] = counts.get(author, 0) + 1
    return counts


# 昵称里常见的不可见字符（零宽空格等），页面渲染可见但会污染
# 作者名/建档文件名，识别后一律剥离
_ZERO_WIDTH_CHARS = "​‌‍⁠﻿"


def _clean_author_name(name):
    for ch in _ZERO_WIDTH_CHARS:
        name = name.replace(ch, "")
    return name.strip()


def detect_author(browser, url):
    """识别作者名：页头昵称优先，识别失败用 URL token 兜底。

    作者回答列表页（/people/{token}/answers）URL 不含昵称，页头
    .ProfileHeader-name 才是真实作者名——手动指定名字会与页面
    实际作者产生冲突，故一律自动识别。"""
    try:
        name = _clean_author_name(str(browser.eval_js(_AUTHOR_NAME_JS) or ""))
        if name:
            return name
    except Exception:
        pass
    seg = url.rstrip("/").split("/")
    return seg[-2] if seg[-1] == "answers" else seg[-1]


def _scroll_load_more(browser, url, done, seen):
    """回到列表页滚动加载更多；返回包含未采集链接的新列表。

    提取详情时会离开列表页（页面停在答案页），滚动前必须重新
    进入列表页。滚动到底触发无限滚动，最多滚 4 轮，无未采集
    新链接即返回 []（调用方据此停止）。滚动后轮询等待新链接
    （固定 1.5s 在慢渲染/无头下常读不到——V4.2.2 用户反馈无头
    采集 0 篇）。"""
    from applications.zhihu_story.browser_adapter import (
        _AUTHOR_LINKS_JS, _check_cancel)
    for _ in range(4):
        _check_cancel()
        browser.get_author_answer_links(url)  # 回列表页顶部（返回值弃用）
        try:
            browser.eval_js(_SCROLL_LOAD_JS)
        except Exception:
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            _check_cancel()
            links = browser.eval_js(_AUTHOR_LINKS_JS) or []
            if any(_norm_url(l.get("href")) not in done
                   and _norm_url(l.get("href")) not in seen for l in links):
                return links
            time.sleep(0.8)
    return []


def collect_author_stories(url, count=10, min_length=None,
                           out_file=None, browser=None, progress=None):
    """采集作者故事：识别作者 → 滚动列表 → 链接去重 → 逐个提取。

    url: 作者回答列表页（zhihu.com/people/{token}/answers）
    count: 本次最多新增篇数（已有的一律跳过，只补新的）
    min_length: 正文最短字数（默认取应用配置 MIN_ANSWER_LENGTH）
    browser: browser_adapter 实例（缺省取共享浏览器单例）
    progress: 可选回调；每步同时发 root logger，webui 经 log_capture
              推送，无需传

    作者名一律自动识别（页头昵称 → URL token 兜底）：库中已有
    该作者则追加新样本，没有则自动建档。返回
    {"collected": 新增记录列表, "author": 作者名,
     "existing": 采集前该作者的已有篇数}。新增记录实时追加写入。
    """
    out_file = out_file or STORY_LIB
    if min_length is None:
        from applications.zhihu_story import config as sconfig
        min_length = int(getattr(sconfig, "MIN_ANSWER_LENGTH", 100) or 100)
    if browser is None:
        from applications.zhihu_story.browser_adapter import get_browser
        browser = get_browser()
    from applications.zhihu_story.browser_adapter import (
        _AUTHOR_LINKS_JS, _check_cancel)

    if progress:
        progress(f"已采集库去重加载中…")
    done = load_done_urls(out_file)
    counts = load_author_counts(out_file)
    log.info("采集启动：%s（本次最多新增 %d 篇，已有 %d 篇去重）",
             url, count, len(done))

    # 1. 进入作者回答列表页，自动识别作者名
    links = browser.get_author_answer_links(url) or []
    author = detect_author(browser, url)
    existing = counts.get(author, 0)
    log.info("作者「%s」：库中已有 %d 篇%s，本次追加新样本",
             author, existing,
             "" if existing else "（新作者，自动建档）")

    collected = []
    seen = set()
    while len(collected) < count:
        _check_cancel()
        fresh = [l for l in links
                 if _norm_url(l.get("href")) not in done
                 and _norm_url(l.get("href")) not in seen]
        if not fresh:
            links = _scroll_load_more(browser, url, done, seen)
            if not links:
                log.info("列表读尽，停止采集（共新增 %d 篇）", len(collected))
                break
            continue

        for link in fresh:
            if len(collected) >= count:
                break
            href = _norm_url(link.get("href"))
            seen.add(href)
            data = browser.get_author_answer(href, author,
                                             min_length=min_length)
            if not data:
                log.warning("  ⚠ 提取失败或正文过短：%s",
                            (link.get("title") or href)[:40])
                continue
            footer = data.get("footer") or {}
            record = {
                "source": "author_page_dom",
                "author": author,
                "title": data.get("title") or "",
                "answer": data.get("answer") or "",
                "footer": footer,
                "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if _norm_url(footer.get("answer_url")):
                done.add(_norm_url(footer.get("answer_url")))
            log.info("  ✓ [%d/%d] %s（%d 字，赞同=%s）",
                     len(collected) + 1, count,
                     record["title"][:32], len(record["answer"]),
                     footer.get("likes"))
            collected.append(record)

    log.info("采集完成：作者「%s」新增 %d 篇（库中共 %d 篇）→ %s",
             author, len(collected), existing + len(collected), out_file)
    return {"collected": collected, "author": author,
            "existing": existing}
