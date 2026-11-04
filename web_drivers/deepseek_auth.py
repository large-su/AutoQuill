"""Observe DeepSeek's explicit authentication failures without retaining secrets."""
import logging
import re
from urllib.parse import urlsplit

from web_drivers.browser_pool import WebAccountRestricted, WebLoginRequired

log = logging.getLogger(__name__)
_AUTH_CODES = {40002, 40003}  # Official web client: MISSING_TOKEN, INVALID_TOKEN.
_RESTRICTED_CODES = {40012, 50006}  # USER_IS_BANNED, MUTED in the official client.


class DeepSeekAuthObserver:
    """Page-scoped evidence, read only after a response has finished downloading.

    SSE streams are never read. A WAF challenge, rate limit or server error alone
    does not establish logout. Explicit auth and account-restriction codes stop
    task pages; the interactive login window can start with expired credentials.
    """

    def __init__(self, page, ignore_auth_errors=False):
        self.page = page
        self._rejection = None
        self._ignore_auth_errors = ignore_auth_errors
        page.on("requestfinished", self._request_finished)

    def _request_finished(self, request):
        if self._rejection is not None:
            return
        try:
            response = request.response()
            if response is None:
                return
            url = urlsplit(response.url)
            if (url.scheme != "https" or url.netloc != "chat.deepseek.com"
                    or not url.path.startswith("/api/v0/")):
                return
            status = response.status
            if status == 401 and self._ignore_auth_errors:
                return
            code = biz_code = None
            if status != 401:
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    return
                payload = response.json()
                if not isinstance(payload, dict):
                    return
                code = payload.get("code")
                data = payload.get("data")
                biz_code = data.get("biz_code") if isinstance(data, dict) else None
                # Keep only protocol integers; response text and headers may contain secrets.
                code = code if type(code) is int else None
                biz_code = biz_code if type(biz_code) is int else None
                accepted_codes = (_RESTRICTED_CODES if self._ignore_auth_errors
                                  else _AUTH_CODES | _RESTRICTED_CODES)
                if not {code, biz_code} & accepted_codes:
                    return
            endpoint = url.path if re.fullmatch(r"/api/v0/[a-z_/]+", url.path) else "/api/v0/[other]"
            self._rejection = (status, code, biz_code)
            log.warning(
                "DeepSeek 账号不可用：endpoint=%s http=%s code=%s biz_code=%s",
                endpoint, status, code, biz_code)
        except Exception:
            # A response disappearing during navigation is not evidence of logout.
            return

    def raise_if_rejected(self):
        if self._rejection is not None:
            status, code, biz_code = self._rejection
            restricted_code = next((c for c in (code, biz_code)
                                    if c in _RESTRICTED_CODES), None)
            if restricted_code is not None:
                from web_drivers.account_status import remember_status
                message = (f"DeepSeek 账号已被平台限制使用（状态码 {restricted_code}），"
                           "请到官网查看限制说明或申诉，也可以在设置中手动选择其他模型")
                remember_status("DeepSeek", "restricted", message)
                raise WebAccountRestricted(message)
            auth_code = code if code in _AUTH_CODES else biz_code
            detail = f"HTTP {status}"
            if auth_code is not None:
                detail += f"，认证码 {auth_code}"
            raise WebLoginRequired(
                "DeepSeek 网页登录已失效"
                f"（{detail}），"
                "请在设置中重新登录后继续")
