"""Update handoff must mean the worker started, with isolated runtime state."""
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import paths, update_stage as stage
from webui import update_api


class UpdateLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aq_lifecycle_")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(paths, "DATA_ROOT", str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"AQ_DATA_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_status_does_not_fail_an_active_install(self):
        stage.update(stage=stage.STAGE_APPLYING, host_pid=os.getpid())
        status = update_api.api_update_status()
        self.assertEqual(status["stage"], stage.STAGE_APPLYING)
        self.assertEqual(stage.load()["stage"], stage.STAGE_APPLYING)

    def test_manual_bootstrap_reconciles_old_broken_update(self):
        from core.version import VERSION
        for old_stage in (stage.STAGE_STAGED, stage.STAGE_APPLYING, stage.STAGE_FAILED):
            with self.subTest(old_stage=old_stage):
                stage.update(stage=old_stage, version="1.0.0", operation="update",
                             install_dir=str(self.root), host_pid=0, error="old failure")
                with mock.patch.object(update_api.updater, "current_install_dir",
                                       return_value=str(self.root)):
                    status = update_api.api_update_status()
                self.assertEqual(status["stage"], stage.STAGE_DONE)
                self.assertEqual(status["installed_version"], VERSION)
                self.assertEqual(status["error"], "")

    def test_old_running_version_does_not_claim_new_update_installed(self):
        stage.update(stage=stage.STAGE_STAGED, version="99.0.0", operation="update",
                     install_dir=str(self.root), host_pid=0)
        with mock.patch.object(update_api.updater, "current_install_dir", return_value=str(self.root)):
            status = update_api.api_update_status()
        self.assertEqual(status["stage"], stage.STAGE_STAGED)

    @unittest.skipUnless(os.name == "nt", "Windows WMI process handoff")
    def test_spawn_means_actual_worker_ran(self):
        # No installation or quitting: a finite worker writes a distinct marker.
        marker = self.root / "worker-started.txt"
        script = ("[IO.File]::WriteAllText('%s', 'started'); "
                  "$r=@{pid=$PID;token=$env:AQ_UPDATE_TOKEN}|ConvertTo-Json; "
                  "[IO.File]::WriteAllText($env:AQ_UPDATE_READY_FILE,$r)") % marker
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
               "-EncodedCommand", encoded]
        ok, detail = update_api._spawn_detached_host(cmd, stage.log_file())
        deadline = time.monotonic() + 4
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(ok, detail)
        self.assertTrue(marker.exists(), "reported ready but worker never ran")


if __name__ == "__main__":
    unittest.main()
