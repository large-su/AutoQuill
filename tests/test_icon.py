# ============================================================
# tests/test_icon.py — 应用图标集成（V4.2.1 用户绘制图标）
# ============================================================
# 断言约定（与全库一致）：源码级断言 + 文件存在性，绝不启动真实浏览器。
# ICO 头解析用 struct（零依赖）；像素级检查（透明度）用 PIL，
# 环境无 PIL 时跳过（PIL 非项目依赖，仅本机测试便利）。

import struct
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "assets" / "AutoQuill.ico"
MASTER = ROOT / "assets" / "icon_master.png"
FAVICON = ROOT / "webui" / "static" / "favicon.ico"


def _read_ico_sizes(path):
    """解析 ICO 头（零依赖）：返回 {宽, 高} 尺寸集合（0 表示 256）。"""
    data = path.read_bytes()
    reserved, typ, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and typ == 1, "非 ICO 文件头"
    sizes = set()
    for i in range(count):
        w, h = data[6 + i * 16], data[6 + i * 16 + 1]
        sizes.add((w or 256, h or 256))
    return sizes


class TestIconAssets(unittest.TestCase):
    def test_ico_exists_with_required_sizes(self):
        # Windows 各显示位置需要的尺寸：任务栏 16、桌面 32/48、资源管理器 64+
        self.assertTrue(ICO.exists())
        sizes = _read_ico_sizes(ICO)
        self.assertEqual(len(sizes), 6)
        for size in (16, 32, 48, 64, 128, 256):
            self.assertIn((size, size), sizes)

    def test_favicon_same_as_app_icon(self):
        # favicon 与安装/窗口图标同一文件，避免两处漂移
        self.assertTrue(FAVICON.exists())
        self.assertEqual(FAVICON.read_bytes(), ICO.read_bytes())

    def test_ico_transparent_corners_opaque_center(self):
        # 黑色画布已抠成透明：四角 alpha=0，主体中心不透明
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("本机无 PIL，跳过像素检查")
        img = Image.open(ICO).convert("RGBA")
        self.assertEqual(img.size, (256, 256))
        for xy in ((0, 0), (255, 0), (0, 255), (255, 255)):
            self.assertEqual(img.getpixel(xy)[3], 0, f"角 {xy} 未透明")
        self.assertEqual(img.getpixel((128, 128))[3], 255)

    def test_master_png_square_rgba(self):
        # 可编辑源图：正方形 + 带 alpha，四角透明
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("本机无 PIL，跳过像素检查")
        img = Image.open(MASTER)
        self.assertEqual(img.size, (1024, 1024))
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.getpixel((0, 0))[3], 0)
        self.assertEqual(img.getpixel((512, 512))[3], 255)


class TestIconIntegration(unittest.TestCase):
    """图标接入点源码级断言：exe 资源 / 安装器 / 窗口 / 网页 favicon。"""

    def test_spec_embeds_icon_in_exe(self):
        src = (ROOT / "build" / "AutoQuill.spec").read_text(encoding="utf-8")
        self.assertIn("icon='../assets/AutoQuill.ico'", src)
        # 打包态窗口图标走 datas（onedir 落在 exe 同目录 assets/）
        self.assertIn("('../assets/AutoQuill.ico', 'assets')", src)

    def test_iss_sets_setup_icon(self):
        src = (ROOT / "installer" / "AutoQuill.iss").read_text(encoding="utf-8")
        self.assertIn("SetupIconFile=..\\assets\\AutoQuill.ico", src)
        self.assertTrue((ROOT / "assets" / "AutoQuill.ico").exists())

    def test_iss_shortcuts_point_at_ico_file(self):
        # V4.2.2 修复：快捷方式显式指向 .ico（覆盖安装时从 exe 提取
        # 图标会命中 Shell 旧缓存 → 桌面图标空白，V4.2.1 用户反馈）
        src = (ROOT / "installer" / "AutoQuill.iss").read_text(encoding="utf-8")
        self.assertIn('IconFilename: "{app}\\AutoQuill.ico"', src)
        self.assertIn('Source: "..\\assets\\AutoQuill.ico"; '
                      'DestDir: "{app}"', src)

    def test_launcher_passes_icon_to_window(self):
        import inspect
        from tools.launcher import open_window
        src = inspect.getsource(open_window)
        self.assertIn("_window_icon()", src)
        # 6.x 把 icon 移到 webview.start：按签名探测后传入 start
        self.assertIn("inspect.signature(webview.start).parameters", src)
        self.assertIn('start_kwargs["icon"] = ico', src)

    def test_launcher_icon_not_passed_to_create_window(self):
        # V4.2.1 回归：create_window 传 icon 在 pywebview 6.x 直接
        # TypeError → 整个窗口失败静默回退浏览器（用户反馈「变成
        # 浏览器打开」即此路径）。create_window 调用处不得再带 icon。
        import inspect
        from tools.launcher import open_window
        src = inspect.getsource(open_window)
        create_call = src.split("webview.start")[0]
        self.assertNotIn("icon=", create_call)

    def test_window_icon_resolves_and_guards_missing(self):
        from tools.launcher import _window_icon
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ico = tmp / "assets" / "AutoQuill.ico"
            ico.parent.mkdir()
            ico.write_bytes(b"fake-ico")
            with mock.patch("tools.launcher.project_root",
                            return_value=tmp):
                self.assertEqual(_window_icon(), str(ico))
            ico.unlink()
            with mock.patch("tools.launcher.project_root",
                            return_value=tmp):
                self.assertIsNone(_window_icon())  # 缺文件不阻断启动

    def test_window_icon_frozen_uses_meipass_first(self):
        # 打包态：datas 落在 _MEIPASS（onedir 的 _internal/），优先于 exe 目录
        from tools.launcher import _window_icon
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            meipass = tmp / "_internal"
            ico = meipass / "assets" / "AutoQuill.ico"
            ico.parent.mkdir(parents=True)
            ico.write_bytes(b"fake-ico")
            with mock.patch("tools.launcher.project_root",
                            return_value=tmp / "exe_dir"), \
                 mock.patch("sys.frozen", True, create=True), \
                 mock.patch("sys._MEIPASS", str(meipass), create=True):
                self.assertEqual(_window_icon(), str(ico))

    def test_index_html_links_favicon(self):
        src = (ROOT / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('<link rel="icon" href="favicon.ico">', src)


if __name__ == "__main__":
    unittest.main()
