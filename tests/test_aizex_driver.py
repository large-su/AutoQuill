"""Offline real-DOM coverage of portal routing and the shared chat lifecycle."""
import unittest
from types import SimpleNamespace
from unittest import mock

from web_drivers.aizex import AizexDriver, PORTAL, is_chat_url


class AizexURLTests(unittest.TestCase):
    def test_relay_url_must_be_https_and_under_provider_domain(self):
        self.assertTrue(is_chat_url('https://leopard-x.memofun.net/c/abc'))
        for url in ('http://leopard-x.memofun.net/', 'https://memofun.net.evil.test/',
                    'https://c.memofun.net/', 'https://evil.test/https://x.memofun.net'):
            self.assertFalse(is_chat_url(url))


class AizexDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from applications.zhihu_story.browser_utils import EDGE_PATH
        if not EDGE_PATH:
            raise unittest.SkipTest('Edge not installed')
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=EDGE_PATH, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.visits = []
        self.context = self.browser.new_context()
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()
        self.driver = AizexDriver({'url': PORTAL, 'poll_interval': .02, 'stable_count': 2, 'max_wait': 2})
        self.driver._page = self.page
        self.driver._browser = SimpleNamespace(context=self.context)

    def tearDown(self):
        self.context.close()

    def route(self, route):
        self.visits.append(route.request.url)
        if route.request.url.startswith(PORTAL):
            html = '''<button onclick="window.open('https://leopard-x.memofun.net/')">立即进入 Web 使用页面</button>'''
        else:
            html = '''<button aria-label="打开个人资料菜单" data-testid="accounts-profile-button">账号</button>
              <nav id="history"></nav><main>
              <div id="prompt-textarea" contenteditable="true"></div>
              <button id="composer-submit-button" onclick="send()">发送</button></main>
              <main><button aria-haspopup="menu">模型</button></main>
              <script>
              function send(){
                const input=document.querySelector('#prompt-textarea'); input.innerHTML='';
                history.pushState({},'', '/c/12345678-1234-1234-1234-123456789abc');
                document.querySelector('#history').innerHTML=`<a href='/c/12345678-1234-1234-1234-123456789abc'>本次测试<button aria-label='打开本次测试的对话选项' onclick='event.preventDefault();menu()'>更多</button></a>`;
                const answer=document.createElement('div');answer.dataset.messageAuthorRole='assistant';
                answer.innerHTML='<div class="markdown"><h2>第一章</h2><p>本轮故事正文</p></div>';
                document.querySelector('main').appendChild(answer);
                const copy=document.createElement('button');copy.dataset.testid='copy-turn-action-button';copy.setAttribute('aria-label','复制回复');copy.innerText='复制';
                document.querySelector('main').appendChild(copy);
              }
              function menu(){
                const item=document.createElement('div');item.setAttribute('role','menuitem');item.innerText='删除';
                item.onclick=()=>{item.remove();const dialog=document.createElement('div');dialog.setAttribute('role','dialog');dialog.innerHTML='<button>删除</button>';
                  dialog.querySelector('button').onclick=()=>{document.querySelector('#history').innerHTML='';dialog.remove();};
                  document.body.appendChild(dialog);};document.body.appendChild(item);
              }
              </script>'''
        route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)

    def test_portal_popup_generate_read_and_delete_exact_owned_session(self):
        with mock.patch('web_drivers.capabilities.apply_selection'):
            answer = self.driver.generate('请写一个短故事', reuse_session=False)
        self.assertIn('## **第一章**', answer)
        self.assertIn('本轮故事正文', answer)
        self.assertEqual(self.driver._session_id, '12345678-1234-1234-1234-123456789abc')
        self.assertTrue(self.page.is_closed())
        self.assertTrue(self.driver.delete_current_session())
        self.assertFalse(self.driver._session_owned)

    def test_parallel_tabs_share_authorization_but_have_independent_pages(self):
        self.driver.open_session()
        first = self.driver._page
        other = AizexDriver({'url': PORTAL})
        other._browser = self.driver._browser
        other.open_session()
        self.assertIsNot(first, other._page)
        self.assertFalse(first.is_closed())
        self.assertEqual(sum(url.startswith(PORTAL) for url in self.visits), 1)
        self.assertEqual(other._page.url, 'https://leopard-x.memofun.net/')

