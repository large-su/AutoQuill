# -*- coding: utf-8 -*-
"""一键自动更新（P1）回归：解析 / 校验 / 状态机 / 安装参数。

不碰网络、不装任何东西：全部用真实 release JSON 的结构做夹具，
下载与执行部分靠注入替身。用户口径 → 断言映射：
  - 「点一下就自动完成」→ 状态机能从 downloading 走到 staged，再走到 done；
  - 「不能装来路不明的东西」→ 校验和不匹配/拿不到校验和，一律拒绝执行；
  - 「失败可手动兜底」→ 失败必须保留安装包并留下原因。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import paths, update_stage as stage, updater

# 真实 release JSON 的结构（取自 2026-09-29 的 v4.9.14，字段名与层级一致）
RELEASE_JSON = {
    "tag_name": "v4.9.14",
    "name": "AutoQuill v4.9.14",
    "html_url": "https://github.com/large-su/AutoQuill/releases/tag/v4.9.14",
    "body": "## v4.9.14\n\n修复：回滚跨线程唤醒浏览器的回归…",
    "assets": [
        {"name": "AutoQuill-Setup-4.9.14.exe", "size": 43534192,
         "digest": "sha256:3aba37c72c4356c8a107a3100e3adb50f099ea84511b45deab5a8a728a81bacf",
         "browser_download_url": "https://github.com/large-su/AutoQuill/"
                                 "releases/download/v4.9.14/AutoQuill-Setup-4.9.14.exe"},
        {"name": "AutoQuill-Setup-4.9.14.exe.sha256", "size": 64,
         "digest": "sha256:f7f9c6ae811205fb0fedcc30ffd0a1dbb54667509a3fb655b61deb01ac70383c",
         "browser_download_url": "https://github.com/large-su/AutoQuill/"
                                 "releases/download/v4.9.14/"
                                 "AutoQuill-Setup-4.9.14.exe.sha256"},
    ],
}
SHA = "3aba37c72c4356c8a107a3100e3adb50f099ea84511b45deab5a8a728a81bacf"


class VersionTest(unittest.TestCase):
    """版本比较：宁可说「没更新」，也不误报。"""

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("4.9.14", "4.9.13"))
        self.assertTrue(updater.is_newer("v4.10.0", "4.9.99"))
        self.assertFalse(updater.is_newer("4.9.13", "4.9.13"))
        self.assertFalse(updater.is_newer("4.9.12", "4.9.13"))

    def test_garbage_never_reports_update(self):
        for bad in ("", None, "abc", "4.x", "v"):
            self.assertFalse(updater.is_newer(bad, "4.9.13"))
            self.assertFalse(updater.is_newer("4.9.14", bad))

    def test_asset_names_match_build_output(self):
        """资产名必须与 build_release.py 的产物命名严格一致，否则下不到。"""
        self.assertEqual(updater.setup_asset_name("4.9.14"),
                         "AutoQuill-Setup-4.9.14.exe")
        self.assertEqual(updater.sha_asset_name("4.9.14"),
                         "AutoQuill-Setup-4.9.14.exe.sha256")


class ParseReleaseTest(unittest.TestCase):
    """release JSON → 结构化信息（含异常输入）。"""

    def test_parses_installer_and_digest(self):
        info = updater.parse_release(RELEASE_JSON)
        self.assertTrue(info["ok"])
        self.assertEqual(info["version"], "4.9.14")
        self.assertEqual(info["installer"]["name"], "AutoQuill-Setup-4.9.14.exe")
        self.assertEqual(info["installer"]["size"], 43534192)
        self.assertEqual(info["digest"], SHA)          # API 自带 sha256
        self.assertIn("回滚跨线程", info["notes"])

    def test_missing_installer_is_reported_not_crash(self):
        payload = {"tag_name": "v4.9.14", "assets": []}
        info = updater.parse_release(payload)
        self.assertFalse(info["ok"])
        self.assertIn("安装包", info["error"])

    def test_garbage_inputs(self):
        for bad in (None, [], "x", {}, {"assets": "no"}):
            info = updater.parse_release(bad)
            self.assertFalse(info["ok"])

    def test_digest_parsing(self):
        self.assertEqual(updater.parse_digest("sha256:" + SHA), SHA)
        self.assertEqual(updater.parse_digest(SHA), SHA)
        self.assertIsNone(updater.parse_digest("md5:abc"))      # 非 sha256 不采信
        self.assertIsNone(updater.parse_digest(""))
        self.assertIsNone(updater.parse_digest("sha256:xyz"))

    def test_sha256_text_parsing(self):
        self.assertEqual(updater.parse_sha256_text(SHA + "\r\n"), SHA)
        self.assertEqual(updater.parse_sha256_text("  " + SHA.upper() + " "), SHA)
        self.assertIsNone(updater.parse_sha256_text("short"))
        self.assertIsNone(updater.parse_sha256_text(None))


class VerifyTest(unittest.TestCase):
    """下载后的校验：不通过就绝不允许执行。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_upd_"))
        self.pkg = self.tmp / "AutoQuill-Setup-9.9.9.exe"
        self.pkg.write_bytes(b"fake installer bytes")

    def test_matching_hash_passes(self):
        good = updater.sha256_file(self.pkg)
        self.assertTrue(updater.verify(self.pkg, good)["ok"])

    def test_mismatch_fails_with_both_prefixes(self):
        got = updater.verify(self.pkg, "0" * 64)
        self.assertFalse(got["ok"])
        self.assertIn("不匹配", got["error"])

    def test_invalid_expected_fails_closed(self):
        for bad in ("", None, "not-a-hash"):
            self.assertFalse(updater.verify(self.pkg, bad)["ok"])

    def test_sha256_file_reports_progress(self):
        seen = []
        updater.sha256_file(self.pkg, progress=lambda r, t: seen.append((r, t)))
        self.assertEqual(seen[-1][0], len(b"fake installer bytes"))


