import hashlib
import json
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import build_release, release


class ReleaseFlowTests(unittest.TestCase):
    def test_message_prefix_replaced_and_body_preserved(self):
        self.assertEqual(release.normalize_message("feat: make it faster\n\nDetails", "5.0.7"),
                         "v5.0.7: make it faster\n\nDetails")
        self.assertEqual(release.normalize_message("v4.1.0: old title", "5.0.7"),
                         "v5.0.7: old title")

    def test_known_and_unknown_test_selection(self):
        self.assertEqual(release.select_tests(["core/update_api.py"]), ["tests.test_updater"])
        self.assertEqual(release.select_tests(["core/surprise_runtime.py"]), ["tests"])
        self.assertEqual(release.select_tests(["README.md", "core/version.py"]), [])
        self.assertEqual(release.select_tests(["README.md"], full=True), ["tests"])

    def test_local_hash_mismatch_is_detectable(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "x.exe"
            sha = exe.with_suffix(exe.suffix + ".sha256")
            exe.write_bytes(b"one")
            sha.write_text(hashlib.sha256(b"two").hexdigest(), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "sha256 不匹配"):
                release.validate_local_assets(exe, sha)

    def test_metadata_sync_updates_inno_and_bom_readme_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "installer").mkdir()
            iss = root / "installer/AutoQuill.iss"
            iss.write_text('#define MyAppVersion "5.0.6"\n', encoding="utf-8")
            readme = root / "README.md"
            readme.write_text('\ufeff# AutoQuill v5.0.6\n| **当前版本** | v5.0.6 |\n'
                              'certutil -hashfile AutoQuill-Setup-5.0.6.exe SHA256\n', encoding="utf-8")
            with mock.patch.object(build_release, "ROOT", root), \
                    mock.patch.object(build_release, "version", return_value="5.0.7"):
                build_release.sync_release_metadata()
            self.assertIn('MyAppVersion "5.0.7"', iss.read_text())
            self.assertTrue(readme.read_text(encoding="utf-8").startswith('\ufeff# AutoQuill v5.0.7'))
            self.assertIn('AutoQuill-Setup-5.0.7.exe', readme.read_text(encoding='utf-8'))

    def test_dirty_paths_are_kept_alongside_previous_release_diff(self):
        with mock.patch.object(release, "out", side_effect=[
                'webui/static/app.js', 'core/updater.py', 'tests/test_update_ui.py']), \
                mock.patch.object(release, "capture", return_value=SimpleNamespace(
                    returncode=0, stdout='v5.0.6\n')):
            got = release.changed_paths()
        self.assertEqual(set(got), {'webui/static/app.js', 'core/updater.py', 'tests/test_update_ui.py'})

    def test_full_check_uses_safe_unified_entry_and_targeted_checks_once(self):
        with mock.patch.object(release, "run"), mock.patch.object(release, "run_tests") as check:
            release.run_checks([], ['tests'])
        self.assertEqual(check.call_args_list[0].args[0],
                         [release.sys.executable, str(release.ROOT / 'tests/run_all.py')])
        with mock.patch.object(release, "run"), mock.patch.object(release, "run_tests") as check:
            release.run_checks([], ['tests.test_updater', 'tests.test_release_flow'])
        self.assertEqual(check.call_args_list[0].args[0],
                         [release.sys.executable, '-m', 'unittest', '-q', 'tests.test_updater', 'tests.test_release_flow'])

    def test_failed_checks_keep_log_and_stop_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(release, 'ROOT', root), \
                    mock.patch.object(release, 'version', return_value='5.0.7'), \
                    mock.patch.object(release.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=1, stdout='', stderr='FAILED (errors=1)')):
                with self.assertRaisesRegex(RuntimeError, '测试失败'):
                    release.run_tests(['python', 'tests/run_all.py'])
            self.assertIn('FAILED', (root / 'logs/release-checks-5.0.7.log').read_text())

    def test_create_release_uploads_only_two_assets_and_version_first_title(self):
        assets = release.release_assets('5.0.7')
        with mock.patch.object(release, 'run') as run:
            release.create_release('v5.0.7', Path('notes.md'), assets)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], list(map(str, assets)))
        self.assertEqual(command[command.index('--title') + 1], 'v5.0.7 — AutoQuill')
        self.assertTrue(all(not str(part).endswith('.zip') for part in command))

    def test_plan_has_no_network_mutations_or_message_requirement(self):
        with mock.patch.object(release, 'version', return_value='5.0.7'), \
                mock.patch.object(release, 'changed_paths', return_value=[]), \
                mock.patch.object(release, 'out', return_value=''), \
                mock.patch.object(release, 'run') as run, \
                mock.patch.object(release, '_preflight') as preflight, \
                mock.patch.object(build_release, 'sync_release_metadata') as sync:
            self.assertEqual(release.main(['--plan']), 0)
        run.assert_not_called()
        preflight.assert_not_called()
        sync.assert_not_called()

    def test_published_digest_mismatch_is_rejected_without_full_download(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / 'AutoQuill-Setup-5.0.7.exe'
            exe.write_bytes(b'package')
            sha = exe.with_suffix('.exe.sha256')
            sha.write_text(release._sha256(exe), encoding='utf-8')
            payload = {'assets': [
                {'name': exe.name, 'size': exe.stat().st_size, 'digest': 'sha256:' + '0' * 64},
                {'name': sha.name, 'size': 64, 'id': 1}]}
            with mock.patch.object(release, 'out', return_value=json.dumps(payload)), \
                    mock.patch.object(release, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'digest'):
                    release.validate_published_assets('v5.0.7', exe, sha)
            run.assert_not_called()

    def test_critical_installation_paths_select_lifecycle_tests(self):
        got = release.select_tests(['core/update_host.ps1', 'tools/build_release.py'])
        self.assertIn('tests.test_update_host_integration', got)
        self.assertIn('tests.test_update_lifecycle', got)
        self.assertIn('tests.test_release_flow', got)

    def test_main_builds_once_commits_once_and_pushes_only_current_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release').mkdir()
            notes = root / 'release/release_notes_5.0.7.md'
            notes.write_text('# v5.0.7', encoding='utf-8')
            exe = root / 'release/AutoQuill-Setup-5.0.7.exe'
            exe.write_bytes(b'package')
            exe.with_suffix('.exe.sha256').write_text(release._sha256(exe), encoding='utf-8')
            with mock.patch.object(release, 'ROOT', root), \
                    mock.patch.object(release, 'version', return_value='5.0.7'), \
                    mock.patch.object(release, 'changed_paths', return_value=[]), \
                    mock.patch.object(release, '_preflight'), \
                    mock.patch.object(build_release, 'sync_release_metadata'), \
                    mock.patch.object(release, 'run_checks'), \
                    mock.patch.object(release, 'commit_release') as commit, \
                    mock.patch.object(release, 'validate_published_assets'), \
                    mock.patch.object(release, 'run') as run:
                self.assertEqual(release.main(['-m', 'fix: new behavior']), 0)
            commit.assert_called_once_with('fix: new behavior', '5.0.7')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(sum('build_release.py' in str(part) for cmd in commands for part in cmd), 1)
            self.assertIn(['git', 'push', '--atomic', 'origin', 'main', 'v5.0.7'], commands)
            self.assertFalse(any('--tags' in cmd or 'delete' in cmd for cmd in commands))
            upload = next(cmd for cmd in commands if cmd[:3] == ['gh', 'release', 'create'])
            self.assertEqual(upload[-2:], [str(exe), str(exe.with_suffix('.exe.sha256'))])

    def test_explicit_full_download_uses_private_dir_and_rejects_bad_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / 'AutoQuill-Setup-5.0.7.exe'
            exe.write_bytes(b'package')
            sha = exe.with_suffix('.exe.sha256')
            sha.write_text(release._sha256(exe), encoding='utf-8')
            payload = {'assets': [
                {'name': exe.name, 'size': 7, 'digest': 'sha256:' + sha.read_text()},
                {'name': sha.name, 'size': 64, 'id': 1}]}
            def download(cmd):
                target = Path(cmd[cmd.index('--dir') + 1])
                self.assertNotEqual(target, exe.parent)
                (target / exe.name).write_bytes(b'corrupt')
            with mock.patch.object(release, 'out', side_effect=[json.dumps(payload), sha.read_text()]), \
                    mock.patch.object(release, 'run', side_effect=download):
                with self.assertRaisesRegex(RuntimeError, '回下载'):
                    release.validate_published_assets('v5.0.7', exe, sha, verify_download=True)

    def test_build_reuse_requires_matching_version_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'dist/AutoQuill/_internal/build_info.json'
            manifest.parent.mkdir(parents=True)
            with mock.patch.object(release, 'ROOT', root), \
                    mock.patch.object(release, 'version', return_value='5.0.7'), \
                    mock.patch.object(release, 'out', return_value='current-commit'):
                for data in ({'version': '5.0.6', 'source_commit': 'current-commit'},
                             {'version': '5.0.7', 'source_commit': 'old-commit'}):
                    manifest.write_text(json.dumps(data), encoding='utf-8')
                    with self.assertRaisesRegex(RuntimeError, '不一致'):
                        release._validate_manifest()
                manifest.write_text(json.dumps({'version': '5.0.7', 'source_commit': 'current-commit'}), encoding='utf-8')
                release._validate_manifest()

    def test_preflight_refuses_existing_tag_without_delete(self):
        with mock.patch.object(release, "out", side_effect=["main", "v5.0.6"]):
            with self.assertRaisesRegex(RuntimeError, "不会删除"):
                release._preflight("v5.0.6", plan=True)


if __name__ == "__main__":
    unittest.main()
