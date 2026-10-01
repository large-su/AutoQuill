# -*- coding: utf-8 -*-
"""Focused regression tests for the update UI state machine.

The test executes the actual update block from webui/static/app.js in a
small Node VM. It deliberately supplies only DOM/fetch/timer doubles, so it
does not open a browser or contact the network.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "webui" / "static" / "app.js"


def run_ui_case(case, release=None):
    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(%s, "utf8");
const start = source.indexOf("/* ---------- 检查更新 ---------- */");
const blockEnd = source.indexOf('$("btnEdgeOk").addEventListener', start);
if (start < 0 || blockEnd < 0) throw new Error("update block markers missing");

const timers = [];
const fetches = [];
let confirms = 0, alerts = 0;
const btn = {
  disabled: false, textContent: "检查更新", title: "", dataset: {},
  classList: { add() {}, remove() {} },
  addEventListener() {}
};
const elements = {btnUpdate: btn, btnRestart: {addEventListener() {}},
                  btnEdgeOk: {addEventListener() {}}};
const jsonResponse = (value) => ({
  ok: true, status: 200,
  text: async () => JSON.stringify(value)
});
let status = {stage: "staged", version: "5.0.6",
              auto_install: true, running_version: "5.0.5"};
const context = {
  console,
  window: {open() {}},
  document: {},
  localStorage: {getItem() { return null; }, setItem() {}},
  $: (id) => elements[id] || {addEventListener() {}},
  setTimeout: (fn) => { fn(); return 1; },
  clearTimeout() {},
  setInterval: (fn) => { timers.push(fn); return timers.length; },
  clearInterval() {},
  confirm: () => { confirms++; return true; },
  alert: () => { alerts++; },
  fetch: async (url, opts) => {
    fetches.push({url, method: (opts && opts.method) || "GET"});
    if (url === "/api/update/check" && %s === "startup-error")
      throw new Error("offline");
    if (url === "/api/update/check")
      return jsonResponse(%s);
    if (url.startsWith("/api/update/download"))
      return jsonResponse({ok: true, version: "5.0.6"});
    if (url === "/api/update/status")
      return jsonResponse(status);
    if (url === "/api/update/apply")
      return jsonResponse({ok: true});
    throw new Error("unexpected URL " + url);
  }
};
const end = source.indexOf('$("btnKeySave"', blockEnd);
vm.runInNewContext(source.slice(start, end),
                   context, {timeout: 1000});

(async () => {
  if (%s === "manual") {
    await context.checkUpdate();
    for (const timer of timers) await timer();
  } else if (%s === "startup" || %s === "startup-error") {
    await context.checkUpdateOnStartup();
    await context.checkUpdateOnStartup();
  } else if (%s === "active-startup") {
    btn.disabled = true;
    btn.textContent = "下载中";
    await context.checkUpdateOnStartup();
  } else if (%s === "restore") {
    await context.restoreUpdateStatus();
    for (const timer of timers) await timer();
  }
  process.stdout.write(JSON.stringify({
    confirms, alerts, fetches, disabled: btn.disabled,
    text: btn.textContent, timers: timers.length
  }));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
""" % (
        json.dumps(str(APP_JS)),
        json.dumps(case),
        json.dumps(release if release is not None else {
            "current": "5.0.5", "latest": "5.0.6", "has_update": True, "channel": "api"}),
        json.dumps(case),
        json.dumps(case),
        json.dumps(case),
        json.dumps(case),
        json.dumps(case),
    )
    completed = subprocess.run(["node", "-e", script], cwd=ROOT,
                               capture_output=True, text=True, encoding="utf-8",
                               timeout=30)
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    return json.loads(completed.stdout)


@unittest.skipUnless(shutil.which("node"), "Node is required for UI regression tests")
class UpdateUiTest(unittest.TestCase):
    def test_confirmed_update_uses_one_confirmation_and_auto_install_download(self):
        got = run_ui_case("manual")
        self.assertEqual(got["confirms"], 1)
        self.assertEqual(got["alerts"], 0)
        self.assertEqual(got["fetches"][0]["url"], "/api/update/check")
        self.assertEqual(got["fetches"][1]["url"],
                         "/api/update/download?auto_install=true")
        self.assertGreaterEqual(
            [x["url"] for x in got["fetches"]].count("/api/update/status"), 1)
        self.assertNotIn("/api/update/apply",
                         [x["url"] for x in got["fetches"]])
        self.assertTrue(got["disabled"])

    def test_startup_check_is_once_and_only_marks_available(self):
        got = run_ui_case("startup")
        self.assertEqual(got["confirms"], 0)
        self.assertEqual(got["alerts"], 0)
        self.assertEqual(len(got["fetches"]), 1)
        self.assertIn("发现 v5.0.6", got["text"])
        self.assertFalse(got["disabled"])

    def test_startup_check_does_not_overwrite_active_update(self):
        got = run_ui_case("active-startup")
        self.assertEqual(got["confirms"], 0)
        self.assertEqual(got["alerts"], 0)
        self.assertEqual(got["text"], "下载中")
        self.assertTrue(got["disabled"])

    def test_startup_network_failure_stays_silent(self):
        got = run_ui_case("startup-error")
        self.assertEqual((got["confirms"], got["alerts"]), (0, 0))
        self.assertEqual(got["text"], "检查更新")
        self.assertEqual(len(got["fetches"]), 1)

    def test_startup_no_update_stays_silent(self):
        got = run_ui_case("startup", {"has_update": False})
        self.assertEqual((got["confirms"], got["alerts"]), (0, 0))
        self.assertEqual(got["text"], "检查更新")

    def test_restore_auto_install_stays_disabled_without_confirmation(self):
        got = run_ui_case("restore")
        self.assertEqual(got["confirms"], 0)
        self.assertEqual(got["alerts"], 0)
        self.assertTrue(got["disabled"])
        self.assertEqual(got["text"], "准备安装并重启…")


if __name__ == "__main__":
    unittest.main()
