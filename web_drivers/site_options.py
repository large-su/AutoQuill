"""Website-specific model menus, probed against their logged-in DOM.

The workflow deals with selections. Only these adapters know where each
website places models, modes, and thinking controls.
"""
import re


def _exact(value):
    return re.compile(r"^\s*" + re.escape(value) + r"\s*$")


def _visible(page, selector):
    return page.locator(selector).filter(visible=True)


def _rows(locator, label=None, id_attr=None):
    return locator.evaluate_all("""(nodes, spec) => nodes.map(e => {
      const label = (spec.label ? e.querySelector(spec.label)?.innerText : e.innerText)?.trim() || '';
      return {id: (spec.id ? e.getAttribute(spec.id) : label) || label, label,
        selected:e.getAttribute('aria-checked') === 'true' || e.classList.contains('selected'),
        disabled:e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('disabled') || e.hasAttribute('data-disabled')};
    }).filter(e=>e.label && !e.disabled)""", {"label": label, "id": id_attr})


def _option(key, label, choices, current):
    return {"key": key, "label": label,
            "choices": [{"id": r["id"], "label": r["label"]} for r in choices],
            "current": current}


class _Menu:
    trigger = ""

    def __init__(self, page):
        self.page = page

    def close(self):
        self.page.keyboard.press("Escape")
        self.page.mouse.move(10, 10)
        composer = _visible(self.page, 'textarea,[contenteditable="true"]')
        if composer.count():
            composer.first.click(timeout=3000)
        self.page.wait_for_timeout(100)

    def open(self):
        from web_drivers.browser_pool import _check_cancel
        _check_cancel()
        self.close()
        _visible(self.page, self.trigger).first.click(timeout=5000)
        self.page.wait_for_timeout(200)

    def models(self):
        raise NotImplementedError

    def options(self):
        return []

    def choose(self, model):
        raise NotImplementedError

    def set_option(self, key, value):
        raise ValueError(f"网页未适配选项：{key}")

    def snapshot(self):
        rows = self.models()
        current = next((r["id"] for r in rows if r.get("selected")), "")
        if not current:
            raise RuntimeError("无法确认网页当前模型")
        options = self.options()
        return {"model": current, "options": {o["key"]: o["current"] for o in options}}


class _ChatGLM(_Menu):
    trigger = ".think-mode-trigger"
    model_rows = ".think-mode-list > .think-mode-item:not(.has-submenu)"

    def open(self):
        from web_drivers.browser_pool import _check_cancel
        _check_cancel()
        if _visible(self.page, ".think-mode-list").count():
            return
        _visible(self.page, self.trigger).first.click(timeout=5000)
        self.page.locator(".think-mode-list").filter(visible=True).first.wait_for(
            state="visible", timeout=5000)
        self.page.wait_for_timeout(400)

    def models(self):
        self.open()
        return _rows(_visible(self.page, self.model_rows), ".item-name")

    def choose(self, model):
        self.open()
        _visible(self.page, self.model_rows).filter(
            has=self.page.locator(".item-name", has_text=_exact(model))).first.click(timeout=5000)
        self.page.wait_for_timeout(450)

    def options(self):
        self.open()
        parent = _visible(self.page, ".think-mode-item.has-submenu")
        if not parent.count():
            return []
        parent.first.hover(timeout=5000)
        self.page.wait_for_timeout(150)
        rows = _rows(_visible(self.page, ".think-submenu .think-mode-item"), ".item-name")
        current = next((r["id"] for r in rows if r["selected"]), "")
        return [_option("thinking", "思考强度", rows, current)] if rows else []

    def set_option(self, key, value):
        if key != "thinking":
            return super().set_option(key, value)
        self.options()
        _visible(self.page, ".think-submenu .think-mode-item").filter(
            has=self.page.locator(".item-name", has_text=_exact(value))).first.click(timeout=5000)
        self.page.wait_for_timeout(450)


