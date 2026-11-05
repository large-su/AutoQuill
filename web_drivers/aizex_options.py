"""Aizex hosted-chat model menu adapter.

The Aizex launcher is handled elsewhere.  This module starts after its
"one-click use" route has reached the hosted ChatGPT-style chat page.
It uses only visible menu controls and does not call undocumented endpoints.
"""
from __future__ import annotations

import json
import re


ROOT_TRIGGER = "main button[aria-haspopup='menu'][class~='group/pill']"
GROUP_ITEM = "[role='menuitem'][data-nextc-group]"
EFFORT_TRIGGER = "[role='menuitem'][data-nextc-effort-trigger]"
RADIO_ITEM = "[role='menuitemradio']"


def _exact(value):
    return re.compile(r"^\s*" + re.escape(value) + r"\s*$")


def _visible(locator):
    return locator.filter(visible=True)


def _label(locator):
    """Read the first human-visible label from the menu item, not its detail."""
    return (locator.evaluate("""el => {
      const candidate = el.querySelector('div.min-w-0 > span, .min-w-0 > span')
        || el.querySelector('.truncate') || el.querySelector('span');
      return (candidate || el).innerText.trim().split('\\n')[0].trim();
    }""") or "").strip()


def _model_id(group, label):
    """Stable catalog ID; labels can repeat across independent Aizex groups."""
    return json.dumps([group or "", label], ensure_ascii=False, separators=(",", ":"))


