"""Aizex portal authorization followed by the provider's ChatGPT-style web UI."""
import logging
import time
from urllib.parse import urlsplit

from web_drivers.text_chat import ConfiguredTextChatDriver, generic_logged_in, generic_login

log = logging.getLogger(__name__)
PORTAL = "https://aizex.net/plusPool"


def is_chat_url(url):
    parsed = urlsplit(url or "")
    host = parsed.hostname or ""
    return parsed.scheme == 'https' and host.endswith('.memofun.net') and host != 'c.memofun.net'


def enter_chat(context, portal_page, login=False, timeout=40):
    """Authorize through the portal once per live browser context.

    The verified origin lives only in this context, allowing independent chat
    tabs without repeatedly refreshing the portal's shared authorization.
    """
    from web_drivers.browser_pool import _check_cancel, WebLoginRequired, WebLoginCheckError
    origin = getattr(context, '_aq_aizex_authorized_origin', None)
    if origin and is_chat_url(origin):
        portal_page.goto(origin, wait_until='domcontentloaded', timeout=20000)
        try:
            portal_page.locator('#prompt-textarea').wait_for(state='visible', timeout=8000)
            return portal_page
        except Exception:
            context._aq_aizex_authorized_origin = None
    portal_page.goto(PORTAL, wait_until='domcontentloaded', timeout=25000)
    deadline = time.monotonic() + timeout
    entry = portal_page.get_by_role('button', name='立即进入 Web 使用页面', exact=True)
    while time.monotonic() < deadline:
        _check_cancel()
        if entry.count() and entry.is_visible():
            break
        if (not login and portal_page.locator('input[type="password"]').filter(visible=True).count()
                and time.monotonic() > deadline - timeout + 3):
            raise WebLoginRequired('Aizex 中转站需要登录，请从中转站入口登录后再一键使用')
        portal_page.wait_for_timeout(300)
    else:
        raise WebLoginCheckError('Aizex 中转站尚未确认授权入口，请打开网页检查登录或验证状态')
    previous = {page: page.url for page in context.pages}
    entry.click(timeout=6000)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        _check_cancel()
        for page in context.pages:
            if page is portal_page or page not in previous or page.url != previous[page]:
                if is_chat_url(page.url) and page.locator('#prompt-textarea').count():
                    page.wait_for_timeout(1000)
                    parsed = urlsplit(page.url)
                    context._aq_aizex_authorized_origin = f'{parsed.scheme}://{parsed.netloc}/'
                    log.info('Aizex 一键使用已进入聊天站点：%s', urlsplit(page.url).hostname)
                    return page
        portal_page.wait_for_timeout(300)
    states = []
    for page in context.pages:
        if page is portal_page or page not in previous or page.url != previous[page]:
            states.append({'host': urlsplit(page.url).hostname, 'title': page.title(),
                           'composer': bool(page.locator('#prompt-textarea').count())})
    log.warning('Aizex 授权跳转未就绪：%s', states)
    raise WebLoginCheckError('Aizex 一键使用后未确认聊天界面，请检查中转站授权或稍后重试')


class AizexDriver(ConfiguredTextChatDriver):
    provider_name = 'Aizex'
    input_selectors = ('#prompt-textarea[contenteditable="true"]', '#prompt-textarea')
    send_selectors = ('#composer-submit-button', 'button[data-testid="send-button"]')
    reply_selectors = ('[data-message-author-role="assistant"] .markdown',
                       '[data-message-author-role="assistant"] [data-dil-widget-copy-target]')
    stop_selectors = ('button[data-testid="stop-button"]', 'button[aria-label="停止生成"]')
    done_selectors = ('button[data-testid="copy-turn-action-button"][aria-label="复制回复"]',)

    def _generation_probe(self):
        interrupted = self._safe_evaluate(r"""()=>[...document.querySelectorAll('div,p,span')]
          .filter(e=>!e.closest('[data-message-author-role]')&&(e.offsetWidth||e.offsetHeight))
          .some(e=>e.childElementCount<5 && /^(本轮响应中断[，,。]|出了点问题[。.]?\s*请重试[。.]?)/.test((e.innerText||'').trim()))""")
        if interrupted:
            from web_drivers.browser_pool import WebResponseInterrupted
            partial = self._new_reply_text()
            from core import paths
            from pathlib import Path
            import json
            target = Path(paths.data('logs', 'web_model_failures'))
            target.mkdir(parents=True, exist_ok=True)
            report = target / ('aizex-interrupted-' + str(time.time_ns()) + '.json')
            report.write_text(json.dumps({'provider': self.provider_name, 'url': self._page_instance().url,
                                         'message': '本轮响应中断', 'partial': partial},
                                        ensure_ascii=False, indent=2), encoding='utf-8')
            raise WebResponseInterrupted(f'Aizex 提示本轮响应中断；部分内容已保存：{report}')
        return super()._generation_probe()

    def open_session(self):
        page = self._page_instance()
        self._page = enter_chat(self._get_browser().context, page)
        if page is not self._page:
            page.close()
        self._page.locator('[data-testid="accounts-profile-button"]').filter(visible=True).first.wait_for(
            state='visible', timeout=10000)
        self._raise_if_logged_out()
        return self

    def new_chat(self):
        self._prepare_new_session()
        self.open_session()
        page = self._page_instance()
        # The fresh authorized landing page must contain no previous reply.
        if self._reply_count():
            page.get_by_role('link', name='创建新聊天', exact=True).filter(visible=True).first.click(timeout=5000)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self._reply_count() and self._visible(self.input_selectors):
                return self
            page.wait_for_timeout(250)
        raise RuntimeError('Aizex 未能确认空白新聊天，已停止发送')

    def _detect_session_id(self):
        import re
        page = self._page_instance()
        if not is_chat_url(page.url):
            return None
        match = re.fullmatch(r'/c/([a-fA-F0-9-]{36})/?', urlsplit(page.url).path)
        return match.group(1) if match else None

    def _delete_current_session_impl(self):
        import re
        page, sid = self._page_instance(), self._session_id
        if not is_chat_url(page.url) or not sid or not re.fullmatch(r'[a-fA-F0-9-]{36}', sid):
            return False
        row = page.locator(f'nav a[href="/c/{sid}"]')
        if row.count() != 1:
            return False
        row.hover(timeout=4000)
        more = row.locator('button[aria-label*="对话选项"]')
        if more.count() != 1:
            return False
        more.click(timeout=4000)
        page.get_by_role('menuitem', name=re.compile(r'^删除')).click(timeout=4000)
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('button', name=re.compile(r'^(删除|Delete)$')).click(timeout=4000)
        row.wait_for(state='detached', timeout=8000)
        return True


def web_llm_logged_in():
    return generic_logged_in('Aizex', {})


def login_web_flow(timeout=300):
    return generic_login('Aizex', timeout)
