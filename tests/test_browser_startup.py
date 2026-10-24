# -*- coding: utf-8 -*-
"""Web server startup ordering regression tests."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class BrowserStartupOrderTest(TestCase):

    def _env(self, prefix):
        temporary = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name)
        config_dir = data_dir / "config"
        config_dir.mkdir()
        shutil.copyfile(
            ROOT / "config" / "llm_providers.example.json",
            config_dir / "llm_providers.json",
        )
        env = os.environ.copy()
        env["AQ_DATA_DIR"] = str(data_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def test_run_registers_browser_factory_before_scheduler_recovery(self):
        script = textwrap.dedent(
            """
            import sys
            from unittest import mock

            import web_drivers.browser_pool as pool

            assert pool._factory is None, pool._factory
            import webui.server as server
            assert pool._factory is None, pool._factory

            seen = {}

            class Scheduler:
                def ensure_running(self):
                    browser = pool.create_browser(headless=True)
                    seen["browser"] = browser
                    seen["started"] = browser.context is not None

            with mock.patch("automation.scheduler.get_scheduler",
                            return_value=Scheduler()), \
                 mock.patch("uvicorn.run"):
                server.run(host="127.0.0.1", port=0)

            browser = seen["browser"]
            assert type(browser).__name__ == "ZhihuBrowser", type(browser)
            assert seen["started"] is False, seen
            print("startup-order-ok")
            """
        )
        env = self._env("aq-startup-")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("startup-order-ok", result.stdout)

    def test_importing_server_does_not_start_scheduler_or_browser(self):
        script = textwrap.dedent(
            """
            import web_drivers.browser_pool as pool
            import webui.server

            assert pool._factory is None, pool._factory
            assert pool.get_shared_browser() is None
            print("import-only-ok")
            """
        )
        env = self._env("aq-import-")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("import-only-ok", result.stdout)