class AizexOptions:
    """Small menu adapter for dynamically exposed Aizex hosted models."""

    def __init__(self, page):
        self.page = page

    def close(self):
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(80)

    def _menus(self):
        return _visible(self.page.locator("[role='menu']"))

    def _active_menu(self):
        menus = self._menus()
        if not menus.count():
            raise RuntimeError("Aizex 未显示模型菜单")
        return menus.nth(menus.count() - 1)

    @staticmethod
    def _is_model_menu(menu):
        return menu.locator(f"{RADIO_ITEM}, {GROUP_ITEM}").count() > 0

    def open(self):
        """Open the model menu, skipping unrelated composer menus."""
        from web_drivers.browser_pool import _check_cancel
        _check_cancel()
        self.close()
        trigger = self.page.locator(ROOT_TRIGGER)
        trigger.first.wait_for(state="visible", timeout=5000)
        trigger = _visible(trigger)
        if not trigger.count():
            raise RuntimeError("Aizex 未找到当前模型菜单入口")
        for index in range(trigger.count()):
            trigger.nth(index).click(timeout=5000)
            self.page.wait_for_timeout(120)
            menus = self._menus()
            for menu_index in range(menus.count() - 1, -1, -1):
                menu = menus.nth(menu_index)
                if self._is_model_menu(menu):
                    return menu
            self.close()
        raise RuntimeError("Aizex 未找到包含模型项的菜单")

    @staticmethod
    def _items(menu, selector):
        return _visible(menu.locator(selector))

    def _click_label(self, menu, selector, label):
        candidates = self._items(menu, selector)
        for index in range(candidates.count()):
            item = candidates.nth(index)
            if _label(item) == label:
                item.evaluate('(element)=>element.click()')
                self.page.wait_for_timeout(120)
                return item
        raise ValueError(f"Aizex 菜单中没有可选项：{label}")

    def _root_entries(self):
        root = self.open()
        entries = []
        for index in range(self._items(root, RADIO_ITEM).count()):
            item = self._items(root, RADIO_ITEM).nth(index)
            label = _label(item)
            if label:
                entries.append({"id": _model_id(None, label), "label": label,
                                "selected": item.get_attribute("aria-checked") == "true",
                                "group": None})
        for index in range(self._items(root, GROUP_ITEM).count()):
            item = self._items(root, GROUP_ITEM).nth(index)
            group = item.get_attribute("data-nextc-group") or _label(item)
            if not group:
                continue
            item.evaluate('(element)=>element.click()')
            self.page.wait_for_timeout(120)
            submenu = self._active_menu()
            radios = self._items(submenu, RADIO_ITEM)
            for radio_index in range(radios.count()):
                radio = radios.nth(radio_index)
                label = _label(radio)
                if label:
                    entries.append({"id": _model_id(group, label), "label": label,
                                    "selected": radio.get_attribute("aria-checked") == "true",
                                    "group": group})
            self.close()
            root = self.open()
        ids = [entry["id"] for entry in entries]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Aizex 当前菜单存在重名模型，无法安全保存选择")
        return entries

    def _choose(self, label, group=None):
        root = self.open()
        if group:
            self._click_label(root, GROUP_ITEM, group)
            root = self._active_menu()
        self._click_label(root, RADIO_ITEM, label)

    def models(self):
        return self._root_entries()

    def _effort(self, label):
        # The effort menu exists only for models that expose the host control.
        root = self.open()
        triggers = self._items(root, EFFORT_TRIGGER)
        matched = None
        for index in range(triggers.count()):
            candidate = triggers.nth(index)
            candidate_label = (candidate.get_attribute("aria-label") or "").split("·", 1)[0].strip()
            if candidate_label == label:
                matched = candidate
                break
        if matched is None:
            return []
        matched.evaluate('(element)=>element.click()')
        self.page.wait_for_timeout(120)
        menu = self._active_menu()
        choices = []
        radios = self._items(menu, RADIO_ITEM)
        for index in range(radios.count()):
            radio = radios.nth(index)
            label = _label(radio)
            if label:
                choices.append({"id": label, "label": label,
                                "selected": radio.get_attribute("aria-checked") == "true"})
        current = next((item["id"] for item in choices if item["selected"]), "")
        return [{"key": "thinking", "label": "思考强度",
                 "choices": [{"id": item["id"], "label": item["label"]} for item in choices],
                 "current": current}] if choices else []

    def snapshot(self):
        entries = self.models()
        current = next((entry for entry in entries if entry["selected"]), None)
        if not current:
            raise RuntimeError("Aizex 无法确认当前模型")
        options = self._effort(current["label"])
        return {"model": current["id"], "options": {item["key"]: item["current"] for item in options},
                "group": current["group"], "label": current["label"]}

    def set_option(self, model, key, value):
        if key != "thinking":
            raise ValueError(f"Aizex 不支持选项：{key}")
        root = self.open()
        triggers = self._items(root, EFFORT_TRIGGER)
        trigger = None
        for index in range(triggers.count()):
            candidate = triggers.nth(index)
            label = (candidate.get_attribute("aria-label") or "").split("·", 1)[0].strip()
            if label == model:
                trigger = candidate
                break
        if trigger is None:
            raise ValueError("当前 Aizex 模型没有思考强度选项")
        trigger.evaluate('(element)=>element.click()')
        self.page.wait_for_timeout(120)
        self._click_label(self._active_menu(), RADIO_ITEM, value)

    @staticmethod
    def _unsupported(model):
        source = model["label"].lower()
        if "banana" in source or "图片" in source or "image" in source:
            return "图像模型不能用于标准文字故事工作流"
        if "deep research" in source or "deepresearch" in source:
            return "Deep Research 交付方式不适合标准文字故事工作流"
        return ""

    def discover(self):
        original = self.snapshot()
        models = self.models()
        try:
            for model in models:
                reason = self._unsupported(model)
                if reason:
                    model.update(options=[], defaults={}, selectable=False,
                                 unsupported_reason=reason)
                    model.pop("selected", None)
                    continue
                self._choose(model["label"], model["group"])
                options = self._effort(model["label"])
                model.update(options=options,
                             defaults={option["key"]: option["current"] for option in options},
                             selectable=True, unsupported_reason=None)
                model.pop("selected", None)
        finally:
            from web_drivers.browser_pool import session_cleanup
            with session_cleanup():
                try:
                    self._choose(original["label"], original["group"])
                    for key, value in original["options"].items():
                        self.set_option(original["label"], key, value)
                    restored = self.snapshot()
                    if restored["model"] != original["model"] or restored["options"] != original["options"]:
                        raise RuntimeError("Aizex 能力检查未能恢复网页原选择")
                finally:
                    self.close()
        return {"models": models, "current": {"model": original["model"], "options": original["options"]}}

    def apply(self, selection):
        current = self.snapshot()
        models = self.models()
        found = next((model for model in models if model["id"] == selection["model"]), None)
        if not found:
            raise ValueError("Aizex 所选模型已不可用")
        if self._unsupported(found):
            raise ValueError(found.get("unsupported_reason") or "该模型不能用于标准文字故事工作流")
        if current["model"] != found["id"]:
            self._choose(found["label"], found["group"])
        for key, value in selection.get("options", {}).items():
            if current["model"] != found["id"] or current["options"].get(key) != value:
                self.set_option(found["label"], key, value)
        actual = self.snapshot()
        if actual["model"] != selection["model"] or any(actual["options"].get(key) != value
                                                           for key, value in selection.get("options", {}).items()):
            raise RuntimeError("Aizex 未确认所选模型或参数")
        self.close()
        return {"model": actual["model"], "options": actual["options"]}


def discover_options(page):
    return AizexOptions(page).discover()


def apply_options(page, selection):
    return AizexOptions(page).apply(selection)
