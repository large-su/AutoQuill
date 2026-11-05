"""Provider-bound account checks. Never return credentials or treat busy as login."""
import threading
import time
from urllib.parse import urlsplit

from web_drivers import browser_pool as pool

_states = {}
_lock = threading.Lock()
_SITES = {
    "DeepSeek": ("chat.deepseek.com",),
    "Doubao": ("www.doubao.com",),
    "Kimi": ("www.kimi.com", "kimi.com", "kimi.moonshot.cn"),
    "Qianwen": ("www.qianwen.com", "qianwen.com"),
    "Yuanbao": ("yuanbao.tencent.com",),
    "ChatGLM": ("chatglm.cn",),
    "Aizex": ("aizex.net",),
}
_MESSAGES = {
    "ready": "已登录，聊天界面可用",
    "logged_out": "尚未登录，请打开网页登录",
    "restricted": "平台提示账号受限，请到官网查看说明或申诉，或手动选择其他模型",
    "challenge": "网页需要人工验证，请打开网页完成验证后再次检查",
    "unknown": "暂无法确认账号状态，请稍后重试；这不表示已退出登录",
    "busy": "浏览器正在使用中，请在当前任务或登录结束后检查",
}

# Read only booleans: no cookies, tokens, account identifiers or chat text leave JS.
_STATE_JS = r"""(name) => {
    const visible = e => !!(e && (e.offsetWidth || e.offsetHeight) &&
        getComputedStyle(e).visibility !== 'hidden');
    const any = s => [...document.querySelectorAll(s)].some(visible);
    const inputs = {
        DeepSeek: 'textarea,[contenteditable="true"]',
        Doubao: '.tiptap[contenteditable="true"],textarea',
        Kimi: '.chat-input-editor[contenteditable="true"]',
        Qianwen: '[data-slate-editor="true"][contenteditable="true"]',
        Yuanbao: '.ql-editor[contenteditable="true"]',
        ChatGLM: 'textarea.scroll-display-none,textarea',
        Aizex: '#prompt-textarea'
    };
    const composer = [...document.querySelectorAll(inputs[name] || 'textarea')]
        .some(e => visible(e) && !e.disabled && !e.readOnly);
    const controls = [...document.querySelectorAll(
        'button,a,[role="button"],[data-testid="to_login_button"],[class*="login"],[class*="Login"],.sidebar-user-name')];
    const login = controls.some(e => visible(e) &&
        /^(登录|立即登录|登录\/注册|登录或注册|登录以同步历史会话|未登录|去登录|登录账号|登录送积分好礼)$/.test((e.innerText || '').trim()));
    let account = [...document.querySelectorAll('button,a,[role="menuitem"]')]
        .some(e => visible(e) && /^(退出登录|退出账号|登出|Log out|Sign out)$/.test((e.innerText || '').trim()));
    // Restriction evidence must be site UI, not a user prompt or generated story.
    const notices = [...document.querySelectorAll(
        '[role="alert"],[role="dialog"],[class*="toast"],[class*="Toast"],.ant-message-notice-content,.t-message,.ban-container')]
        .filter(visible).map(e => (e.innerText || '').slice(0, 1200)).join('\n');
    const restricted = (name === 'ChatGLM' && any('.ban-container')) ||
        /(?:账号|账户|帐号).{0,18}(?:封禁|封停|封号|禁用|限制使用|禁止使用|暂停服务)|(?:account).{0,30}(?:banned|suspended|disabled)/i.test(notices);
    const challenge = any('iframe[src*="captcha"],iframe[src*="challenge"],#challenge-running,.tcaptcha-popup,.secsdk-captcha-drag-wrapper') ||
        /^(Just a moment|安全验证|人机验证|滑动验证|滑块验证|访问验证)/i.test(document.title);
    let credential = false;
    if (name === 'DeepSeek') {
        try { const t = JSON.parse(localStorage.getItem('userToken') || 'null');
              credential = !!(t && typeof t.value === 'string' && t.value.trim()); } catch (_) {}
    }
    if (name === 'Kimi') {
        // Official auth store; anonymous_access_token is deliberately excluded.
        credential = ['access_token', 'msh_user_id'].every(k => {
            const v = localStorage.getItem(k); return v && v.trim() && !['null','undefined'].includes(v);
        });
    }
    if (name === 'Qianwen') {
        const user = window._USER_;
        account = account || !!(user && (user.userId || user.aliyunUid));
    }
    if (name === 'Yuanbao') {
        const nick = document.querySelector('.nick-info-name');
        account = account || !!(visible(nick) && (nick.innerText || '').trim() &&
            !/未登录|登录/.test(nick.innerText));
    }
    if (name === 'Aizex') {
        account = account || any('[data-testid="accounts-profile-button"]');
    }
    return {composer, login, account, restricted, challenge, credential};
}"""


def remember_status(name, status, message=None):
    result = {"provider": name, "status": status,
              "message": message or _MESSAGES[status], "checked_at": time.time()}
    with _lock:
        _states[name] = result
    return dict(result)


def get_account_status(name):
    with _lock:
        return dict(_states.get(name) or {
            "provider": name, "status": "unknown", "message": "尚未检查", "checked_at": None})


def _validate(name):
    from config import WEB_DRIVERS
    if name not in _SITES or name not in WEB_DRIVERS:
        raise ValueError("未知的网页版模型")
    return WEB_DRIVERS[name]["url"]


