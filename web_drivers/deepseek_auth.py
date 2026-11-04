"""Observe DeepSeek's explicit authentication failures without retaining secrets."""
import logging
import re
from urllib.parse import urlsplit

from web_drivers.browser_pool import WebLoginRequired

log = logging.getLogger(__name__)
_AUTH_CODES = {40002, 40003}  # Official web client: MISSING_TOKEN, INVALID_TOKEN.


class DeepSeekAuthObserver:
    """Page-scoped evidence, read only after a response has finished downloading.

    SSE streams are never read. A WAF challenge, rate limit or server error alone
    does not establish logout. Only a 401 or the site's auth codes is terminal.
    """

    def __init__(self, page):
        self.page = page
        self._rejection = None
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
                if code not in _AUTH_CODES and biz_code not in _AUTH_CODES:
                    return
            endpoint = url.path if re.fullmatch(r"/api/v0/[a-z_/]+", url.path) else "/api/v0/[other]"
            self._rejection = (status, code, biz_code)
            log.warning(
                "DeepSeek 认证失效：endpoint=%s http=%s code=%s biz_code=%s",
                endpoint, status, code, biz_code)
        except Exception:
            # A response disappearing during navigation is not evidence of logout.
            return

    def raise_if_rejected(self):
        if self._rejection is not None:
            status, code, biz_code = self._rejection
            auth_code = code if code in _AUTH_CODES else biz_code
            detail = f"HTTP {status}"
            if auth_code is not None:
                detail += f"，认证码 {auth_code}"
            raise WebLoginRequired(
                "DeepSeek 网页登录已失效"
                f"（{detail}），"
                "请在设置中重新登录后继续")