class _Kimi(_Menu):
    trigger = '[data-testid="model-select-trigger"]'

    def models(self):
        self.open()
        rows = _rows(_visible(self.page, '[data-testid="model-option"]'), ".name", "data-moon-key")
        more = _visible(self.page, '[data-testid="model-more-trigger"]')
        if more.count():
            more.first.hover(timeout=5000)
            self.page.wait_for_timeout(200)
            rows += _rows(_visible(self.page, '[data-testid="model-more-option"]'),
                          ".kimi-menu-item__label", "data-model-id")
        return rows

    def choose(self, model):
        rows = self.models()
        if model not in {r["id"] for r in rows}:
            raise ValueError("Kimi 模型已不可用")
        loc = self.page.locator('[data-testid="model-option"], [data-testid="model-more-option"]')
        for attr in ("data-moon-key", "data-model-id"):
            target = loc.filter(visible=True)
            for i in range(target.count()):
                item = target.nth(i)
                if item.get_attribute(attr) == model:
                    item.click(timeout=5000)
                    self.page.wait_for_timeout(200)
                    return
        raise RuntimeError("Kimi 模型入口无法确认")

    def options(self):
        specs = []
        for key, label, testid, choice in (
                ("thinking", "思考强度", "model-effort-item", "model-effort-option"),
                ("context_length", "对话长度", "model-context-length-item", "model-context-length-option")):
            self.open()
            entry = _visible(self.page, f'[data-testid="{testid}"]')
            if not entry.count():
                continue
            entry.first.hover(timeout=5000)
            self.page.wait_for_timeout(200)
            rows = _rows(_visible(self.page, f'[data-testid="{choice}"]'), ".effort-name")
            current = next((r["id"] for r in rows if r["selected"]), "")
            if rows:
                specs.append(_option(key, label, rows, current))
        return specs

    def set_option(self, key, value):
        names = {"thinking": ("model-effort-item", "model-effort-option"),
                 "context_length": ("model-context-length-item", "model-context-length-option")}
        if key not in names:
            return super().set_option(key, value)
        entry, choice = names[key]
        self.open()
        _visible(self.page, f'[data-testid="{entry}"]').first.hover(timeout=5000)
        _visible(self.page, f'[data-testid="{choice}"]').filter(has_text=_exact(value)).first.click(timeout=5000)


class _Doubao(_Menu):
    trigger = '[data-testid="chat_input_action_model"]'

    def close(self):
        self.page.keyboard.press("Escape")
        self.page.mouse.move(10, 10)
        self.page.wait_for_timeout(150)

    def open(self):
        from web_drivers.browser_pool import _check_cancel
        _check_cancel()
        self.close()
        _visible(self.page, self.trigger).first.click(timeout=5000)
        self.page.locator('[role="menu"][data-state="open"] [role="menuitem"]').first.wait_for(
            state="visible", timeout=5000)

    def models(self):
        current = _visible(self.page, self.trigger).first.inner_text().strip()
        self.open()
        rows = _rows(_visible(self.page, '[role="menu"][data-state="open"] [role="menuitem"]'))
        for row in rows:
            row["selected"] = row["label"] == current
        return rows

    def choose(self, model):
        self.open()
        _visible(self.page, '[role="menu"][data-state="open"] [role="menuitem"]').filter(has_text=_exact(model)).first.click(timeout=5000)


class _Yuanbao(_Menu):
    trigger = 'button[aria-label="切换模型"]'

    def models(self):
        self.open()
        _visible(self.page, 'button[aria-label="选择模型"]').first.hover(timeout=5000)
        self.page.wait_for_timeout(200)
        return _rows(_visible(self.page, '[role="menu"][aria-label="模型列表"] [role="menuitemradio"]'),
                     "span.text-sm")

    def choose(self, model):
        self.models()
        _visible(self.page, '[role="menu"][aria-label="模型列表"] [role="menuitemradio"]').filter(
            has=self.page.locator("span.text-sm", has_text=_exact(model))).first.click(timeout=5000)
        self.page.wait_for_timeout(200)

    def options(self):
        self.open()
        rows = _rows(_visible(self.page, '[role="menu"]:not([aria-label="模型列表"]) [role="menuitemradio"]'),
                     "span.text-sm span.truncate")
        current = next((r["id"] for r in rows if r["selected"]), "")
        return [_option("mode", "回答模式", rows, current)] if rows else []

    def set_option(self, key, value):
        if key != "mode":
            return super().set_option(key, value)
        self.open()
        _visible(self.page, '[role="menu"]:not([aria-label="模型列表"]) [role="menuitemradio"]').filter(
            has=self.page.locator("span.truncate", has_text=_exact(value))).first.click(timeout=5000)


