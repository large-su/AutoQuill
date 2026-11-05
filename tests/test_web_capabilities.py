"""Regression tests for discovered web model catalogs and selections."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import paths
import config
from web_drivers import capabilities


class WebCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = paths.DATA_ROOT
        paths.DATA_ROOT = self.tmp.name
        self.old_drivers = config.WEB_DRIVERS
        config.WEB_DRIVERS = {
            "TestSite": {"url": "https://example.test"},
            "OtherSite": {"url": "https://other.test"},
        }

    def tearDown(self):
        config.WEB_DRIVERS = self.old_drivers
        paths.DATA_ROOT = self.old_root
        self.tmp.cleanup()

    @staticmethod
    def catalog(*, models=None, status="ready"):
        return {
            "status": status,
            "checked_at": 0,
            "models": models or [{
                "id": "story-pro", "label": "Story Pro",
                "options": [{"key": "thinking", "label": "思考强度",
                             "choices": [{"id": "low", "label": "低"},
                                         {"id": "high", "label": "高"}]}],
            }],
            "current": {"model": "story-pro", "options": {}},
            "changes": {"added": [], "removed": []},
            "message": "ok",
        }

    def test_selection_requires_ready_catalog_and_bound_option(self):
        with self.assertRaisesRegex(ValueError, "先检查状态"):
            capabilities.save_provider_selection("TestSite", "story-pro", {})
        capabilities.remember_catalog("TestSite", self.catalog())
        saved = capabilities.save_provider_selection("TestSite", "story-pro",
                                                     {"thinking": "high"})
        self.assertEqual(saved["selection"], {"model": "story-pro",
                                              "options": {"thinking": "high"}})
        with self.assertRaisesRegex(ValueError, "不支持选项"):
            capabilities.save_provider_selection("TestSite", "story-pro",
                                                 {"thinking": "medium"})
        with self.assertRaisesRegex(ValueError, "不可用"):
            capabilities.save_provider_selection("TestSite", "missing", {})

    def test_catalog_refresh_reports_added_removed_and_keeps_selection(self):
        capabilities.remember_catalog("TestSite", self.catalog())
        capabilities.save_provider_selection("TestSite", "story-pro",
                                             {"thinking": "high"})
        updated = self.catalog(models=[{
            "id": "story-max", "label": "Story Max", "options": []
        }])
        with mock.patch("web_drivers.site_options.discover_options",
                        return_value=updated):
            refreshed = capabilities.refresh_capabilities("TestSite", object())
        self.assertEqual(refreshed["status"], "ready")
        self.assertEqual(refreshed["changes"], {
            "added": ["story-max"], "removed": ["story-pro"]})
        settings = capabilities.get_provider_settings("TestSite")
        self.assertEqual(settings["selection"]["model"], "story-pro")
        self.assertEqual(settings["catalog"]["validation"], {})

    def test_failed_refresh_is_error_and_does_not_accept_new_selection(self):
        capabilities.remember_catalog("TestSite", self.catalog())
        with mock.patch("web_drivers.site_options.discover_options",
                        side_effect=RuntimeError("DOM changed")):
            failed = capabilities.refresh_capabilities("TestSite", object())
        self.assertEqual(failed["status"], "error")
        self.assertNotEqual(failed["status"], "ready")
        with self.assertRaisesRegex(ValueError, "先检查状态"):
            capabilities.save_provider_selection("TestSite", "story-pro", {})

    def test_selections_are_independent_per_site(self):
        capabilities.remember_catalog("TestSite", self.catalog())
        capabilities.remember_catalog("OtherSite", self.catalog())
        capabilities.save_provider_selection("TestSite", "story-pro",
                                             {"thinking": "low"})
        self.assertEqual(capabilities.get_provider_settings("OtherSite")["selection"],
                         {"model": "", "options": {}})

    def test_validation_evidence_is_separate_from_discovery(self):
        capabilities.remember_catalog("TestSite", self.catalog())
        capabilities.record_validation("TestSite", "classic", {
            "model": "story-pro", "options": {"thinking": "high"}},
            "passed", {"run": "redacted-evidence"})
        settings = capabilities.get_provider_settings("TestSite")
        self.assertEqual(settings["catalog"]["status"], "ready")
        self.assertEqual(len(settings["validation"]), 1)
        with mock.patch("web_drivers.site_options.discover_options",
                        return_value=self.catalog()):
            capabilities.refresh_capabilities("TestSite", object())
        self.assertEqual(len(capabilities.get_provider_settings("TestSite")
                             ["validation"]), 1)


if __name__ == "__main__":
    unittest.main()
