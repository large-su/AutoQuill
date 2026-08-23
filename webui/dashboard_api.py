"""已发布内容看板 API 路由（由 webui/server.py 的 register_dashboard 注册）。"""
import logging
import re
import threading

from pydantic import BaseModel

from webui.browser_tasks import _DASH_DEL, _DASH_REFRESH, browser_busy

log = logging.getLogger(__name__)


class _AidListSpec(BaseModel):
    aids: list[str]


def register_dashboard(app):
    @app.get("/api/dashboard")
    def api_dashboard(q: str = "", start: str = "", end: str = "",
                      min_likes: int = 0, min_reads: int = 0,
                      min_comments: int = 0, min_collects: int = 0,
                      min_favors: int = 0, sort: str = "newest",
                      direction: str = "desc"):
        """已发布内容看板：读最新快照 + 筛选/搜索/排序 + 汇总。"""
        from webui import published
        d = published.load()
        rows = published.filter_rows(
            d["rows"], q=q, start=start, end=end,
            min_likes=min_likes, min_reads=min_reads,
            min_comments=min_comments, min_collects=min_collects,
            min_favors=min_favors, sort=sort, direction=direction)
        rows = [{k: v for k, v in r.items() if k != "content"} for r in rows]
        log.info("看板查询 q=%r start=%s end=%s min_likes=%d sort=%s → %d/%d 条",
                 q, start, end, min_likes, sort, len(rows), d["total"])
        return {
            "rows": rows,
            "total": len(rows),
            "all_total": d["total"],
            "stats": published.summarize(rows),
            "generated_at": d["generated_at"],
            "source_file": d["source_file"],
            "refresh": _DASH_REFRESH,
        }

    @app.post("/api/dashboard/refresh")
    def api_dashboard_refresh():
        """后台抓取创作中心最新已发布内容，落盘后前端可轮询状态。"""
        busy = browser_busy()
        if busy:
            return {"ok": False, "status": "busy",
                    "message": "「" + busy[0] + "」任务进行中，请完成后再刷新看板"}
        _DASH_REFRESH.update(status="running", progress="启动抓取…",
                             count=0, pct=None, error="")
        log.info("看板刷新任务启动：从知乎创作中心抓取已发布内容")

        def _on_scrape_progress(text, pct):
            m = re.search(r"已加载 (\d+) 条", text or "")
            _DASH_REFRESH.update(
                progress=text or "",
                pct=pct,
                count=int(m.group(1)) if m else _DASH_REFRESH["count"])

        def _run():
            try:
                from webui import published
                rows = published.scrape(progress=_on_scrape_progress)
                if rows:
                    _DASH_REFRESH.update(status="done", count=len(rows), pct=100,
                                         progress=f"完成，共 {len(rows)} 条")
                    log.info("看板刷新完成：%d 条", len(rows))
                else:
                    _DASH_REFRESH.update(
                        status="error",
                        error="抓取未取到有效数据（可能未登录/页面改版），已保留上次快照")
                    log.warning("看板刷新未取到有效数据，已保留上次快照")
            except Exception as exc:  # noqa: BLE001
                log.exception("dashboard 刷新失败")
                _DASH_REFRESH.update(status="error", error=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "status": "started"}

    @app.get("/api/dashboard/refresh/status")
    def api_dashboard_refresh_status():
        """轮询刷新任务状态。"""
        return dict(_DASH_REFRESH)

    @app.get("/api/dashboard/poor")
    def api_dashboard_poor(before: str = "", max_likes: int = 5,
                           max_reads: int = 100, max_comments: int = 1,
                           max_collects: int = 0, max_favors: int = 0):
        """筛选「时间久远 + 数据不佳」的候选（只读，不删除）。"""
        from webui import published
        d = published.load()
        rows = published.poor_and_old(
            d["rows"], before=before, max_likes=max_likes, max_reads=max_reads,
            max_comments=max_comments, max_collects=max_collects,
            max_favors=max_favors)
        rows = [{k: v for k, v in r.items() if k != "content"} for r in rows]
        log.info("看板筛选待清理候选 %d 条（全部 %d）", len(rows), d["total"])
        return {"rows": rows, "count": len(rows), "all_total": d["total"]}

    @app.post("/api/dashboard/prune")
    def api_dashboard_prune(spec: _AidListSpec):
        """仅从看板本地快照移除指定 aid（可逆，可重抓恢复）。"""
        from webui import published
        removed = published.prune_aids(spec.aids)
        log.info("看板本地移除 %d 条", removed)
        return {"ok": True, "removed": removed}

    @app.post("/api/dashboard/delete-zhihu")
    def api_dashboard_delete_zhihu(spec: _AidListSpec):
        """后台从知乎删除指定答案（★ 不可逆，前端必须显式确认后调用）。"""
        busy = browser_busy()
        if busy:
            return {"ok": False, "status": "busy",
                    "message": "「" + busy[0] + "」任务进行中，请完成后再删除"}
        aids = list(spec.aids)
        log.info("看板删除任务启动：%d 条（%s）",
                 len(aids), ",".join(aids[:20]) + ("…" if len(aids) > 20 else ""))
        _DASH_DEL.update(status="running", progress="开始…", count=len(aids),
                         deleted=0, error="")

        def _run():
            try:
                from webui import published
                deleted = published.delete_zhihu(
                    aids, progress=lambda t, p: _DASH_DEL.update(progress=t))
                _DASH_DEL.update(status="done", deleted=len(deleted))
                log.info("看板删除任务完成：%d/%d 条", len(deleted), len(aids))
            except Exception as exc:  # noqa: BLE001
                log.exception("知乎删除任务失败")
                _DASH_DEL.update(status="error", error=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "status": "started", "count": len(aids)}

    @app.get("/api/dashboard/delete-zhihu/status")
    def api_dashboard_delete_zhihu_status():
        return dict(_DASH_DEL)
