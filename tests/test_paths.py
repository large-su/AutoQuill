# ============================================================
# tests/test_paths.py — 数据目录重定向（正式版打包核心）
#
# 源码态（默认）：PROGRAM_ROOT == DATA_ROOT == 项目根，行为与
# V3.x 完全一致；AQ_DATA_DIR 环境变量模拟安装态（冻结态语义）。
#
# 运行：python -m unittest discover -s tests
# ============================================================

import importlib
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reload_paths():
    import core.paths
    return importlib.reload(core.paths)


class TestPathsSourceMode(unittest.TestCase):
    """源码态：数据与程序同根，路径与 V3.x 一致。"""

    def test_roots_equal_project_root(self):
        p = _reload_paths()
        self.assertEqual(p.PROGRAM_ROOT, PROJECT_ROOT)
        self.assertEqual(p.DATA_ROOT, PROJECT_ROOT)

    def test_data_program_join(self):
        p = _reload_paths()
        self.assertEqual(
            p.data("config", "llm_providers.json"),
            os.path.join(PROJECT_ROOT, "config", "llm_providers.json"))
        self.assertEqual(
            p.program("images", "logo.png"),
            os.path.join(PROJECT_ROOT, "images", "logo.png"))

    def test_config_providers_file_unchanged(self):
        # 关键回归：config 读取的服务商注册表路径在源码态必须不变
        from config import _PROVIDERS_FILE
        self.assertEqual(
            os.path.normpath(_PROVIDERS_FILE),
            os.path.normpath(os.path.join(
                PROJECT_ROOT, "config", "llm_providers.json")))

    def test_ensure_provider_file_source_mode_noop(self):
        # 源码态缺失 llm_providers.json 保持原「缺失即报错」，
        # 不自动复制 example（开发人员需要响亮提示）
        p = _reload_paths()
        self.assertIsNone(p.ensure_provider_file())

    def test_migrate_noop_in_source_mode(self):
        p = _reload_paths()
        result = p.migrate_legacy_data()
        self.assertFalse(result["migrated"])
        self.assertIsNone(result["error"])


class TestPathsInstallMode(unittest.TestCase):
    """AQ_DATA_DIR 模拟安装态（冻结语义）：数据与程序分离。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aq_paths_")
        os.environ["AQ_DATA_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("AQ_DATA_DIR", None)
        _reload_paths()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_override(self):
        p = _reload_paths()
        self.assertEqual(p.DATA_ROOT, os.path.abspath(self.tmp))
        self.assertNotEqual(p.PROGRAM_ROOT, p.DATA_ROOT)

    def test_ensure_provider_file_copies_example(self):
        p = _reload_paths()
        dst = p.ensure_provider_file()
        self.assertEqual(dst, os.path.join(self.tmp, "config",
                                           "llm_providers.json"))
        self.assertTrue(os.path.isfile(dst))
        with open(dst, encoding="utf-8") as f:
            self.assertIn("DeepSeek", f.read())
        # 幂等：已存在不再复制
        self.assertEqual(p.ensure_provider_file(), dst)

    def test_migrate_copies_dirs_and_files_once(self):
        # 模拟旧版解压目录（PROGRAM_ROOT 旁挂 data/output/config 数据）
        fake_old = tempfile.mkdtemp(prefix="aq_old_")
        try:
            for d in ("data", "output", "config"):
                os.makedirs(os.path.join(fake_old, d))
            for rel, body in (
                    ("data/collected_stories.jsonl", "{}"),
                    ("output/story_x.md", "# x"),
                    ("config/llm_providers.json", "[]"),
                    ("config/browser_state.json", "{}"),
                    ("config/webui_model.json", "{}")):
                with open(os.path.join(fake_old, rel), "w",
                          encoding="utf-8") as f:
                    f.write(body)

            p = _reload_paths()
            p.PROGRAM_ROOT = fake_old
            result = p.migrate_legacy_data()
            self.assertIsNone(result["error"])
            self.assertTrue(result["migrated"])
            for rel in ("data/collected_stories.jsonl",
                        "output/story_x.md",
                        "config/llm_providers.json",
                        "config/browser_state.json",
                        "config/webui_model.json"):
                self.assertTrue(
                    os.path.isfile(os.path.join(self.tmp, rel)),
                    f"未迁移：{rel}")
            # 二次迁移：目标已存在 → 不再复制
            result2 = p.migrate_legacy_data()
            self.assertFalse(result2["migrated"])
        finally:
            shutil.rmtree(fake_old, ignore_errors=True)

    def test_migrate_does_not_clobber_existing(self):
        # 目标已有数据（二次启动/已有安装）→ 不覆盖、不迁移
        fake_old = tempfile.mkdtemp(prefix="aq_old_")
        try:
            os.makedirs(os.path.join(fake_old, "data"))
            with open(os.path.join(fake_old, "data", "old.jsonl"),
                      "w") as f:
                f.write("{}")
            marker = os.path.join(self.tmp, "data")
            os.makedirs(marker)
            with open(os.path.join(marker, "keep.txt"), "w") as f:
                f.write("keep")

            p = _reload_paths()
            p.PROGRAM_ROOT = fake_old
            result = p.migrate_legacy_data()
            self.assertFalse(result["migrated"])
            self.assertTrue(
                os.path.isfile(os.path.join(marker, "keep.txt")))
            self.assertFalse(
                os.path.exists(os.path.join(marker, "old.jsonl")))
        finally:
            shutil.rmtree(fake_old, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