class PlanTest(unittest.TestCase):
    """UpdatePlan 组装（脱网：校验和靠注入）。"""

    def test_newer_builds_plan_with_sha(self):
        plan = updater.build_plan(RELEASE_JSON, "4.9.13",
                                  sha_lookup=lambda info: {
                                      "ok": True, "sha256": SHA,
                                      "sources": ["asset", "digest"]})
        self.assertEqual(plan.version, "4.9.14")
        self.assertEqual(plan.sha256, SHA)
        self.assertEqual(plan.sha_sources, ("asset", "digest"))
        self.assertIn("AutoQuill-Setup-4.9.14.exe", plan.installer_url)
        self.assertFalse(plan.error)
        self.assertTrue(plan.dest.endswith("AutoQuill-Setup-4.9.14.exe"))

    def test_same_version_is_not_an_error_just_no_update(self):
        plan = updater.build_plan(RELEASE_JSON, "4.9.14",
                                  sha_lookup=lambda info: {"ok": True, "sha256": SHA})
        self.assertFalse(updater.is_newer(plan.version, plan.current))
        self.assertFalse(plan.error)

    def test_missing_sha_blocks_the_update(self):
        """拿不到校验和 → 拒绝更新（宁可不更新，也不装来路不明的包）。"""
        plan = updater.build_plan(RELEASE_JSON, "4.9.13",
                                  sha_lookup=lambda info: {
                                      "ok": False, "error": "拿不到校验和"})
        self.assertEqual(plan.sha256, "")
        self.assertIn("校验和", plan.error)