def _auth_cookie(context, name):
    # Doubao's auth cookie names are verified by its existing adapter. An
    # arbitrary tracking cookie is never sufficient login evidence.
    if name not in ("Doubao", "ChatGLM"):
        return False
    if name == "Doubao":
        from web_drivers.doubao import _DOUBAO_AUTH_COOKIE_NAMES
        names = set(_DOUBAO_AUTH_COOKIE_NAMES)
        domains = ("doubao.com", "www.doubao.com")
    else:
        # Official ChatGLM token helper writes these cookies after authentication.
        names = {"chatglm_token"}
        domains = ("chatglm.cn",)
    return any(c.get("name") in names and c.get("value")
               and (c.get("expires", -1) <= 0 or c["expires"] > time.time())
               and c.get("domain", "").lstrip(".") in domains
               for c in context.cookies())


def observe_account(name, page, login=False):
    if name == "DeepSeek":
        from web_drivers.deepseek_auth import DeepSeekAuthObserver
        # The login window intentionally starts signed out. Do not latch its
        # initial 401 forever after a successful sign-in; ban/mute still stop it.
        observer = DeepSeekAuthObserver(page, ignore_auth_errors=login)
        page._aq_account_observer = observer
        return observer
    return None


def inspect_account(name, context, page):
    """Inspect an already-open page without navigation or changing selection."""
    _validate(name)
    url = urlsplit(page.url if isinstance(page.url, str) else "")
    allowed = url.scheme == "https" and url.netloc in _SITES[name]
    if name == "Aizex":
        from web_drivers.aizex import is_chat_url
        allowed = is_chat_url(page.url)
    if not allowed:
        return remember_status(name, "unknown")
    observer = getattr(page, "_aq_account_observer", None)
    if observer:
        try:
            observer.raise_if_rejected()
        except pool.WebAccountRestricted as exc:
            return remember_status(name, "restricted", str(exc))
        except pool.WebLoginRequired:
            return remember_status(name, "logged_out")
    if name == "DeepSeek" and url.path.rstrip("/") == "/banned":
        return remember_status(name, "restricted")
    if any(p in ("login", "signin", "sign_in", "sign-in") for p in url.path.lower().split("/")):
        return remember_status(name, "logged_out")
    facts = pool.safe_evaluate(page, _STATE_JS, name)
    if not isinstance(facts, dict):
        return remember_status(name, "unknown")
    if facts.get("restricted"):
        return remember_status(name, "restricted")
    if facts.get("challenge"):
        return remember_status(name, "challenge")
    if facts.get("login"):
        return remember_status(name, "logged_out")
    if facts.get("composer") and (facts.get("credential") or facts.get("account")
                                  or _auth_cookie(context, name)):
        return remember_status(name, "ready")
    return remember_status(name, "unknown")


def _visit(name, login, timeout, discover=False):
    url = _validate(name)
    if pool.profile_in_use() or pool.live_browsers() or not pool._browser_lock.acquire(blocking=False):
        return remember_status(name, "busy")
    try:
        # Recheck under the launch lock; browser.start also holds the profile lease.
        if pool.profile_in_use() or pool.live_browsers():
            return remember_status(name, "busy")
        with pool.create_browser(headless=not login) as browser:
            page = browser.context.new_page()
            observe_account(name, page, login=login)
            try:
                from playwright.sync_api import TimeoutError as NavigationTimeout
                try:
                    if name != "Aizex":
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except NavigationTimeout:
                    pass  # A usable SPA may render before navigation finishes.
                if name == "Aizex":
                    from web_drivers.aizex import enter_chat
                    chat = enter_chat(browser.context, page, login=login,
                                      timeout=timeout if login else 10)
                    if chat is not page:
                        page.close()
                        page = chat
                deadline = time.monotonic() + timeout
                result = get_account_status(name)
                while time.monotonic() < deadline:
                    pool._check_cancel()
                    result = inspect_account(name, browser.context, page)
                    if result["status"] in ("ready", "restricted"):
                        if login and result["status"] == "ready":
                            browser.save_storage_state()
                        if discover and result["status"] == "ready":
                            from web_drivers.capabilities import refresh_capabilities
                            result["capabilities"] = refresh_capabilities(name, page)
                            with _lock:
                                _states[name] = dict(result)
                        return result
                    if not login and result["status"] in ("logged_out", "challenge"):
                        return result
                    page.wait_for_timeout(500)
                return result
            finally:
                page.close()
    except pool.WorkflowCancelled:
        raise
    except pool.ProfileBusy:
        return remember_status(name, "busy")
    except pool.WebLoginRequired as exc:
        return remember_status(name, "logged_out", str(exc))
    except Exception:
        # Browser errors can include page content/URLs. Keep status messages static.
        return remember_status(name, "unknown")
    finally:
        pool._browser_lock.release()


def check_account(name, refresh=True):
    return _visit(name, login=False, timeout=8, discover=refresh)


def login_account(name, timeout=300):
    result = _visit(name, login=True, timeout=timeout, discover=True)
    return result["status"] == "ready", result["message"]


def logged_in(name):
    result = check_account(name, refresh=False)
    if result["status"] == "ready":
        return True
    if result["status"] == "logged_out":
        return False
    if result["status"] == "restricted":
        raise pool.WebAccountRestricted(result["message"])
    if result["status"] == "challenge":
        raise pool.WebVerificationRequired(result["message"])
    raise pool.WebLoginCheckError(result["message"])
