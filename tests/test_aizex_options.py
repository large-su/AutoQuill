"""Offline DOM coverage for the Aizex hosted-chat dynamic model menu."""
import unittest

from applications.zhihu_story.browser_utils import EDGE_PATH
from web_drivers.aizex_options import apply_options, discover_options, _model_id


_PAGE = """
<main>
  <button id='composer-plus-btn' aria-haspopup='menu'>添加文件</button>
  <button id='trigger' aria-haspopup='menu' class='group/pill'>当前模型</button>
  <div id='root' role='menu' style='display:none'>
    <div role='menuitemradio' aria-checked='true'><div class='min-w-0'><span>GPT-6</span></div></div>
    <div role='menuitem' data-nextc-group='Gemini 系列 [原生]'><div class='min-w-0'><span>Gemini 系列 [原生]</span></div></div>
    <div role='menuitem' data-nextc-group='香蕉模型 [Nano Banana]'><div class='min-w-0'><span>香蕉模型 [Nano Banana]</span></div></div>
    <div id='effortTrigger' role='menuitem' data-nextc-effort-trigger='gpt-6-thinking' aria-label='GPT-6 · 高'><span>思考强度</span></div>
  </div>
  <div id='geminiMenu' role='menu' style='display:none'>
    <div role='menuitemradio' aria-checked='false'><div class='min-w-0'><span>Gemini3.1Pro</span></div></div>
    <div role='menuitemradio' aria-checked='false'><div class='min-w-0'><span>Gemini3.8Flash</span></div></div>
  </div>
  <div id='effortMenu' role='menu' style='display:none'>
    <div role='menuitemradio' aria-checked='false'><div class='min-w-0'><span>低</span></div></div>
    <div role='menuitemradio' aria-checked='true'><div class='min-w-0'><span>高</span></div></div>
  </div>
  <div id='bananaMenu' role='menu' style='display:none'>
    <div role='menuitemradio' aria-checked='false'><div class='min-w-0'><span>Nano Banana Pro</span></div></div>
  </div>
</main>
<script>
  const root = document.querySelector('#root'), gemini = document.querySelector('#geminiMenu'), effort = document.querySelector('#effortMenu');
  let model = 'GPT-6';
  const banana = document.querySelector('#bananaMenu');
  const allMenus = () => [root, gemini, effort, banana];
  const close = () => allMenus().forEach(x => x.style.display = 'none');
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  document.querySelector('#trigger').onclick = () => { close(); root.style.display = 'block'; };
  document.querySelector('[data-nextc-group]').onclick = () => { gemini.style.display = 'block'; };
  document.querySelector("[data-nextc-group='香蕉模型 [Nano Banana]']").onclick = () => { banana.style.display = 'block'; };
  document.querySelector('#effortTrigger').onclick = () => { if (model === 'GPT-6') effort.style.display = 'block'; };
  function selected(menu, label) { menu.querySelectorAll('[role=menuitemradio]').forEach(x => x.setAttribute('aria-checked', String(x.innerText.trim() === label))); }
  root.querySelector('[role=menuitemradio]').onclick = e => { model = e.currentTarget.innerText.trim(); selected(root, model); document.querySelector('#effortTrigger').style.display = ''; };
  gemini.querySelectorAll('[role=menuitemradio]').forEach(x => x.onclick = e => { model = e.currentTarget.innerText.trim(); selected(gemini, model); selected(root, ''); document.querySelector('#effortTrigger').style.display = 'none'; });
  effort.querySelectorAll('[role=menuitemradio]').forEach(x => x.onclick = e => selected(effort, e.currentTarget.innerText.trim()));
</script>
"""


@unittest.skipUnless(EDGE_PATH, "Edge is required for offline DOM verification")
class AizexOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=EDGE_PATH, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(_PAGE)

    def tearDown(self):
        self.page.close()

    def test_discovery_collects_nested_group_and_model_bound_effort_then_restores(self):
        result = discover_options(self.page)
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(result["current"], {"model": _model_id(None,"GPT-6"), "options": {"thinking": "高"}})
        self.assertEqual(by_id[_model_id(None,"GPT-6")]["defaults"], {"thinking": "高"})
        self.assertEqual(by_id[_model_id("Gemini 系列 [原生]","Gemini3.1Pro")]["group"], "Gemini 系列 [原生]")
        self.assertEqual(by_id[_model_id("Gemini 系列 [原生]","Gemini3.1Pro")]["options"], [])
        self.assertFalse(by_id[_model_id("香蕉模型 [Nano Banana]","Nano Banana Pro")]["selectable"])
        self.assertIn("图像模型", by_id[_model_id("香蕉模型 [Nano Banana]","Nano Banana Pro")]["unsupported_reason"])
        self.assertEqual(self.page.locator("#root [role=menuitemradio]").get_attribute("aria-checked"), "true")
        self.assertEqual(self.page.locator("#effortMenu [aria-checked='true']").inner_text(), "高")

    def test_apply_uses_group_path_and_only_accepts_an_exposed_effort(self):
        actual = apply_options(self.page, {"model": _model_id("Gemini 系列 [原生]","Gemini3.8Flash"), "options": {}})
        self.assertEqual(actual, {"model": _model_id("Gemini 系列 [原生]","Gemini3.8Flash"), "options": {}})
        actual = apply_options(self.page, {"model": _model_id(None,"GPT-6"), "options": {"thinking": "低"}})
        self.assertEqual(actual, {"model": _model_id(None,"GPT-6"), "options": {"thinking": "低"}})
        self.assertEqual(self.page.locator("#effortMenu [aria-checked='true']").inner_text(), "低")

    def test_unknown_model_fails_without_selecting_a_lookalike(self):
        with self.assertRaisesRegex(ValueError, "不可用"):
            apply_options(self.page, {"model": "Gemini3.1", "options": {}})
        with self.assertRaisesRegex(ValueError, "文字故事"):
            apply_options(self.page, {"model": _model_id("香蕉模型 [Nano Banana]","Nano Banana Pro"), "options": {}})
        self.assertEqual(discover_options(self.page)["current"]["model"], _model_id(None,"GPT-6"))


if __name__ == "__main__":
    unittest.main()