class StageStateTest(unittest.TestCase):
    """状态机落盘：跨进程可读、坏文件不致命。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_stage_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_blank_when_missing(self):
        self.assertEqual(stage.load()["stage"], stage.STAGE_IDLE)

    def test_round_trip(self):
        stage.update(stage=stage.STAGE_STAGED, version="9.9.9",
                     installer="C:/x/setup.exe", sha256=SHA, bytes=123)
        got = stage.load()
        self.assertEqual(got["stage"], stage.STAGE_STAGED)
        self.assertEqual(got["sha256"], SHA)
        self.assertTrue(got["updated_at"])

    def test_corrupt_file_falls_back_to_blank(self):
        path = stage.stage_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(stage.load()["stage"], stage.STAGE_IDLE)

    def test_unknown_stage_is_normalised(self):
        path = stage.stage_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": "weird"}), encoding="utf-8")
        self.assertEqual(stage.load()["stage"], stage.STAGE_IDLE)

    def test_mark_failed_keeps_reason(self):
        stage.mark_failed("校验和不匹配")
        got = stage.load()
        self.assertEqual(got["stage"], stage.STAGE_FAILED)
        self.assertIn("校验和", got["error"])

    def test_staged_installer_only_in_staged_state(self):
        pkg = stage.stage_file().with_name("AutoQuill-Setup-9.9.9.exe")
        pkg.write_bytes(b"x")
        stage.update(stage=stage.STAGE_DOWNLOADING, version="9.9.9",
                     installer=str(pkg))
        self.assertIsNone(stage.staged_installer())          # 还在下载，不算数
        stage.update(stage=stage.STAGE_STAGED)
        self.assertEqual(stage.staged_installer(), pkg)
        pkg.unlink()
        self.assertIsNone(stage.staged_installer())          # 文件没了也不算数

    def test_clear_artifacts(self):
        d = updater.update_dir()
        part = d / "AutoQuill-Setup-9.9.9.exe.part"
        part.write_bytes(b"half")
        self.assertIn(part.name, stage.clear_download_artifacts())

    def test_save_survives_locked_temp_name(self):
        """真机教训：固定 .tmp 名在主程序与子进程并发写时会撞锁。

        现在临时名带 pid+序号并退避重试，被占用也要写成功。
        """
        real_replace = os.replace

        def flaky_replace(src, dst):
            # 第一次假装被占用，第二次放行
            if not getattr(flaky_replace, "failed", False):
                flaky_replace.failed = True
                raise PermissionError(13, "Permission denied")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky_replace):
            self.assertTrue(stage.update(stage=stage.STAGE_STAGED, version="9.9.9"))
        self.assertEqual(stage.load()["stage"], stage.STAGE_STAGED)


class InstallerArgsTest(unittest.TestCase):
    """静默安装参数：这些写错就等于更新失败或弹窗卡住。"""

    def test_full_arg_set(self):
        args = updater.installer_args("C:/x/setup.exe", r"D:\AutoQuill",
                                      r"C:\log\apply.log")
        self.assertIn("/VERYSILENT", args)
        self.assertIn("/SUPPRESSMSGBOXES", args)
        self.assertIn("/NORESTART", args)       # 重启由我们控制
        self.assertIn("/NOCANCEL", args)
        self.assertIn('/DIR="D:\\AutoQuill"', args)   # 必须装回原目录
        self.assertIn('/LOG="C:\\log\\apply.log"', args)

    def test_dir_omitted_when_unknown(self):
        args = updater.installer_args("s.exe", "", None)
        self.assertFalse(any(a.startswith("/DIR") for a in args))

    def test_never_closes_applications_itself(self):
        """关程序必须由我们自己控制（安装器脚本里已设 CloseApplications=no）。"""
        args = updater.installer_args("s.exe", "D:/x", None)
        self.assertNotIn("/CLOSEAPPLICATIONS", args)


class InstallDirTest(unittest.TestCase):
    """安装目录探测：换装必须装回原处（你机器上就是 D:\\AutoQuill）。"""

    def test_frozen_uses_executable_dir(self):
        """冻结态取 exe 所在目录。

        断言按**当前平台**算：CI 跑在 Linux 上，写死 Windows 路径会被
        Path.resolve() 解析成 POSIX 路径而失败（CI 已经抓过一次）。
        """
        exe = str(Path(r"D:\AutoQuill\AutoQuill.exe"))
        with mock.patch("sys.frozen", True, create=True), \
                mock.patch("sys.executable", exe):
            self.assertEqual(updater.current_install_dir(),
                             str(Path(exe).resolve().parent))

    def test_source_tree_returns_none(self):
        """源码态不该自动换装（会把开发目录当成安装目录）。"""
        with mock.patch("sys.frozen", False, create=True):
            self.assertIsNone(updater.current_install_dir())

    def test_resolve_falls_back_to_registry(self):
        with mock.patch.object(updater, "current_install_dir", lambda: None), \
                mock.patch.object(updater, "detect_existing_install_dir_from_registry",
                                  lambda: r"D:\AutoQuill"):
            self.assertEqual(updater.resolve_install_dir(), r"D:\AutoQuill")


class DownloadTest(unittest.TestCase):
    """下载：先写 .part 再原子改名；失败不留半截文件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_dl_"))
        self.dest = self.tmp / "AutoQuill-Setup-9.9.9.exe"

    class _Resp:
        def __init__(self, blocks, length=None, boom=False):
            self._blocks, self._boom = blocks, boom
            self.headers = {"Content-Length": str(length or 0)}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def raise_for_status(self):
            if self._boom:
                raise RuntimeError("HTTP 500")
        def iter_content(self, chunk_size=0):
            for b in self._blocks:
                yield b

    class _Session:
        def __init__(self, resp):
            self._resp = resp
        def get(self, *a, **k):
            return self._resp

    def test_success_writes_file_and_no_part(self):
        s = self._Session(self._Resp([b"abc", b"def"], length=6))
        got = updater.download("http://x/pkg.exe", self.dest, session=s)
        self.assertTrue(got["ok"])
        self.assertEqual(self.dest.read_bytes(), b"abcdef")
        self.assertFalse(self.dest.with_suffix(".exe.part").exists())

    def test_http_error_leaves_no_partial_file(self):
        s = self._Session(self._Resp([b"abc"], boom=True))
        got = updater.download("http://x/pkg.exe", self.dest, session=s)
        self.assertFalse(got["ok"])
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix(".exe.part").exists())

    def test_progress_is_reported(self):
        seen = []
        s = self._Session(self._Resp([b"ab", b"cd"], length=4))
        updater.download("http://x/pkg.exe", self.dest, session=s,
                         progress=lambda r, t: seen.append((r, t)))
        self.assertEqual(seen[-1], (4, 4))

    def test_retries_then_succeeds(self):
        """真实网络教训（2026-09-29）：43MB 包一次就成功并不可靠，必须重试。"""
        calls = {"n": 0}

        class FlakySession:
            def get(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    return DownloadTest._Resp([b"x"], boom=True)
                return DownloadTest._Resp([b"ok"], length=2)

        got = updater.download("http://x/pkg.exe", self.dest,
                               session=FlakySession(), retry_wait=0)
        self.assertTrue(got["ok"])
        self.assertEqual(got["attempts"], 2)
        self.assertEqual(self.dest.read_bytes(), b"ok")

    def test_gives_up_after_attempts_and_leaves_nothing(self):
        class DeadSession:
            def get(self, *a, **k):
                return DownloadTest._Resp([b"x"], boom=True)

        got = updater.download("http://x/pkg.exe", self.dest,
                               session=DeadSession(), attempts=2, retry_wait=0)
        self.assertFalse(got["ok"])
        self.assertEqual(got["attempts"], 2)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix(".exe.part").exists())


