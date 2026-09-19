# -*- coding: utf-8 -*-
"""自动化模块视觉检查：起临时服务（独立数据目录）+ 造当天排班 → 截图时间轴。

不碰真实账号：全程只读 mock/本地状态，浏览器只打开本机控制台页面。
运行：.venv/Scripts/python tools/archive/probes/shot_automation.py [--width 1280]
输出：data/cleanup/auto_timeline.png
"""
import argparse, json, os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=820)
    args = ap.parse_args()
    data_dir = tempfile.mkdtemp(prefix="aq_auto_shot_")
    # 独立数据目录：把本机 config 复制过去（config 模块要求 llm_providers.json 存在）
    os.makedirs(os.path.join(data_dir, "config"), exist_ok=True)
    for name in ("llm_providers.json", "webui_model.json"):
        src = ROOT / "config" / name
        if src.exists():
            with open(src, "rb") as f:
                blob = f.read()
            with open(os.path.join(data_dir, "config", name), "wb") as f:
                f.write(blob)
    env = dict(os.environ, AQ_DATA_DIR=data_dir, PYTHONIOENCODING="utf-8")
    os.environ["AQ_DATA_DIR"] = data_dir      # ★ 本进程也要切，否则种子写进真实数据目录
    from automation import store, planner
    from automation.model import normalize_plan
    plan = normalize_plan({
        "enabled": True,
        "window": {"start": "08:00", "end": "23:30"},
        "tasks": {"full_chain": {"enabled": True, "daily_cap": 3},
                  "publish_drafts": {"enabled": True, "daily_cap": 3}},
    })
    store.save_plan(plan)
    from datetime import datetime, timedelta
    now = datetime.now().replace(hour=16, minute=28, second=0, microsecond=0)
    day = now.strftime("%Y-%m-%d")
    data = planner.materialize_day(now, plan, {}, {})
    # 造几条已完成/失败的台账 + 状态，让时间轴有层次
    jobs = data["schedule"]
    done_n = 0
    for j in jobs:
        if j["status"] != "planned":
            continue
        if done_n < 2:
            j["status"] = "done"
            j["note"] = "完成"
            store.append_ledger({"day": day, "key": j["key"], "type": j["type"],
                                 "status": "done", "units": 1, "message": "完成",
                                 "planned_at": j["planned_at"],
                                 "started_at": j["planned_at"],
                                 "finished_at": j["planned_at"],
                                 "artifacts": ["output/story_%s.md" % day.replace("-", "")]})
            done_n += 1
        elif done_n == 2:
            j["status"] = "failed"
            j["note"] = "模型无输出（重试 3 次）"
            store.append_ledger({"day": day, "key": j["key"], "type": j["type"],
                                 "status": "failed", "units": 0,
                                 "message": "模型无输出（重试 3 次）",
                                 "planned_at": j["planned_at"],
                                 "started_at": j["planned_at"],
                                 "finished_at": j["planned_at"]})
            done_n += 1
    store.save_day(day, data)
    boot = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    root_esc = str(ROOT).replace(chr(92), chr(92) * 2)
    boot.write("import sys\nsys.path.insert(0, r'%s')\n" % root_esc)
    boot.write("import webui.server as srv\n")
    boot.write("srv._ALLOWED_HOSTS.update({'127.0.0.1:%d','localhost:%d'})\n" % (args.port, args.port))
    boot.write("srv._ALLOWED_ORIGINS.update({'http://127.0.0.1:%d','http://localhost:%d'})\n" % (args.port, args.port))
    boot.write("srv.run(host='127.0.0.1', port=%d)\n" % args.port)
    boot.close()
    log = tempfile.NamedTemporaryFile("wb", suffix=".log", delete=False)
    proc = subprocess.Popen([sys.executable, boot.name], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        import urllib.request
        for _ in range(60):
            try:
                if urllib.request.urlopen("http://127.0.0.1:%d/" % args.port, timeout=1).status == 200:
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
            errors = []
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.goto("http://127.0.0.1:%d/" % args.port, wait_until="networkidle", timeout=30000)
            pg.evaluate("() => { const m = document.getElementById('setupMask'); if (m) m.classList.remove('show'); }")
            pg.select_option("#leftModeSel", "automation")
            pg.wait_for_timeout(2500)
            lanes = pg.evaluate("() => document.querySelectorAll('#autoTimeline .tl-lane').length")
            blocks = pg.evaluate("() => document.querySelectorAll('#autoTimeline .tl-block').length")
            png = outdir / "auto_timeline.png"
            pg.screenshot(path=str(png), full_page=False)
            # 左侧控制台通常比一屏高（任务配额 / 按钮在下面）——再拍一张滚到底的，
            # 否则「演练发布」这类新按钮在截图里永远看不到（一次性验收会漏）
            pg.evaluate("""() => {
              // 真正能滚的是 #pane-automation 的某个祖先（左栏），逐层找一个能滚的
              let el = document.getElementById('pane-automation');
              while (el && el.scrollHeight <= el.clientHeight) el = el.parentElement;
              if (el) el.scrollTop = el.scrollHeight;
              return el ? (el.className || el.tagName) : '';
            }""")
            pg.wait_for_timeout(400)
            png2 = outdir / "auto_timeline_console.png"
            pg.screenshot(path=str(png2), full_page=False)
            print("POINTS: lanes=%d blocks=%d console_errors=%d" % (lanes, blocks, len(errors)))
            print("screenshot(console):", png2)
            for e in errors[:5]:
                print("  console error:", e[:160])
            print("screenshot:", png)
            browser.close()
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
