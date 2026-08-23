"""草稿箱素材管理 API 路由（由 webui/server.py 的 register_drafts 注册）。"""
import logging
import re
import threading

from pydantic import BaseModel

from webui.browser_tasks import _DRAFTS_DEL, _DRAFTS_REFRESH, browser_busy

log = logging.getLogger(__name__)


class _DraftQidSpec(BaseModel):
    qids: list[str]


def register_drafts(app):
    @app.get("/api/drafts")
    def api_drafts(q: str = "", start: str = "", end: str = "",
                   min_chars: int = 0, max_chars: int = 0,
                   sort: str = "updated", direction: str = "desc"):
        """草稿箱：读最新快照 + 筛选/搜索/排序 + 汇总。"""
        from webui import drafts
        d = drafts.load()
        rows = drafts.filter_rows(d["rows"], q=q, start=start, end=end,
                                  min_chars=min_chars, max_chars=max_chars,
                                  sort=sort, direction=direction)
        log.info("草稿箱查询 q=%r start=%s end=%s 字数=%d-%d → %d/%d 个",
                 q, start, end, min_chars, max_chars, len(rows), d["total"])
        return {
            "rows": rows,
            "total": len(rows),
            "all_total": d["total"],
            "stats": drafts.summarize(rows),
            "generated_at": d["generated_at"],
            "source_file": d["source_file"],
            "refresh": _DRAFTS_REFRESH,
        }

    @app.post("/api/drafts/refresh")
    def api_drafts_refresh():
        """后台抓取草稿箱，落盘后前端轮询状态。"""
        busy = browser_busy()
        if busy:
            return {"ok": False, "status": "busy",
                    "message": "「" + busy[0] + "」任务进行中，请完成后再刷新草稿箱"}
        _DRAFTS_REFRESH.update(status="running", progress="启动抓取…",
                               count=0, pct=None, error="")
        log.info("草稿箱刷新任务启动")

        def _on_progress(text, pct):
            m = re.search(r"已加载 (\d+) 个草稿", text or "")
            _DRAFTS_REFRESH.update(
                progress=text or "",
                pct=pct,
                count=int(m.group(1)) if m else _DRAFTS_REFRESH["count"])

        def _run():
            try:
                from webui import drafts
                rows = drafts.scrape(progress=_on_progress)
                if rows:
                    _DRAFTS_REFRESH.update(status="done", count=len(rows), pct=100,
                                           progress=f"完成，共 {len(rows)} 个")
                    log.info("草稿箱刷新完成：%d 个", len(rows))
                else:
                    _DRAFTS_REFRESH.update(
                        status="error",
                        error="未抓取到草稿（可能未登录/页面改版），已保留上次快照")
            except Exception as exc:  # noqa: BLE001
                log.exception("草稿箱刷新失败")
                _DRAFTS_REFRESH.update(status="error", error=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "status": "started"}

    @app.get("/api/drafts/refresh/status")
    def api_drafts_refresh_status():
        return dict(_DRAFTS_REFRESH)

    @app.post("/api/drafts/delete")
    def api_drafts_delete(spec: _DraftQidSpec):
        """后台从知乎草稿箱删除指定草稿（不可逆，界面显式确认后调用）。"""
        busy = browser_busy()
        if busy:
            return {"ok": False, "status": "busy",
                    "message": "「" + busy[0] + "」任务进行中，请完成后再删除草稿"}
        qids = list(spec.qids)
        log.info("草稿删除任务启动：%d 个（%s）",
                 len(qids), ",".join(qids[:20]) + ("…" if len(qids) > 20 else ""))
        _DRAFTS_DEL.update(status="running", progress="开始…", count=len(qids),
                           deleted=0, error="")

        def _run():
            try:
                from webui import drafts
                deleted = drafts.delete_drafts(
                    qids, progress=lambda t, p: _DRAFTS_DEL.update(progress=t))
                _DRAFTS_DEL.update(status="done", deleted=len(deleted))
                log.info("草稿删除任务完成：%d/%d 个", len(deleted), len(qids))
            except Exception as exc:  # noqa: BLE001
                log.exception("草稿删除任务失败")
                _DRAFTS_DEL.update(status="error", error=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "status": "started", "count": len(qids)}

    @app.get("/api/drafts/delete/status")
    def api_drafts_delete_status():
        return dict(_DRAFTS_DEL)