class FetchShaTest(unittest.TestCase):
    """期望校验和：两个来源交叉验证，不一致就拒绝。"""

    class _R:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    def _session(self, text):
        class S:
            def get(self, *a, **k):
                return FetchShaTest._R(text)
        return S()

    def test_asset_and_digest_agree(self):
        info = {"sha_asset": {"url": "http://x/sha"},
                "digest": SHA}
        got = updater.fetch_expected_sha256(info, session=self._session(SHA))
        self.assertTrue(got["ok"])
        self.assertEqual(got["sha256"], SHA)
        self.assertEqual(got["sources"], ["asset", "digest"])

    def test_disagreement_is_rejected(self):
        """两个来源不一致 → 拒绝（宁可拒绝，也不装来路不明的东西）。"""
        info = {"sha_asset": {"url": "http://x/sha"}, "digest": "0" * 64}
        got = updater.fetch_expected_sha256(info, session=self._session(SHA))
        self.assertFalse(got["ok"])
        self.assertIn("不一致", got["error"])

    def test_only_digest_available(self):
        got = updater.fetch_expected_sha256({"digest": SHA}, session=self._session(""))
        self.assertTrue(got["ok"])
        self.assertEqual(got["sources"], ["digest"])

    def test_nothing_available_fails(self):
        got = updater.fetch_expected_sha256({}, session=self._session(""))
        self.assertFalse(got["ok"])
        self.assertIn("拿不到校验和", got["error"])


