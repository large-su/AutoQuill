# -*- coding: utf-8 -*-
"""残留 Edge profile 进程清理的回归测试。"""

from types import SimpleNamespace
import json
import os
import tempfile
from unittest import TestCase, mock

from applications.zhihu_story import browser_session as session
import web_drivers.browser_pool as bp


class StaleProfileProcessCleanupTest(TestCase):

    def _run(self, result, profile=r"C:\AQ\DeepSeek 'profile"):
        with mock.patch.object(session.os, "name", "nt"), \
                mock.patch("web_drivers.browser_pool.live_browsers", return_value=0), \
                mock.patch("desktop_utils.run_process_silent", side_effect=result) as run:
            session._kill_stale_profile_processes(profile)
        return run

    def test_active_instance_is_never_killed(self):
        with mock.patch.object(session.os, "name", "nt"), \
                mock.patch("web_drivers.browser_pool.live_browsers", return_value=1), \
                mock.patch("desktop_utils.run_process_silent") as run:
            session._kill_stale_profile_processes(r"C:\AQ\profile")
        run.assert_not_called()

    def test_query_failure_does_not_kill_and_is_logged(self):
        with self.assertLogs(session.log, level="WARNING") as logs:
            run = self._run([SimpleNamespace(returncode=1, stdout="", stderr="access denied")])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "powershell")
        self.assertIn("查询残留 Edge 进程失败", "\n".join(logs.output))

    def test_multiple_pids_are_each_passed_to_taskkill(self):
        run = self._run([
            SimpleNamespace(returncode=0, stdout="101\n202\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0],
                         ["taskkill", "/F", "/PID", "101", "/PID", "202"])

    def test_cleanup_failure_is_visible(self):
        with self.assertLogs(session.log, level="ERROR") as logs:
            run = self._run([
                SimpleNamespace(returncode=0, stdout="101\n", stderr=""),
                SimpleNamespace(returncode=5, stdout="", stderr="拒绝访问"),
            ])
        self.assertEqual(run.call_count, 2)
        self.assertIn("清理残留 Edge 进程失败", "\n".join(logs.output))

    def test_profile_path_is_quoted_and_escaped_in_query(self):
        run = self._run([
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ], profile=r"C:\AQ\O'Brien\profile")
        script = run.call_args.args[0][-1]
        self.assertIn("O''Brien", script)
        self.assertIn("--user-data-dir=", script)


class StorageRecoveryTest(TestCase):

    def tearDown(self):
        while bp.profile_in_use():
            bp.release_profile()
        while bp.live_browsers() > 0:
            bp.note_browser_closed()

    def _browser(self, path):
        b = session.SessionMixin.__new__(session.SessionMixin)
        b.storage_state = path
        b.context = mock.MagicMock()
        b._lease_held = False
        return b

    def test_empty_or_corrupt_snapshot_does_not_block_profile(self):
        for content in (b"", b"{broken", b"[]", b'{"cookies": {}}'):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as root:
                    path = os.path.join(root, "browser_state.json")
                    with open(path, "wb") as f:
                        f.write(content)
                    b = self._browser(path)
                    self.assertFalse(b.load_storage_state())
                    b.context.add_cookies.assert_not_called()

    def test_atomic_write_failure_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "browser_state.json")
            original = b'{"cookies":[{"name":"old"}]}'
            with open(path, "wb") as f:
                f.write(original)
            b = self._browser(path)
            b.context.storage_state.return_value = {"cookies": [{"name": "new"}]}
            with mock.patch.object(session.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    b.save_storage_state()
            with open(path, "rb") as f:
                self.assertEqual(f.read(), original)

    def test_failed_start_closes_context_and_driver_without_live_decrement(self):
        b = session.SessionMixin.__new__(session.SessionMixin)
        b.context = None
        b.page = None
        b._pw = None
        b._lease_held = False
        context = mock.MagicMock()
        driver = mock.MagicMock()

        def fail_start():
            b.context = context
            b._pw = driver
            raise RuntimeError("load state failed")

        with mock.patch("web_drivers.browser_pool.acquire_profile", return_value=True), \
                mock.patch.object(b, "_start_locked", side_effect=fail_start):
            with self.assertRaises(RuntimeError):
                b.start()
        context.close.assert_called_once()
        driver.stop.assert_called_once()
        self.assertEqual(bp.live_browsers(), 0)

