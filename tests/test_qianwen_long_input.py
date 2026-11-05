"""Offline Edge DOM regression for Qianwen long prompt upload handling."""
import unittest
from types import SimpleNamespace
from unittest import mock

from config import WEB_DRIVERS
from web_drivers.qianwen import QianwenDriver


class QianwenLongInputDOMTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from applications.zhihu_story.browser_utils import EDGE_PATH
        if not EDGE_PATH:
            raise unittest.SkipTest("Edge not installed")
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(executable_path=EDGE_PATH, headless=True)
        except Exception:
            cls.pw.stop()
            raise unittest.SkipTest("Edge unavailable for offline DOM regression")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_content("""
          <button id="attach" aria-label="添加附件">附件</button>
          <div id="menu"></div>
          <div role="textbox" data-slate-editor="true" contenteditable="true"></div>
          <script>
            window.uploadedText = '';
            document.querySelector('#attach').onclick = () => {
              const menu = document.querySelector('#menu');
              menu.innerHTML = '<div role="menuitem">上传文档</div><input id="file" type="file" hidden>';
              const item = menu.querySelector('[role=menuitem]');
              const input = menu.querySelector('#file');
              item.onclick = () => input.click();
              input.onchange = async () => {
                const f = input.files[0];
                if (window.failUpload) {
                  const failed = document.createElement('div'); failed.className = 'fileBox-test';
                  failed.innerHTML = '<span>' + f.name + '</span><span class="statusLine-test">上传失败</span>';
                  document.body.appendChild(failed); return;
                }
                window.uploadedText = await f.text();
                const card = document.createElement('div'); card.className = 'fileBox-test';
                card.innerHTML = '<span>' + f.name + '</span><span class="statusLine-test">' +
                  (f.size / 1024).toFixed(1) + ' KB</span>';
                document.body.appendChild(card);
              };
            };
          </script>
        """)
        self.driver = QianwenDriver(WEB_DRIVERS["Qianwen"])
        self.driver._page = self.page
        self.driver._browser = SimpleNamespace(context=self.context)
        self.account = mock.patch("web_drivers.account_status.inspect_account",
                                  return_value={"status": "ready"})
        self.account.start()

    def tearDown(self):
        self.account.stop()
        self.context.close()

    def test_long_prompt_uploads_complete_text_and_sends_short_instruction(self):
        prompt = "任务正文 JSON 输出\n" + ("素材段落。" * 2200) + "\n末尾schema:{\"ending\":\"保留\"}"
        self.assertGreater(len(prompt), 10000)
        self.driver.input(prompt)
        uploaded = self.page.evaluate("window.uploadedText")
        composer = self.page.locator("[role='textbox']").inner_text()
        augmented = prompt + "\n\n本次结果由程序读取：请直接在当前对话返回完整、合法的 JSON；保留字符串内的转义字符，不创建文档或卡片，不附建议问题或其他说明。"
        self.assertEqual(uploaded, augmented)
        self.assertIn("末尾schema", uploaded)
        self.assertLess(len(composer), 500)
        self.assertIn("请完整读取附件", composer)
        self.assertEqual(self.page.locator("[class*='fileBox-']").count(), 1)
        self.assertRegex(self.page.locator("[class*='statusLine-']").inner_text(), r"[\d.]+\s*KB")

    def test_short_prompt_stays_in_composer_without_attachment(self):
        self.driver.input("请用三句话概括这段材料。")
        self.assertEqual(self.page.evaluate("window.uploadedText"), "")
        self.assertEqual(self.page.locator("[class*='fileBox-']").count(), 0)
        self.assertEqual(self.page.locator("[role='textbox']").inner_text(), "请用三句话概括这段材料。")

    def test_upload_failure_does_not_leave_a_sendable_prompt(self):
        self.page.evaluate("window.failUpload = true")
        prompt = "长任务\n" + ("内容。" * 4000)
        self.page.on("pageerror", lambda _: None)
        with self.assertRaises(Exception):
            self.driver.input(prompt)
        self.assertEqual(self.page.locator("[role='textbox']").inner_text(), "")


if __name__ == "__main__":
    unittest.main()
