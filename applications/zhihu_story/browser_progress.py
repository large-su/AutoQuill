# ============================================================
# browser_progress.py — 进度事实的**只读**页面原语
#
# 分层：本模块只负责「去页面上把原始数据取回来」，不做任何解析与判断——
# 解析在 core/progress.py（纯逻辑、可单测），落盘与差异在 webui/site_progress.py。
# 这样接口改版时只需要动这一个文件，且删掉 webui 也不影响 core 的单测。
#
# 数据源（2026-09-28 真机确认，都是创作中心自己在用的正式接口）：
#   · 已发布回答：/api/v4/creators/creations/v2/answer?start&end&sort_type=created
#     → paging.totals = 区间内发布篇数；data[].data.{id,created_time,question_id,title}
#   · 草稿数：/api/v4/answer-drafts/count → count
# 两条都是只读 GET，各一次请求（实测 <1 秒）。
#
# ★ 本模块绝不写操作、绝不改动页面状态。
# ============================================================

import logging
import time

from applications.zhihu_story.browser_utils import _NAV_TIMEOUT
from core import progress as _progress

log = logging.getLogger(__name__)

# 创作中心「已发布回答」页：报告类/统计类接口以它为 Referer 最稳
CREATOR_ANSWERS_URL = "https://www.zhihu.com/creator/manage/creation/answer"

# 一次取回两个接口的原始 JSON（不做解析：解析归 core/progress）
# ★ 时间戳不在这里取：JS 的 toISOString() 是 UTC，而全系统都用本地时间，
#   混用会让「快照几小时前」这种新鲜度判断直接算错（2026-09-28 测试抓到）。
#   时间一律由 Python 侧统一打（core.progress.build_snapshot 的 at 参数）。
_READ_JS = r"""
async (arg) => {
  const dayStart = arg.day_start;
  const now = arg.now;
  const out = {};
  const get = async (url) => {
    try {
      const r = await fetch(url, {credentials: 'include'});
      if (!r.ok) return {__status: r.status};
      return await r.json();
    } catch (e) {
      return {__error: String(e).slice(0, 120)};
    }
  };
  const q = 'start=' + dayStart + '&end=' + now
      + '&limit=50&offset=0&need_co_creation=1&sort_type=created';
  out.published = await get('/api/v4/creators/creations/v2/answer?' + q);
  out.drafts = await get('/api/v4/answer-drafts/count');
  return out;
}
"""


def _day_bounds(now=None):
    """今天 00:00 与当前时刻的 Unix 秒（接口用它按时间过滤，不用解析相对时间）。"""
    import datetime as _dt
    now = now or _dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def read_raw(browser, now=None, wait=2.0):
    """读原始进度数据。返回 {"published": {...}, "drafts": {...}, "at": ...} 或 None。

    失败一律返回 None（未登录 / 接口改版 / 超时）：**读不到就不猜**，
    由调用方退回本地计数并记一条 warning。
    """
    if browser is None or getattr(browser, "page", None) is None:
        return None
    try:
        # 保证在知乎域下（否则 fetch 相对路径会 404）
        url = browser.page.url or ""
        if "zhihu.com" not in url:
            browser.page.goto(CREATOR_ANSWERS_URL, wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT)
            time.sleep(wait)
        start, end = _day_bounds(now)
        got = browser._safe_evaluate(_READ_JS,
                                     {"day_start": start, "now": end})
    except Exception as exc:                # noqa: BLE001
        log.warning("进度校核：读取失败（%s）", exc)
        return None
    if not isinstance(got, dict):
        return None
    published = got.get("published")
    drafts = got.get("drafts")
    # 两侧都拿不到 → 视为读取失败，不用 0 覆盖真实数字
    if not isinstance(published, dict) or not isinstance(drafts, dict):
        log.warning("进度校核：接口返回异常（published=%s drafts=%s）",
                    type(published).__name__, type(drafts).__name__)
        return None
    if published.get("__status") or published.get("__error"):
        log.warning("进度校核：已发布接口异常 %s",
                    published.get("__status") or published.get("__error"))
        return None
    if drafts.get("__status") or drafts.get("__error"):
        log.warning("进度校核：草稿数接口异常 %s",
                    drafts.get("__status") or drafts.get("__error"))
        return None
    return {"published": published, "drafts": drafts,
            "day": _today(now)}


def _today(now=None):
    import datetime as _dt
    return (now or _dt.datetime.now()).strftime("%Y-%m-%d")


# 供测试与调用方引用（避免各自硬编码接口路径）
PUBLISHED_API = _progress.PUBLISHED_API
DRAFT_COUNT_API = _progress.DRAFT_COUNT_API
