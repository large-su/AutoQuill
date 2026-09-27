# -*- coding: utf-8 -*-
# ============================================================
# tests/test_static_checks.py — 发布前的静态扫描 + 处理器冒烟
#
# 起因（2026-09-27 真机事故）：automation/executor.py 的 _reply_comment 把
# `profile_in_use` 漏在了 import 之外，处理器一进来就 NameError。单元测试
# 全绿（没人真跑过那个处理器）、装到真机连续失败 3 次还被熔断自动停用，
# 用户看到的是「回复评论全都失败了」。
#
# 因此加两条防线：
#   1) pyflakes 静态扫描：未定义名字这类「一跑到就炸」的问题（未装则跳过）；
#   2) 处理器冒烟：把每个作业处理器的前置检查段真跑一遍，任何
#      NameError / AttributeError / ImportError 都判失败（其它异常可接受）。
# ============================================================

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCAN_TARGETS = ("applications", "automation", "core", "webui", "workflows",
                "tools", "main.py")


def _has_pyflakes():
    try:
        import pyflakes  # noqa: F401
        return True
    except Exception:                # noqa: BLE001
        return False


class TestUndefinedNames(unittest.TestCase):
    """静态扫描：全仓库不许有「未定义名字」。"""

    @unittest.skipUnless(_has_pyflakes(), "未安装 pyflakes（pip install pyflakes）")
    def test_no_undefined_names(self):
        r = subprocess.run([sys.executable, "-m", "pyflakes", *SCAN_TARGETS],
                           cwd=str(ROOT), capture_output=True, text=True)
        # 只认真正的报告行（形如 `文件:行:列: undefined name 'x'`）；
        # 排除 `from x import *` 的「unable to detect undefined names」提示
        bad = [ln for ln in (r.stdout or "").splitlines()
               if ": undefined name '" in ln]
        self.assertEqual(bad, [],
                         "存在未定义名字（运行到就 NameError）：" + chr(10)
                         + chr(10).join(bad))


class _StopHere(Exception):
    """哨兵：处理器走到「要开浏览器」这一步 = 前置检查段已通过。"""


class TestHandlersSmoke(unittest.TestCase):
    """每个作业处理器的前置检查段必须能跑通——不许出现未定义名字。"""

    def test_handler_early_path_has_no_name_errors(self):
        from automation import executor
        fake_browser = mock.MagicMock()
        fake_browser.return_value.start.side_effect = _StopHere
        patches = [
            mock.patch.object(executor, "_browser_busy", return_value=[]),
            mock.patch("web_drivers.browser_pool.profile_in_use",
                       return_value=False),
            mock.patch("web_drivers.browser_pool.get_browser",
                       side_effect=_StopHere),
            mock.patch("web_drivers.browser_pool.close_shared_browser",
                       return_value=None),
            mock.patch("webui.run_manager.runner.start", side_effect=_StopHere),
            mock.patch("applications.zhihu_story.browser_adapter.ZhihuBrowser",
                       fake_browser),
        ]
        for p in patches:
            p.start()
        try:
            for name, handler in executor._HANDLERS.items():
                job = {"type": name, "key": "test:" + name, "params": {}}
                try:
                    handler(job)
                except _StopHere:
                    continue          # 前置检查通过，走到浏览器这一步 = 合格
                except (NameError, AttributeError, ImportError) as exc:
                    self.fail("%s 处理器前置检查段报 %s：%s"
                              % (name, type(exc).__name__, exc))
                except Exception:     # noqa: BLE001
                    continue          # 其它异常（环境未就绪等）不算问题
        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main()
