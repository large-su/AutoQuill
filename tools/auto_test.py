#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AutoQuill 自动回归测试（替代人工测试员）。

按检查点跑：
  1) 后端单元测试        python tests/run_all.py（324 用例）
  2) 语法检查            关键 Python 模块 py_compile + app.js node --check
  3) 前端 UI 回归         起临时服务（拦截业务 API 返回样例，离线可跑）,
                          Playwright 遍历主要功能并收集 console 错误
  4) 日志检查            服务端日志 ERROR/Traceback

用法:
  python tools/auto_test.py            # 全套（含前端 UI，需要本机 Edge）
  python tools/auto_test.py --quick    # 仅后端单测 + 语法检查（CI 可用）
  python tools/auto_test.py --port 8799

退出码：0 = 全部通过；1 = 存在失败。
"""
import argparse
import glob
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799
UI_TIMEOUT = 30000

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("auto_test")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL" if detail != "SKIP" else "SKIP"
    log.info("  [%s] %s%s", mark, name, (" - " + detail if detail and detail != "SKIP" else ""))


def run_cmd(cmd, timeout=600):
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


# ---------- 1) 后端单元测试 ----------
def test_backend():
    log.info("== 1) 后端单元测试 ==")
    try:
        r = run_cmd([sys.executable, "tests/run_all.py"], timeout=600)
    except subprocess.TimeoutExpired:
        check("后端单元测试", False, "执行超时"); return
    import re as _re
    m = _re.search(r"共执行 (\d+) 个用例", (r.stdout + r.stderr))
    detail = ("%s 个用例" % m.group(1)) if m else "已执行"
    check("后端单元测试", r.returncode == 0,
          detail if r.returncode == 0 else (r.stdout + r.stderr)[-500:])


# ---------- 2) 语法检查 ----------
def test_syntax():
    log.info("== 2) 语法检查 ==")
    py_files = []
    for pat in ["webui/*.py", "workflows/*.py", "applications/zhihu_story/*.py",
                "web_drivers/*.py", "core/*.py", "config/*.py", "story_*.py",
                "main.py", "llm_*.py", "kb_manager.py", "tools/*.py"]:
        py_files += glob.glob(str(ROOT / pat))
    bad = []
    for f in sorted(py_files):
        if f.endswith("__init__.py"):
            continue
        r = subprocess.run([sys.executable, "-m", "py_compile", f],
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(Path(f).name)
    check("Python 编译", not bad, ("失败: " + ", ".join(bad)) if bad else f"{len(py_files)} 文件")

    node = shutil_which("node")
    if node:
        r = run_cmd([node, "--check", "webui/static/app.js"])
        check("app.js 语法", r.returncode == 0, r.stderr[-200:] if r.returncode else "")
    else:
        check("app.js 语法", True, "SKIP（未找到 node）")


# ---------- 3) 前端 UI 回归 ----------
def _wait_http(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            time.sleep(0.5)
    return False


def test_frontend(port):
    log.info("== 3) 前端 UI 回归 ==")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        check("前端 UI 回归", True, "SKIP（playwright 不可用: %s）" % exc)
        return

    boot = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    root_esc = str(ROOT).replace("\\", "\\\\")
    boot.write(
        "import sys\n"
        "sys.path.insert(0, r'%s')\n"
        "import webui.server as srv\n"
        "srv._ALLOWED_HOSTS.update({'127.0.0.1:%d','localhost:%d'})\n"
        "srv._ALLOWED_ORIGINS.update({'http://127.0.0.1:%d','http://localhost:%d'})\n"
        "srv.run(host='127.0.0.1', port=%d)\n" % (
            root_esc, port, port, port, port, port))
    boot.close()
    server_log = Path(tempfile.mkdtemp(prefix="aq_uitest_")) / "server.log"
    proc = subprocess.Popen(
        [sys.executable, boot.name], cwd=str(ROOT),
        stdout=open(server_log, "ab"), stderr=subprocess.STDOUT)
    try:
        if not _wait_http("http://127.0.0.1:%d/" % port):
            check("前端 UI 回归", False, "测试服务启动失败")
            return
        with sync_playwright() as p:
            try:
                b = p.chromium.launch(channel="msedge", headless=True)
                launched = True
            except Exception as exc:
                check("前端 UI 回归", True, "SKIP（本机无 Edge: %s）" % exc)
                return
            pg = b.new_page(viewport={"width": 1680, "height": 1050})
            errors = []
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            DASH = {"rows": [
                {"aid": "1", "url": "u1", "title": "测试故事A", "publish_date": "2026-08-20",
                 "likes": 120, "reads": 3000, "comments": 5, "collects": 8, "favors": 1, "genre": "古言"},
                {"aid": "2", "url": "u2", "title": "测试故事B", "publish_date": "2026-08-22",
                 "likes": 30, "reads": 900, "comments": 1, "collects": 2, "favors": 0, "genre": "甜文"},
            ], "total": 2, "all_total": 2,
                "stats": {"total": 2, "liked": 1, "sum_likes": 150, "sum_reads": 3900,
                          "avg_reads": 1950, "liked_ratio": 50, "sum_comments": 6,
                          "date_min": "2026-08-20", "date_max": "2026-08-22"},
                "generated_at": "2026-08-23T12:00:00", "source_file": "data/pub.json",
                "refresh": {"status": "idle"}}
            DRAFTS = {"rows": [
                {"qid": "9", "url": "u9", "title": "草稿问题一？", "updated_date": "2026-08-22",
                 "chars": 1200, "content": "草稿内容……" * 10},
                {"qid": "8", "url": "u8", "title": "草稿问题二？", "updated_date": "2026-08-21",
                 "chars": 800, "content": "另一个草稿……" * 8},
            ], "total": 2, "all_total": 2,
                "stats": {"total": 2, "sum_chars": 2000, "avg_chars": 1000,
                          "date_min": "2026-08-21", "date_max": "2026-08-22"},
                "generated_at": "2026-08-23T12:00:00", "source_file": "data/dr.json",
                "refresh": {"status": "idle"}}
            STORIES = {"stories": [{"name": "story_1_20260823.md", "size": 5120}]}

            def route(route):
                u = route.request.url
                if "/api/dashboard" in u and "refresh" not in u and "status" not in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(DASH, ensure_ascii=False))
                elif "/api/drafts" in u and "delete" not in u and "status" not in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(DRAFTS, ensure_ascii=False))
                elif "/api/drafts/delete/status" in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"status":"done","progress":"完成：共 1 个，已删除 1，跳过 0，异常 0","count":1,"deleted":1,"error":""}')
                elif "/api/drafts/delete" in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"ok":true,"status":"started","count":1}')
                elif "/api/stories" in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(STORIES, ensure_ascii=False))
                else:
                    route.continue_()
            pg.route("**/api/**", route)

            base = "http://127.0.0.1:%d" % port
            pg.goto(base + "/", wait_until="networkidle", timeout=UI_TIMEOUT)
            pg.evaluate("() => { const m = document.getElementById('setupMask'); if (m) m.classList.remove('show'); }")
            pg.wait_for_timeout(600)
            check("首页加载", "AutoQuill" in pg.title())
            check("样式生效", "rgb(11, 14, 20)" in pg.evaluate("() => getComputedStyle(document.body).backgroundColor"))
            modes = pg.evaluate("() => Array.from(document.querySelectorAll('#leftModeSel option')).map(o => o.text)")
            check("四大模式", modes == ["工作台", "作者蒸馏", "已发布内容看板", "草稿箱素材"], json.dumps(modes, ensure_ascii=False))

            pg.click("#btnSetup"); pg.wait_for_timeout(300)
            check("设置弹窗", pg.evaluate("() => document.getElementById('settingsMask')?.classList.contains('show')"))
            pg.click("#settingsClose"); pg.wait_for_timeout(200)

            pg.select_option("#leftModeSel", "dashboard")
            pg.wait_for_function("() => document.querySelectorAll('#dashTable tbody tr').length === 2", timeout=UI_TIMEOUT)
            check("看板表格", pg.evaluate("() => document.querySelectorAll('#dashTable tbody tr').length") == 2)
            check("看板 KPI", pg.evaluate("() => document.querySelectorAll('#dashKpis .kpi-card').length") == 4)
            # 图表 tab 切换 + canvas
            pg.click("#chartTabs .chart-tab[data-tab='top']"); pg.wait_for_timeout(500)
            cv = pg.evaluate("() => { const c = document.querySelector('#chartTop canvas'); return c ? c.width > 0 : false; }")
            check("图表渲染", cv)
            pg.click("#chartTabs .chart-tab[data-tab='trend']"); pg.wait_for_timeout(300)
            # 分页（样例 2 条 < 页容量：应为 1/1 且下一页禁用）
            page_no = pg.evaluate("() => document.getElementById('dashPageNo').textContent")
            next_disabled = pg.evaluate("() => document.getElementById('dashNext').disabled")
            if not next_disabled:
                pg.click("#dashNext"); pg.wait_for_timeout(300)
            page_no2 = pg.evaluate("() => document.getElementById('dashPageNo').textContent")
            check("看板分页", page_no == "1/1" and next_disabled and page_no2 == "1/1",
                  "page=%s next_disabled=%s after=%s" % (page_no, next_disabled, page_no2))
            # 筛选 chips
            pg.fill("#dashMinLikes", "100"); pg.dispatch_event("#dashMinLikes", "change")
            pg.wait_for_timeout(900)
            check("筛选 chips", pg.evaluate("() => document.querySelectorAll('#dashFilterbar .f-chip').length > 0"))

            pg.select_option("#leftModeSel", "drafts")
            pg.wait_for_function("() => document.querySelectorAll('#draftList .dft-row').length === 2", timeout=UI_TIMEOUT)
            check("草稿列表", pg.evaluate("() => document.querySelectorAll('#draftList .dft-row').length") == 2)
            check("草稿 KPI", pg.evaluate("() => document.querySelectorAll('#draftKpis .kpi-card').length") == 4)
            pg.click("#draftList .dft-row:nth-child(1) .cb")
            check("草稿勾选", "已选 1 个" in pg.evaluate("() => document.getElementById('draftSelStatus').textContent"))
            pg.click("#draftList .dft-row:nth-child(2)")
            pg.wait_for_timeout(300)
            check("草稿预览", pg.evaluate("() => document.getElementById('draftViewMask').classList.contains('show')"))
            pg.click("#draftViewClose"); pg.wait_for_timeout(200)
            pg.on("dialog", lambda d: d.accept())
            pg.click("#btnDraftsDelete"); pg.wait_for_timeout(7500)
            status_txt = pg.evaluate("() => (document.getElementById('draftStatus').innerText || '').slice(0, 60)")
            check("草稿删除流",
                  "已删除" in status_txt or "完成" in status_txt,
                  "status='%s' del_disabled=%s" % (
                      status_txt,
                      pg.evaluate("() => document.getElementById('btnDraftsDelete').disabled")))

            pg.select_option("#leftModeSel", "workspace")
            pg.wait_for_timeout(300)
            check("回工作台", pg.evaluate("() => !document.getElementById('pane-workspace').hidden"))
            check("页面无 console 错误", not errors, errors[:3] and "; ".join(errors[:3]))

            b.close()
            # 服务日志检查
            text = server_log.read_text(encoding="utf-8", errors="replace")
            bad = [ln for ln in text.splitlines() if "[ERROR]" in ln or "Traceback" in ln]
            check("服务端日志无错误", not bad, "; ".join(bad[:2]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            os.unlink(boot.name)
        except Exception:
            pass


def shutil_which(name):
    import shutil
    return shutil.which(name)


def main():
    ap = argparse.ArgumentParser(description="AutoQuill 自动回归测试")
    ap.add_argument("--quick", action="store_true", help="仅后端单测 + 语法检查")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    log.info("开始 AutoQuill 自动回归（%s）", "quick" if args.quick else "full")
    test_backend()
    test_syntax()
    if not args.quick:
        test_frontend(args.port)

    log.info("")
    log.info("=" * 60)
    fails = [r for r in RESULTS if not r[1]]
    skips = [r for r in RESULTS if r[1] and r[2] == "SKIP"]
    log.info("汇总：检查点 %d，通过 %d，失败 %d，跳过 %d",
             len(RESULTS), len(RESULTS) - len(fails) - len(skips), len(fails), len(skips))
    for name, ok, detail in RESULTS:
        if not ok:
            log.info("  ✗ %s：%s", name, detail)
    log.info("=" * 60)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
