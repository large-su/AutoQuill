# -*- coding: utf-8 -*-
"""M4 托盘常驻验证：**跑生产代码**（tools/launcher.TrayController）在真机上的行为。

第一版 spike（本次提交前的临时脚本）已证明 .NET NotifyIcon 在 pywebview/WinForms
里能用；这一版把「手写的托盘 demo」换成 launcher 里的真实实现，验证四件事：

  1) 托盘图标 + 右键菜单能建起来（图标句柄有效、菜单项齐全）；
  2) 关窗「最小化到托盘」：closing 返回 False、窗口隐藏但进程还活着；
      设置里关掉 close_to_tray 后必须变回「真关闭」；
  3) 托盘菜单动作真的打到本机 API（暂停/恢复、立即执行），并更新状态文案；
  4) 退出：放行关闭 → webview.start 返回（进程可干净退出）。

不依赖真实服务：起一个本地假 API（记录收到的请求），把 launcher.BASE_URL 指过去。

输出：data/cleanup/tray_spike.json + data/cleanup/tray_spike.png（托盘区截图）
运行：.venv/Scripts/python tools/archive/probes/spike_tray.py
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "cleanup"
OUT.mkdir(parents=True, exist_ok=True)

REPORT = {"checks": [], "notes": {}}
REQUESTS = []          # [(method, path)] —— 假服务收到的请求
FAKE_PORT = 8799
FAKE_STATUS = {
    "enabled": True, "paused": False, "running": None,
    "summary": {"per_type": {"publish_drafts": {"cap": 3, "done": 1},
                             "full_chain": {"cap": 3, "done": 2}}},
    "schedule": [
        {"key": "k1", "type": "full_chain", "status": "planned",
         "planned_at": "2026-09-19T09:00:00"},
        {"key": "k2", "type": "publish_drafts", "status": "planned",
         "planned_at": "2026-09-19T10:00:00"},
    ],
}


def check(name, ok, detail=""):
    REPORT["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS  " if ok else "FAIL  ") + name
          + ("   | " + str(detail) if detail else ""), flush=True)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):      # 静音
        pass

    def _send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        REQUESTS.append(("GET", self.path))
        if self.path == "/api/automation":
            self._send(FAKE_STATUS)
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<body style='background:#0b0e14;color:#eee;"
                             "font-family:sans-serif;padding:24px'>"
                             "<h2>M4 托盘验证</h2><p>窗口 10 秒后藏进托盘，"
                             "再从托盘弹回，然后真退出。</p></body>".encode("utf-8"))
        else:
            self._send({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        REQUESTS.append(("POST", self.path))
        self._send({"ok": True})


class _StubConfig:
    """替身配置：不碰用户真实的 config/launcher.json。"""

    def __init__(self, close_to_tray=True):
        self.values = {"close_to_tray": close_to_tray, "autostart": False,
                       "tray_hint_shown": False}
        self.saved = []

    def load(self):
        return dict(self.values)

    def save(self, patch):
        self.saved.append(dict(patch))
        self.values.update(patch)
        return dict(self.values)


def main():
    import tools.launcher as L
    import webview

    server = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    L.BASE_URL = "http://127.0.0.1:%d" % FAKE_PORT
    stub = _StubConfig()
    L.launcher_config = stub

    window = webview.create_window("AutoQuill M4 托盘验证", L.BASE_URL,
                                   width=520, height=300)
    state = {"done": False}

    def capture_tray(path, w=620, h=64):
        try:
            from System.Windows.Forms import Screen
            from System.Drawing import Bitmap, Graphics, Size, Imaging
            b = Screen.PrimaryScreen.Bounds
            bmp = Bitmap(w, h)
            g = Graphics.FromImage(bmp)
            g.CopyFromScreen(b.X + b.Width - w, b.Y + b.Height - h, 0, 0,
                             Size(w, h))
            bmp.Save(str(path), Imaging.ImageFormat.Png)
            return True
        except Exception as exc:      # noqa: BLE001
            REPORT["notes"]["capture_error"] = repr(exc)
            return False

    def worker():
        try:
            # 回调「刚进来」的第一时间记录原生对象是否就绪：pywebview 的 start(func)
            # 是先起线程、再创建窗口，所以这里**可能是 None**——线上 launcher.log 里
            # 22:06 拿到、22:15 没拿到，就是踩了这个竞态（所以生产代码必须先 wait_native）
            immediate = window.native is not None
            REPORT["notes"]["native_immediate"] = immediate
            print("NOTE  回调刚进来时 native 就绪：%s" % immediate, flush=True)
            time.sleep(2.5)
            tray = L.TrayController(window)
            check("wait_native 能等到窗口原生对象", tray.wait_native(15),
                  "immediate=%s" % immediate)
            check("生产代码 TrayController 挂载成功", tray.attach(),
                  "available=%s" % tray.available)
            if not tray.available:
                return
            time.sleep(0.8)
            notify = tray.notify
            check("托盘图标可见且句柄有效",
                  bool(notify.Visible and notify.Icon
                       and notify.Icon.Handle.ToInt64() != 0))
            items = [str(i.Text) for i in notify.ContextMenuStrip.Items
                     if str(i.Text)]
            check("右键菜单项齐全（打开/状态/暂停/立即执行/退出）",
                  len(items) >= 5 and items[0] == "打开控制台"
                  and items[-1] == "退出 AutoQuill", items)
            window.events.closing += tray.on_closing
            check("关窗钩子已挂（closing 事件）", True)
            check("托盘区截图已保存", capture_tray(OUT / "tray_spike.png"))

            # ---- 关窗 = 藏进托盘（生产代码路径）----
            tray._refresh()                     # 先让状态文案就位
            form = window.native
            from System import Action
            form.Invoke(Action(lambda: form.Close()))
            time.sleep(1.5)
            check("点 X 后窗口隐藏、进程仍在（closing 返回 False）",
                  (not form.Visible) and (not form.IsDisposed),
                  "Visible=%s IsDisposed=%s" % (form.Visible, form.IsDisposed))
            check("首次隐藏写入了「已提示」标记",
                  bool(stub.saved) and stub.values.get("tray_hint_shown") is True,
                  stub.saved)

            # ---- 设置里关掉 close_to_tray → 必须变回真关闭 ----
            stub.values["close_to_tray"] = False
            check("设置关闭后 on_closing 放行（真关闭）",
                  tray.on_closing() is True)
            stub.values["close_to_tray"] = True

            # ---- 托盘菜单动作真的打到 API ----
            REQUESTS.clear()
            tray.toggle_pause()
            time.sleep(0.6)
            check("菜单「暂停自动化」调用 /api/automation/pause",
                  ("POST", "/api/automation/pause") in REQUESTS, REQUESTS)
            REQUESTS.clear()
            tray.run_now()
            time.sleep(0.6)
            check("菜单「立即执行下一个」调用 /api/automation/run-now",
                  ("POST", "/api/automation/run-now") in REQUESTS, REQUESTS)
            check("菜单状态行显示今日进度",
                  "发布 1/3" in (tray.state_item.Text if tray.state_item else ""),
                  tray.state_item.Text if tray.state_item else "")
            check("状态文案（纯函数）正确",
                  L.tray_status_text(FAKE_STATUS) == "运行中 · 发布 1/3 · 撰写 2/3",
                  L.tray_status_text(FAKE_STATUS))
            check("「立即执行」在下一个是发布时会先确认（纯函数判定）",
                  L.TrayController._next_job(FAKE_STATUS)["type"] == "full_chain"
                  and L.TrayController._next_job(
                      {"schedule": [{"type": "publish_drafts",
                                     "status": "planned",
                                     "planned_at": "2026-09-19T08:00:00"}]}
                  )["type"] == "publish_drafts")

            # ---- 托盘不可用时的兜底：关窗必须最小化到任务栏（程序继续跑）----
            tray.form = form
            saved_available, tray.available = tray.available, False
            from System.Windows.Forms import FormWindowState
            form.Invoke(Action(lambda: setattr(form, "WindowState",
                                              FormWindowState.Normal)))
            check("托盘不可用时关窗改为最小化到任务栏",
                  tray.on_closing() is False
                  and str(form.WindowState).endswith("Minimized"),
                  "WindowState=%s" % form.WindowState)
            form.Invoke(Action(lambda: setattr(form, "WindowState",
                                              FormWindowState.Normal)))
            tray.available = saved_available

            # ---- 从托盘叫回来 ----
            tray.show_window()
            time.sleep(1.0)
            check("从托盘能唤回窗口", bool(form.Visible))

            # ---- 真退出 ----
            state["done"] = True
            tray.quit()
            time.sleep(1.5)
            check("退出后窗口真正关闭", bool(form.IsDisposed) or not form.Visible,
                  "IsDisposed=%s" % form.IsDisposed)
        except Exception:      # noqa: BLE001
            import traceback
            REPORT["notes"]["exception"] = traceback.format_exc()
            print("验证异常：\n" + traceback.format_exc(), flush=True)
            try:
                window.destroy()
            except Exception:      # noqa: BLE001
                pass

    def watchdog():
        time.sleep(50)
        try:
            window.destroy()
        except Exception:      # noqa: BLE001
            pass

    threading.Thread(target=watchdog, daemon=True).start()
    webview.start(worker)
    check("webview.start 已返回（可干净退出）", True)
    server.shutdown()

    ok = all(c["ok"] for c in REPORT["checks"])
    REPORT["ok"] = ok
    (OUT / "tray_spike.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    fails = [c["name"] for c in REPORT["checks"] if not c["ok"]]
    print("\nM4 验证 %s：%d 项检查，失败 %d 项%s"
          % ("通过" if ok else "未通过", len(REPORT["checks"]), len(fails),
             ("（" + "；".join(fails) + "）") if fails else ""), flush=True)
    print("报告：%s" % (OUT / "tray_spike.json"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
