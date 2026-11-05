"""Focused route tests for the in-site web model preference contract."""
import unittest
from unittest import mock

from fastapi import HTTPException

import config
from webui import api_settings


class WebPreferencesRoutesTest(unittest.TestCase):
    def setUp(self):
        self.providers = {"TestSite": {"url": "https://example.test"}}
        self.settings = {
            "provider": "TestSite",
            "selection": {"model": "story-pro", "options": {"thinking": "high"}},
            "catalog": {"status": "ready", "models": []},
            "validation": {},
        }

    def test_get_returns_catalog_and_selection_for_named_site(self):
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch("config.WEB_DRIVER_NAME", "TestSite"), \
                mock.patch("web_drivers.capabilities.get_provider_settings",
                           return_value=self.settings) as get:
            result = api_settings.api_web_preferences("TestSite")
        self.assertEqual(result, self.settings)
        get.assert_called_once_with("TestSite")

    def test_get_rejects_unknown_site(self):
        with mock.patch("config.WEB_DRIVERS", self.providers):
            with self.assertRaisesRegex(HTTPException, "400"):
                api_settings.api_web_preferences("Missing")

    def test_post_saves_valid_selection(self):
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch.object(api_settings, "_web_preferences_busy", return_value=None), \
                mock.patch("web_drivers.capabilities.save_provider_selection",
                           return_value=self.settings) as save:
            result = api_settings.api_set_web_preferences(
                api_settings._WebPreferencesSpec(name="TestSite",
                                                  model="story-pro",
                                                  options={"thinking": "high"}))
        self.assertEqual(result, self.settings)
        save.assert_called_once_with("TestSite", "story-pro", {"thinking": "high"})

    def test_post_rejects_busy_and_invalid_value(self):
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch.object(api_settings, "_web_preferences_busy",
                                  return_value="任务正在运行"):
            with self.assertRaisesRegex(HTTPException, "409"):
                api_settings.api_set_web_preferences(
                    api_settings._WebPreferencesSpec(name="TestSite", model="x"))
        with mock.patch("config.WEB_DRIVERS", self.providers), \
                mock.patch.object(api_settings, "_web_preferences_busy", return_value=None), \
                mock.patch("web_drivers.capabilities.save_provider_selection",
                           side_effect=ValueError("模型选项无效")):
            with self.assertRaisesRegex(HTTPException, "400"):
                api_settings.api_set_web_preferences(
                    api_settings._WebPreferencesSpec(name="TestSite", model="x"))


if __name__ == "__main__":
    unittest.main()
