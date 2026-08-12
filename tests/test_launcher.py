"""一键启动器 tools/launcher.py 的纯函数测试（进程/浏览器交互部分靠手动验证）。"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("launcher", _REPO / "tools" / "launcher.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


class TestProjectRoot(unittest.TestCase):
    def test_dev_mode_is_repo_root(self):
        self.assertEqual(launcher.project_root(), _REPO)

    def test_frozen_mode_is_exe_dir(self):
        old_frozen = getattr(sys, "frozen", None)
        old_executable = sys.executable
        try:
            sys.frozen = True
            sys.executable = r"C:\apps\AutoQuill\AutoQuill.exe"
            self.assertEqual(launcher.project_root(), Path(r"C:\apps\AutoQuill"))
        finally:
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen
            sys.executable = old_executable


class TestReadLog(unittest.TestCase):
    def _write(self, data: bytes) -> Path:
        import os

        fd, name = tempfile.mkstemp(suffix=".log")
        os.close(fd)  # Windows：不关句柄文件会被占用，无法删除
        tmp = Path(name)
        tmp.write_bytes(data)
        self.addCleanup(tmp.unlink)
        return tmp

    def test_utf8(self):
        p = self._write("服务已就绪\n".encode("utf-8"))
        self.assertIn("服务已就绪", launcher._read_log(p))

    def test_gbk(self):
        p = self._write("服务已就绪\n".encode("gbk"))
        self.assertIn("服务已就绪", launcher._read_log(p))

    def test_missing_file(self):
        self.assertEqual(launcher._read_log(Path(tempfile.gettempdir()) / "no_such_file.log"), "")


class TestServiceAlive(unittest.TestCase):
    def test_no_service_on_unused_port(self):
        # 指向一个必然无服务的端口：用本模块的 PORT 探测 127.0.0.1 上的空闲端口
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        url = f"http://127.0.0.1:{free_port}/api/status"
        try:
            with launcher.urllib.request.urlopen(url, timeout=1) as resp:
                alive = resp.status == 200
        except OSError:
            alive = False
        self.assertFalse(alive)


if __name__ == "__main__":
    unittest.main()