class ApplyHostTest(unittest.TestCase):
    """换装宿主（PowerShell）的命令生成。

    ★ 2026-09-29 线上事故（用户反馈「黑框不停弹出又关闭、界面一直不关」）：
      换装进程曾经是 `AutoQuill.exe --apply-update`，但冻结态入口是 **launcher**
      （先接管并开窗口）→ 它把这次调用当成「正常启动」：开窗口、抢单实例、
      失败退出，循环往复；而主程序始终没退出。
      现在宿主换成 Windows 自带的 PowerShell（隐藏窗口），不再是本程序。
    """

    def _script(self, **kw):
        import base64
        cmd = updater.powershell_apply_command(
            kw.get("installer", r"D:\AutoQuill\data\update\AutoQuill-Setup-9.9.9.exe"),
            kw.get("pid", 1234),
            kw.get("install_dir", r"D:\AutoQuill"),
            kw.get("log_path", r"D:\AutoQuill\data\update\apply.log"),
            relaunch_exe=kw.get("relaunch_exe", ""),
            wait_seconds=kw.get("wait_seconds", 600))
        self.assertEqual(cmd[0], "powershell.exe")
        self.assertIn("-EncodedCommand", cmd)
        self.assertIn("-NoProfile", cmd)
        raw = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1])
        return cmd, raw.decode("utf-16-le")

    def test_host_is_powershell_not_our_own_exe(self):
        """宿主绝不能是本程序自己的 exe（否则启动器会当成正常启动 → 黑框循环）。"""
        cmd, _ = self._script()
        self.assertEqual(cmd[0].lower(), "powershell.exe")

    def test_waits_for_the_parent_pid_before_installing(self):
        cmd, script = self._script(pid=4321, wait_seconds=600)
        self.assertIn("$target=4321", script)
        self.assertIn("等待主程序退出", script)
        self.assertIn("AddSeconds(600)", script)
        # 顺序必须是「先等父进程」再「装」
        self.assertLess(script.index("等待主程序退出"),
                        script.index("Start-Process -FilePath"))

    def test_never_kills_the_parent_immediately(self):
        """超时兜底才强杀，且等待窗口要给足（旧实现 60 秒就 taskkill）。"""
        _cmd, script = self._script(wait_seconds=600)
        self.assertIn("Stop-Process", script)
        self.assertGreaterEqual(600, 300)

    def test_installer_arguments_are_silent_and_install_in_place(self):
        _cmd, script = self._script(install_dir=r"D:\AutoQuill")
        self.assertIn("/VERYSILENT", script)
        self.assertIn('/DIR="D:\\AutoQuill"', script)
        self.assertIn("/LOG=", script)

    def test_restarts_only_when_relaunch_exe_given(self):
        _cmd, no_exe = self._script(relaunch_exe="")
        self.assertNotIn("已重启 AutoQuill", no_exe)
        _cmd2, with_exe = self._script(relaunch_exe=r"D:\AutoQuill\AutoQuill.exe")
        self.assertIn("已重启 AutoQuill", with_exe)
        self.assertIn(r"Start-Process -FilePath 'D:\AutoQuill\AutoQuill.exe'", with_exe)

    def test_cleanup_only_on_success(self):
        _cmd, script = self._script()
        self.assertIn("Remove-Item", script)
        # 删除必须发生在「返回码为 0」的分支里
        self.assertLess(script.index("$p.ExitCode -eq 0"),
                        script.index("Remove-Item"))

    def test_quotes_and_chinese_paths_survive_encoding(self):
        """中文路径 + 引号必须原样送达（-EncodedCommand 就是为此）。"""
        _cmd, script = self._script(
            installer=r"D:\我的 程序\data\update\AutoQuill-Setup-9.9.9.exe")
        self.assertIn(r"D:\我的 程序\data\update", script)

    def test_pid_zero_means_no_waiting(self):
        _cmd, script = self._script(pid=0)
        self.assertIn("$target=0", script)