class _Qianwen(_Menu):
    trigger = 'div[type="button"][aria-haspopup="dialog"]'

    def models(self):
        self.open()
        rows = _visible(self.page, '[role="dialog"][data-state="open"] div.group.cursor-pointer')
        return rows.evaluate_all("""nodes=>nodes.map(e=>{
          const label=e.querySelector('.truncate')?.innerText.trim();
          const check=e.querySelector('[data-icon-type="qwpcicon-check"]');
          return {id:label,label,selected:!!check&&!check.classList.contains('invisible')};
        }).filter(e=>e.id)""")

    def choose(self, model):
        self.open()
        _visible(self.page, '[role="dialog"][data-state="open"] div.group.cursor-pointer').filter(
            has=self.page.locator(".truncate", has_text=_exact(model))).first.click(timeout=5000)
        self.page.wait_for_timeout(200)

    def options(self):
        self.close()
        entry = _visible(self.page, 'button[aria-label="快速"], button[aria-label="思考研究"]')
        if not entry.count():
            return []
        entry.first.click(timeout=5000)
        self.page.wait_for_timeout(150)
        rows = _rows(_visible(self.page, '[role="menu"] [role="menuitemcheckbox"]'), ".text-14")
        current = next((r["id"] for r in rows if r["selected"]), "")
        return [_option("mode", "回答模式", rows, current)] if rows else []

    def set_option(self, key, value):
        if key != "mode":
            return super().set_option(key, value)
        self.options()
        _visible(self.page, '[role="menu"] [role="menuitemcheckbox"]').filter(
            has=self.page.locator(".text-14", has_text=_exact(value))).first.click(timeout=5000)


_MENUS = {"ChatGLM": _ChatGLM, "Kimi": _Kimi, "Doubao": _Doubao,
          "Yuanbao": _Yuanbao, "Qianwen": _Qianwen}


def discover_options(name, page):
    if name == "Aizex":
        from web_drivers.aizex_options import discover_options as discover_aizex
        return discover_aizex(page)
    if name not in _MENUS:
        raise ValueError(f"{name} 站内模型菜单尚未适配")
    menu = _MENUS[name](page)
    original = menu.snapshot()
    models = menu.models()
    try:
        for model in models:
            menu.choose(model["id"])
            specs = menu.options()
            model.update(options=specs, defaults={s["key"]: s["current"] for s in specs})
            model.pop("selected", None)
            model.pop("disabled", None)
    finally:
        from web_drivers.browser_pool import session_cleanup
        with session_cleanup():
            try:
                menu.choose(original["model"])
                for key, value in original["options"].items():
                    menu.set_option(key, value)
                restored = menu.snapshot()
                if restored != original:
                    raise RuntimeError("模型能力检查未能恢复网页原选择")
            finally:
                menu.close()
    return {"models": models, "current": original}


def apply_options(name, page, selection):
    if name == "Aizex":
        from web_drivers.aizex_options import apply_options as apply_aizex
        return apply_aizex(page, selection)
    menu = _MENUS[name](page)
    try:
        current = menu.snapshot()
        if current["model"] != selection["model"]:
            menu.choose(selection["model"])
        for key, value in selection.get("options", {}).items():
            if current.get("options", {}).get(key) != value or current["model"] != selection["model"]:
                menu.set_option(key, value)
        return menu.snapshot()
    finally:
        menu.close()
