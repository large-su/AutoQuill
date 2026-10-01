"""Windows regression tests for the real PowerShell update host.

These tests deliberately start powershell.exe and real temporary .exe files.  The
fake binaries only write marker files and never touch the network or an installed
AutoQuill copy.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import paths, update_stage, updater


_FAKE_CS = r'''
using System;
using System.IO;
class Fake {
  static int Main(string[] args) {
    string marker = Environment.GetEnvironmentVariable("AQ_FAKE_MARKER") ?? "";
    string role = Environment.GetEnvironmentVariable("AQ_FAKE_ROLE") ?? "";
    if (AppDomain.CurrentDomain.FriendlyName.ToLowerInvariant().Contains("relauncher")) role = "relauncher";
    if (marker != "") File.AppendAllText(marker, role + Environment.NewLine);
    string environmentMarker = Environment.GetEnvironmentVariable("AQ_FAKE_ENV_MARKER") ?? "";
    if (environmentMarker != "") {
      File.AppendAllText(environmentMarker, role + "|" +
        (Environment.GetEnvironmentVariable("TEMP") ?? "<missing>") + "|" +
        (Environment.GetEnvironmentVariable("TMP") ?? "<missing>") + Environment.NewLine);
    }
    if (role == "installer" && Environment.GetEnvironmentVariable("AQ_FAKE_INSTALL_OK") == "1") {
      string target = Environment.GetEnvironmentVariable("AQ_FAKE_TARGET") ?? "";
      if (target != "") File.WriteAllText(target, "upgraded");
    }
    if (role == "installer") return int.Parse(Environment.GetEnvironmentVariable("AQ_FAKE_EXIT") ?? "0");
    return 0;
  }
}
'''


class UpdateHostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not cls.powershell:
            raise unittest.SkipTest("Windows PowerShell is required")
        cls.build = Path(tempfile.mkdtemp(prefix="aq-host-bins-"))
        cls.installer = cls._compile("installer.exe")
        cls.relauncher = cls._compile("relauncher.exe")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "build", ""), ignore_errors=True)

    @classmethod
    def _compile(cls, name):
        out = cls.build / name
        source = base64.b64encode(_FAKE_CS.encode("utf-8")).decode("ascii")
        script = "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')); Add-Type -TypeDefinition $s -OutputType ConsoleApplication -OutputAssembly '%s'" % (source, out)
        result = subprocess.run([cls.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode != 0 or not out.exists():
            raise RuntimeError("could not compile fake executable: %s" % result.stderr)
        return out

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="aq-host-test-"))
        self.data_root = self.temp / "data root 中文"
        self.install_dir = self.temp / "install dir 中文"
        self.data_root.mkdir(parents=True)
        self.install_dir.mkdir(parents=True)
        self.marker = self.temp / "events.log"
        self.installer = self.temp / "installer.exe"
        shutil.copy2(type(self).installer, self.installer)
        self.target = self.install_dir / "AutoQuill.exe"
        self.target.write_text("old", encoding="utf-8")
        self.stage_path = self.data_root / "data" / "update" / "stage.json"
        self.stage_path.parent.mkdir(parents=True, exist_ok=True)
        self.old_data_root = paths.DATA_ROOT
        self.old_aq_data = os.environ.get("AQ_DATA_DIR")
        paths.DATA_ROOT = str(self.data_root)
        os.environ["AQ_DATA_DIR"] = str(self.data_root)

    def tearDown(self):
        paths.DATA_ROOT = self.old_data_root
        if self.old_aq_data is None:
            os.environ.pop("AQ_DATA_DIR", None)
        else:
            os.environ["AQ_DATA_DIR"] = self.old_aq_data
        shutil.rmtree(self.temp, ignore_errors=True)

    def _run(self, *, exit_code="0", install_ok="1", dry_run=False,
             expected_hash="", expected_version="", pid=0, wait_seconds=3,
             runtime_url="", restart_only=False, environment=None):
        log = self.temp / "apply log 中文.log"
        env = os.environ.copy()
        env.update({"AQ_FAKE_MARKER": str(self.marker), "AQ_FAKE_TARGET": str(self.target),
                    "AQ_FAKE_ROLE": "installer", "AQ_FAKE_EXIT": exit_code,
                    "AQ_FAKE_INSTALL_OK": install_ok})
        if environment:
            env.update(environment)
        script = updater.powershell_host_script(
            self.installer, pid, self.install_dir, log, self.relauncher,
            wait_seconds=wait_seconds, stage_path=self.stage_path,
            expected_sha256=expected_hash, expected_version=expected_version,
            dry_run=dry_run, restart_only=restart_only, runtime_url=runtime_url)
        host_file = self.temp / "host.ps1"
        host_file.write_text(script, encoding="utf-8-sig")
        return subprocess.run([self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(host_file)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)

    def _events(self):
        return self.marker.read_text(encoding="utf-8").splitlines() if self.marker.exists() else []

    def _wait_events(self, count):
        deadline = time.time() + 3
        while time.time() < deadline and len(self._events()) < count:
            time.sleep(0.05)
        return self._events()

    def test_success_installs_then_relaunches(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._wait_events(2), ["installer", "relauncher"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "upgraded")

    def test_failing_installer_does_not_restart_old_version_and_persists_failed(self):
        installer_copy = self.temp / "installer-copy.exe"
        shutil.copy2(self.installer, installer_copy)
        self.installer = installer_copy
        self.stage_path.write_text(json.dumps({"stage": update_stage.STAGE_APPLYING,
                                               "installer": str(self.installer)}), encoding="utf-8")
        result = self._run(exit_code="7", install_ok="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._events(), ["installer"])
        stage_file = Path(update_stage.stage_file())
        self.assertTrue(stage_file.exists())
        self.assertEqual(json.loads(stage_file.read_text(encoding="utf-8"))["stage"], update_stage.STAGE_FAILED)
        self.assertTrue(self.installer.exists())

    def test_paths_with_spaces_chinese_and_apostrophes_survive(self):
        self.data_root = self.temp / "data root 中文 'quoted'"
        self.install_dir = self.temp / "install dir 中文 'quoted'"
        self.data_root.mkdir()
        self.install_dir.mkdir()
        self.marker = self.temp / "events 'quoted'.log"
        self.target = self.install_dir / "AutoQuill.exe"
        self.target.write_text("old", encoding="utf-8")
        self.stage_path = self.data_root / "data" / "update" / "stage.json"
        self.stage_path.parent.mkdir(parents=True, exist_ok=True)
        os.environ["AQ_DATA_DIR"] = str(self.data_root)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.target.read_text(encoding="utf-8"), "upgraded")

    def test_dry_run_does_not_install(self):
        result = self._run(dry_run=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.target.read_text(encoding="utf-8"), "old")

    def test_installer_uses_dedicated_temp_and_relauncher_inherits_original_environment(self):
        environment_marker = self.temp / "process-environments.log"
        bad_temp = self.temp / "missing temp"
        bad_tmp = self.temp / "not-a-directory.tmp"
        bad_tmp.write_text("not a directory", encoding="utf-8")
        result = self._run(environment={
            "TEMP": str(bad_temp),
            "TMP": str(bad_tmp),
            "AQ_FAKE_ENV_MARKER": str(environment_marker),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        expected_temp = self.stage_path.parent / "installer-temp"
        self.assertEqual(self._wait_events(2), ["installer", "relauncher"])
        self.assertEqual(environment_marker.read_text(encoding="utf-8").splitlines(), [
            "installer|{0}|{0}".format(expected_temp),
            "relauncher|{0}|{1}".format(bad_temp, bad_tmp),
        ])
        self.assertTrue(expected_temp.is_dir())

    def test_dry_run_does_not_start_installer_with_dedicated_temp(self):
        environment_marker = self.temp / "process-environments.log"
        result = self._run(dry_run=True, environment={
            "AQ_FAKE_ENV_MARKER": str(environment_marker),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(environment_marker.exists())

    def test_restart_only_does_not_create_or_inherit_installer_temp(self):
        environment_marker = self.temp / "process-environments.log"
        original_temp = self.temp / "restart temp"
        original_tmp = self.temp / "restart-tmp.tmp"
        result = self._run(restart_only=True, environment={
            "TEMP": str(original_temp),
            "TMP": str(original_tmp),
            "AQ_FAKE_ENV_MARKER": str(environment_marker),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._wait_events(1), ["relauncher"])
        self.assertEqual(environment_marker.read_text(encoding="utf-8").splitlines(), [
            "relauncher|{0}|{1}".format(original_temp, original_tmp),
        ])
        self.assertFalse((self.stage_path.parent / "installer-temp").exists())

    def test_unusable_installer_temp_fails_before_ready_or_process_exit(self):
        dedicated_temp = self.stage_path.parent / "installer-temp"
        dedicated_temp.write_text("a file prevents directory creation", encoding="utf-8")
        ready_file = self.temp / "ready.json"
        result = self._run(environment={
            "AQ_UPDATE_READY_FILE": str(ready_file),
            "AQ_UPDATE_TOKEN": "test-ready-token",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(ready_file.exists())
        self.assertEqual(self._events(), [])
        state = json.loads(self.stage_path.read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], update_stage.STAGE_FAILED)

    def test_wrong_checksum_does_not_execute_installer(self):
        result = self._run(expected_hash="0" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._events(), [])
        self.assertEqual(json.loads(self.stage_path.read_text(encoding="utf-8"))["stage"], update_stage.STAGE_FAILED)
        self.assertTrue(self.installer.exists())

    def test_manifest_mismatch_does_not_relaunch_or_cleanup(self):
        result = self._run(expected_version="9.9.9")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._events(), ["installer"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "upgraded")
        self.assertEqual(json.loads(self.stage_path.read_text(encoding="utf-8"))["stage"], update_stage.STAGE_FAILED)
        self.assertTrue(self.installer.exists())

    def test_live_parent_times_out_before_install(self):
        result = self._run(pid=os.getpid(), wait_seconds=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._events(), [])
        self.assertEqual(json.loads(self.stage_path.read_text(encoding="utf-8"))["stage"], update_stage.STAGE_FAILED)

    def test_utf8_runtime_status_completes_unicode_install(self):
        install_dir = self.temp / "安装目录 'quoted' 中文"
        install_dir.mkdir()
        internal = install_dir / "_internal"
        internal.mkdir()
        (internal / "build_info.json").write_text(
            json.dumps({"version": "9.9.9"}), encoding="utf-8")
        self.install_dir = install_dir

        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({
                    "running_version": "9.9.9",
                    "running_pid": 424242,
                    "running_install_dir": str(install_dir),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self._run(
                expected_version="9.9.9",
                runtime_url="http://127.0.0.1:%d/status" % server.server_port)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(self.stage_path.read_text(encoding="utf-8"))
            self.assertEqual(state["stage"], update_stage.STAGE_DONE)
            self.assertEqual(state["installed_version"], "9.9.9")
            self.assertEqual(self._wait_events(2), ["installer", "relauncher"])
            self.assertFalse(self.installer.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