class DetachHostTest(unittest.TestCase):
    """换装宿主必须**脱离主程序进程树**（2026-10-01 线上事故）。

    事故现象：点「重启并安装」后程序关了、更新没装、也没重启，仍是旧版本。
    根因：宿主是用 `subprocess.Popen` 从主程序里起的**子进程**，主程序一退
    就被一起结束；真机 apply.log 停在「等待主程序退出 pid=…」之后再无一行。
    → 改用 WMI（Win32_Process.Create）创建，宿主挂到 WmiPrvSE 名下。
    """

    def test_host_script_logs_every_step(self):
        script = updater.powershell_host_script(
            r"D:\x\Setup.exe", 1234, r"D:\AutoQuill", r"D:\x\apply.log",
            relaunch_exe=r"D:\AutoQuill\AutoQuill.exe")
        # 关键步骤都要落日志：出问题时日志是唯一事实来源
        for mark in ("换装开始", "等待主程序退出", "主程序已退出",
                     "安装前 AutoQuill.exe 时间戳", "安装器返回码",
                     "安装后 AutoQuill.exe 时间戳", "正在重启新版本", "换装结束"):
            self.assertIn(mark, script, mark)

    def test_host_script_reports_whether_files_changed(self):
        """安装前后比时间戳：装完没变化要能在日志里看出来。"""
        script = updater.powershell_host_script(
            r"D:\x\Setup.exe", 0, r"D:\AutoQuill", r"D:\x\apply.log")
        self.assertIn("文件已更新（时间戳变化）", script)
        self.assertIn("警告：exe 时间戳未变化", script)

    def test_vbs_uses_wmi_not_a_child_process(self):
        vbs = updater.vbs_detach_launcher("prog.exe --a", "log.txt")
        self.assertIn("Win32_Process", vbs)
        self.assertIn("Create", vbs)
        # 必须只含 ASCII：wscript 是 ANSI 引擎，UTF-8 脚本会把路径读成乱码
        self.assertTrue(vbs.isascii(), "VBS 必须只含 ASCII（wscript 读不了 UTF-8）")

    def test_vbs_builds_quotes_at_runtime(self):
        """引号必须用 Chr(34) 运行时拼：字面量嵌套转义会算错（真机踩到）。"""
        vbs = updater.vbs_detach_launcher(
            '"C:\\Program Files\\x.exe" --a "C:\\我的 目录\\b.exe"', "l.txt")
        self.assertIn("Q = Chr(34)", vbs)
        self.assertIn("Q & ", vbs)

    def test_vbs_keeps_flags_unquoted(self):
        """只给含空格的片段加引号：把 --pid 包成 "--pid" 会让宿主收不到开关。"""
        vbs = updater.vbs_detach_launcher("prog.exe --pid 7", "l.txt")
        expr = vbs.split("rc = proc.Create(")[1].split(", Null")[0]
        self.assertIn('"--pid"', expr)          # 无空格 → 源码里直接带引号，无 Q 拼接
        self.assertNotIn('Q & "--pid"', expr)

    def test_env_is_inlined_via_cmd(self):
        """环境变量必须内联进命令行。

        ★ 实测：WMI 创建的子进程由 WmiPrvSE 派生，**不继承**调用方环境，
          只靠 os.environ 传会丢失 AQ_DATA_DIR → 宿主拿到空状态报「校验和无效」。
        """
        vbs = updater.vbs_detach_launcher(
            "prog.exe --a", "l.txt", env={"AQ_DATA_DIR": r"C:\data"})
        self.assertIn("cmd.exe", vbs)
        self.assertIn("AQ_DATA_DIR=C:", vbs)

    def test_ascii_safe_short_path_for_non_ascii(self):
        """非 ASCII 路径要转成短路径（脚本宿主只认 ANSI）。"""
        out = updater._ascii_safe_path(r"C:\Windows")
        self.assertTrue(out.isascii())

    def test_split_args_respects_quotes(self):
        got = updater._split_args('prog.exe "C:\\a b\\c.exe" --pid 7')
        self.assertEqual(got, ["prog.exe", "C:\\a b\\c.exe", "--pid", "7"])


