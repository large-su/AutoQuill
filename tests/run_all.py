# -*- coding: utf-8 -*-
"""统一测试入口（本地与 CI 共用）。

跳过需要真实浏览器/登录态的用例（web_drivers_dom / zhihu_workflow /
browser_adapter / web_parallel / collector），它们依赖 Windows Edge 与
知乎会话，不适合通用 CI 或无头环境。
"""
import os
import sys
import unittest

SKIP_MODULES = {
    "test_web_drivers_dom",
    "test_zhihu_workflow",
    "test_browser_adapter",
    "test_web_parallel",
    "test_collector",
}

HERE = os.path.dirname(os.path.abspath(__file__))
# 保证 tools/ applications/ webui/ 等顶层包可导入（本地/CI 均可）
sys.path.insert(0, os.path.dirname(HERE))


def _collect(node, out):
    if isinstance(node, unittest.TestSuite):
        for t in node:
            _collect(t, out)
    elif isinstance(node, unittest.TestCase):
        mod = type(node).__module__.split(".")[-1]
        if mod not in SKIP_MODULES:
            out.append(node)


def main():
    suite = unittest.defaultTestLoader.discover(
        start_dir=HERE, pattern="test_*.py")
    flat = []
    _collect(suite, flat)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(flat))
    summary = "共执行 %d 个用例，失败 %d，错误 %d" % (
        result.testsRun, len(result.failures), len(result.errors))
    sys.stderr.write(os.linesep + summary + os.linesep)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
