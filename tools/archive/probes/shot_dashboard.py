# -*- coding: utf-8 -*-
"""看板视觉检查：起临时服务 + 造一批接近真实的样例数据，截图统计/明细两个视图。

用途：改看板布局/样式后跑一次，肉眼（或读图）确认排版、留白、字号、栅格。
运行：.venv/Scripts/python tools/archive/probes/shot_dashboard.py [--port 8799]
输出：data/cleanup/dash_stats.png / dash_detail.png
"""
import argparse, json, random, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

GENRES = ["古言", "甜文", "虐文", "爽文", "悬疑", "都市", "重生", "仙侠"]
TITLES = ["嫡女的规矩", "替身三年", "我把合同扔进火锅", "第十七年", "冷宫照月",
          "她在雨里等了很久", "我的前夫失忆了", "山神的新娘", "长夜将明", "退婚后我封了侯"]


def build_rows(n=260, months=20):
    random.seed(20260919)
    rows = []
    for i in range(n):
        likes = int(random.lognormvariate(3.2, 1.15))
        reads = likes * random.randint(8, 26) + random.randint(50, 400)
        month = 1 + (i % months)
        year = 2025 if month <= 4 else 2026
        rows.append({
            "aid": str(1000 + i), "url": "https://www.zhihu.com/answer/%d" % (1000 + i),
            "title": TITLES[i % len(TITLES)] + "（%d）" % (i + 1),
            "publish_date": "%d-%02d-%02d" % (year, ((month - 1) % 12) + 1, (i % 27) + 1),
            "likes": likes, "reads": reads,
            "comments": max(0, int(likes * random.uniform(0.02, 0.12))),
            "collects": max(0, int(likes * random.uniform(0.1, 0.5))),
            "favors": max(0, int(likes * random.uniform(0.05, 0.3))),
            "genre": GENRES[i % len(GENRES)],
        })
    return rows


def stats_of(rows):
    n = len(rows)
    likes = sum(r["likes"] for r in rows)
    reads = sum(r["reads"] for r in rows)
    liked = sum(1 for r in rows if r["likes"] > 0)
    dates = sorted(r["publish_date"] for r in rows)
    return {"total": n, "liked": liked, "sum_likes": likes, "sum_reads": reads,
            "sum_comments": sum(r["comments"] for r in rows),
            "sum_collects": sum(r["collects"] for r in rows),
            "sum_favors": sum(r["favors"] for r in rows),
            "avg_likes": likes // n, "avg_reads": reads // n,
            "liked_ratio": int(liked * 100 / n),
            "date_min": dates[0], "date_max": dates[-1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1050)
    args = ap.parse_args()
    port = args.port
    rows = build_rows()
    dash = {"rows": rows, "total": len(rows), "all_total": len(rows),
            "stats": stats_of(rows), "generated_at": "2026-09-19T15:30:00",
            "source_file": "data/published_answers_2026-09-19.json",
            "refresh": {"status": "idle"}}

    boot = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    root_esc = str(ROOT).replace(chr(92), chr(92) * 2)
    boot.write("import sys\nsys.path.insert(0, r'%s')\n" % root_esc)
    boot.write("import webui.server as srv\n")
    boot.write("srv._ALLOWED_HOSTS.update({'127.0.0.1:%d','localhost:%d'})\n" % (port, port))
    boot.write("srv._ALLOWED_ORIGINS.update({'http://127.0.0.1:%d','http://localhost:%d'})\n" % (port, port))
    boot.write("srv.run(host='127.0.0.1', port=%d)\n" % port)
    boot.close()
    log = tempfile.NamedTemporaryFile("wb", suffix=".log", delete=False)
    proc = subprocess.Popen([sys.executable, boot.name], cwd=str(ROOT),
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        import urllib.request
        for _ in range(60):
            try:
                if urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1).status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        from playwright.sync_api import sync_playwright
        outdir = ROOT / "data" / "cleanup"
        outdir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            pg = browser.new_page(viewport={"width": args.width, "height": args.height},
                                  device_scale_factor=2)
            body = json.dumps(dash, ensure_ascii=False)

            def route(r):
                if "/api/dashboard" in r.request.url and "refresh" not in r.request.url:
                    r.fulfill(status=200, content_type="application/json", body=body)
                elif "/api/stories" in r.request.url:
                    r.fulfill(status=200, content_type="application/json", body='{"stories":[]}')
                else:
                    r.continue_()
            pg.route("**/api/**", route)
            pg.goto("http://127.0.0.1:%d/" % port, wait_until="networkidle", timeout=30000)
            pg.evaluate("() => { const m = document.getElementById('setupMask'); if (m) m.classList.remove('show'); }")
            pg.select_option("#leftModeSel", "dashboard")
            pg.wait_for_timeout(1800)
            pg.evaluate("() => document.getElementById('dashCard').scrollIntoView()")
            stats_png = outdir / "dash_stats.png"
            pg.screenshot(path=str(stats_png), full_page=False)
            print("统计视图：", stats_png)
            pg.click("#dashViewSwitch .view-btn[data-view='detail']")
            pg.wait_for_timeout(900)
            detail_png = outdir / "dash_detail.png"
            pg.screenshot(path=str(detail_png), full_page=False)
            print("明细视图：", detail_png)
            browser.close()
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