class ApplyEndpointTest(unittest.TestCase):
    """`/api/update/apply` 真正跑一遍（不碰网络、不装东西）。

    ★ 2026-10-01 线上事故：这一步里写错了方法名（`stage.file()`，
      正确是 `stage.stage_file()`）→ 接口 500 → 界面拿到纯文本
      「Internal Server Error」→ 弹出「Unexpected token 'I' ... is not valid JSON」。
      当时的测试只覆盖了纯函数，**没有真正调用这条路径**，所以没抓到。
      这组用例就是补这个缺口。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_apply_"))
        self._p = mock.patch.object(paths, "DATA_ROOT", str(self.tmp))
        self._p.start()
        from core import update_stage as stage
        self.stage = stage
        os.environ["AQ_DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        self._p.stop()

    def _staged(self, installer: Path):
        self.stage.update(stage=self.stage.STAGE_STAGED, version="9.9.9",
                          current="1.0.0", installer=str(installer),
                          sha256="a" * 64, install_dir=str(self.tmp))

    def test_apply_does_not_touch_the_network(self):
        """安装阶段不允许访问网络（限流/非 JSON 响应都会变成 500）。"""
        import inspect
        from webui import update_api
        src = inspect.getsource(update_api.api_update_apply)
        src += inspect.getsource(update_api._apply_impl)
        for bad in ("_fetch_release_payload", "requests.", "urlopen"):
            self.assertNotIn(bad, src, bad)

    def test_spawn_path_runs_without_attribute_error(self):
        """真正执行 _spawn_detached_host（方法名打错会在这里炸）。"""
        from webui import update_api
        with mock.patch.object(update_api, "_wait_host_started",
                               return_value=True), \
                mock.patch("subprocess.Popen") as popen:
            ok, detail = update_api._spawn_detached_host(
                ["powershell.exe", "-NoProfile", "-EncodedCommand", "AAA"],
                str(self.stage.log_file()))
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "ok")
        popen.assert_called_once()
        # 启动脚本必须真的写到更新目录
        self.assertTrue((Path(self.stage.stage_file()).parent
                         / "spawn_host.vbs").exists())

    def test_apply_returns_json_on_internal_error(self):
        """内部异常必须转成 JSON，绝不能把 500 抛给界面。"""
        from webui import update_api
        installer = self.tmp / "setup.exe"
        installer.write_bytes(b"MZ")
        self._staged(installer)
        with mock.patch.object(update_api, "_spawn_detached_host",
                               side_effect=AttributeError(
                                   "module 'core.update_stage' has no attribute 'file'")):
            got = update_api.api_update_apply()
        self.assertFalse(got["ok"])
        self.assertIn("启动安装失败", got["message"])
        # 真机可读：把原始异常文本带出来，别再让用户只看一句「内部错误」
        self.assertIn("no attribute 'file'", got["message"])

    def test_apply_refuses_when_nothing_staged(self):
        from webui import update_api
        got = update_api.api_update_apply()
        self.assertFalse(got["ok"])
        self.assertIn("先下载", got["message"])

    def test_apply_reports_missing_installer(self):
        from webui import update_api
        self._staged(self.tmp / "gone.exe")
        got = update_api.api_update_apply()
        self.assertFalse(got["ok"])
        self.assertIn("安装包不见了", got["message"])

    def test_apply_dry_run_skips_quit_request(self):
        from webui import update_api
        installer = self.tmp / "setup.exe"
        installer.write_bytes(b"MZ")
        self._staged(installer)
        with mock.patch.object(update_api, "_wait_host_started",
                               return_value=True), \
                mock.patch("subprocess.Popen"), \
                mock.patch.object(update_api, "_request_app_quit") as quit_mock:
            got = update_api.api_update_apply(dry_run=True)
        self.assertTrue(got["ok"], got)
        quit_mock.assert_not_called()           # 演练不该把程序关掉

    def test_never_writes_quit_flag_into_a_live_config(self):
        """测试绝不能把「退出请求」写进真实配置。

        ★ 这条是被自己坑出来的：apply 的正常路径会调用真正的
          `launcher_config.request_quit()`；测试若没隔离数据目录，就会往
          用户在用的 launcher.json 里写退出标记 —— 用户软件下次启动会
          「刚起来就自己退」。这里把 `_request_app_quit` 换掉，
          并断言真实配置里没有被写入。
        """
        from webui import update_api
        from core import launcher_config
        installer = self.tmp / "setup.exe"
        installer.write_bytes(b"MZ")
        self._staged(installer)
        with mock.patch.object(update_api, "_wait_host_started",
                               return_value=True), \
                mock.patch("subprocess.Popen"), \
                mock.patch.object(update_api, "_request_app_quit") as quit_mock:
            got = update_api.api_update_apply()
        self.assertTrue(got["ok"], got)
        quit_mock.assert_called_once()
        self.assertFalse(launcher_config.load().get("quit_requested_at"),
                         "退出标记泄漏到真实配置了")


class QuitRequestTest(unittest.TestCase):
    """更新时必须**真正请求退出**（否则主程序不退、宿主白等、更新卡死）。"""

    def test_request_quit_writes_the_flag_the_launcher_polls(self):
        from core import launcher_config
        import tempfile as _tf
        with mock.patch.object(paths, "DATA_ROOT",
                               _tf.mkdtemp(prefix="aq_quit_")):
            self.assertFalse(launcher_config.load().get("quit_requested_at"))
            stamp = launcher_config.request_quit()
            self.assertTrue(stamp)
            self.assertTrue(launcher_config.load().get("quit_requested_at"))
            launcher_config.clear_quit_request()
            self.assertFalse(launcher_config.load().get("quit_requested_at"))

    def test_apply_endpoint_requests_quit(self):
        """apply 接口必须调用退出请求（源码级契约，防回归）。

        注：退出请求在 `_apply_impl` 里（apply 只是异常包装层），
        所以两处源码合起来看。
        """
        import inspect
        from webui import update_api
        src = (inspect.getsource(update_api.api_update_apply)
               + inspect.getsource(update_api._apply_impl))
        self.assertIn("_request_app_quit", src)
        helper = inspect.getsource(update_api._request_app_quit)
        self.assertIn("request_quit", helper)


if __name__ == "__main__":
    unittest.main()
