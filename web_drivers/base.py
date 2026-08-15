# ============================================================
# web_drivers/base.py — 网页版大模型驱动基类（DOM 语义化）
#
# 与知乎 browser_adapter 同一技术栈：Playwright 持久化会话 +
# DOM 指令，与物理鼠标/坐标/OCR 完全解绑。
#   - 复用 get_browser() 共享持久化 context（同一 data/browser_profile，
#     登录 cookie 共存；profile 锁不允许第二实例）
#   - driver 用 context.new_page() 开独立页面，不碰知乎流程的
#     browser.page；close_session() 关页
#   - 所有页面交互走 _safe_evaluate（Promise.race 自限时哨兵 +
#     取消检查点），失败返回 None 不阻塞流程
#
# 生命周期（workflows/base.py 的 _generate_web_short_form 依赖此不变）：
#   generate(prompt) → open_session → setup → input → send
#                    → wait_complete → read_result
# 并行调度（web_drivers/parallel.py）每任务用 new_chat 重置会话：
#   new_chat → setup → input → send → 轮询（非阻塞原语）→ read_result
# ============================================================

import logging

log = logging.getLogger(__name__)

# 页面交互超时（毫秒）：与 browser_adapter 约定一致，所有 evaluate 有界
_EVAL_TIMEOUT = 15000


class WebLLMDriver:
    """网页版大模型驱动基类：浏览器会话 + 有界页面交互。

    子类只需实现：setup()、input(prompt)、send()、wait_complete()、
    read_result()；基类提供会话管理与 DOM 探测工具。
    """

    def __init__(self, config):
        self.config = config or {}
        self._page = None
        self._browser = None

    # ---------------- 会话管理 ----------------

    def _get_browser(self):
        """共享持久化浏览器（知乎流程同实例，避免 profile 锁冲突）。

        每次任务结束后 webui/server.py 会 close_shared_browser() 关闭
        共享浏览器（context 置 None、全局引用清空）。本 driver 单例跨
        任务存活时缓存的旧引用已失效（context 为 None），必须重新获取，
        否则 .context.new_page() 报 'NoneType' has no attribute。
        """
        if (self._browser is None
                or getattr(self._browser, "context", None) is None):
            from web_drivers.browser_pool import get_browser
            self._browser = get_browser()
            self._page = None  # 旧页来自已关闭的 context，一并丢弃
        return self._browser

    def _page_instance(self):
        """惰性开独立页面（不碰知乎流程的 browser.page）。"""
        if self._page is None or self._page.is_closed():
            self._page = self._get_browser().context.new_page()
        return self._page

    def open_session(self):
        """打开网页版 LLM 站点（导航到配置 URL）。"""
        from web_drivers.browser_pool import _check_cancel
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        url = WEB_DRIVERS[WEB_DRIVER_NAME]["url"]
        _check_cancel()
        page = self._page_instance()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            log.warning("web_drivers: 打开 %s 失败：%s", url, exc)
            raise RuntimeError(f"网页版 LLM 站点打开失败：{url}") from exc
        log.info("web_drivers: 已打开 %s", url)
        return self

    def close_session(self):
        """关闭本 driver 的独立页面（不关共享浏览器）。"""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

    def new_chat(self):
        """重置当前页为全新对话（重新导航到站点 URL，丢弃历史上下文）。

        并行调度每派发一个任务前调用，防止多轮对话历史污染。
        默认实现 = open_session()；子类可覆盖以等待 SPA 渲染。
        """
        return self.open_session()

    # ---------------- 有界页面交互 ----------------

    def _safe_evaluate(self, js, *args, timeout=_EVAL_TIMEOUT):
        """执行页面 JS，失败返回 None；JS 内部带自限时哨兵。

        有界页面交互（实现下沉 web_drivers/browser_pool.safe_evaluate）。"""
        from web_drivers.browser_pool import safe_evaluate
        return safe_evaluate(self._page_instance(), js, *args,
                             timeout=timeout)

    def _probe_selectors(self, candidates, attr="innerText"):
        """从候选 selector 列表返回首个命中元素（含其指定属性）。

        返回 (selector, value)；全部未命中返回 (None, None)。
        供子类定位输入框/发送按钮/回复容器——前端改版时扩展
        候选列表即可，避免硬编码单一 selector。"""
        if not candidates:
            return None, None
        js = (
            "async function() {"
            "  for (const s of arguments[0]) {"
            "    const el = document.querySelector(s);"
            "    if (el) return {sel: s, val: el.%s};"
            "  }"
            "  return {sel: null, val: null};"
            "}" % attr
        )
        try:
            r = self._safe_evaluate(js, list(candidates)) or {}
            return r.get("sel"), r.get("val")
        except Exception:
            return None, None

    # ---------------- 失败降级 ----------------

    def _dump_page_state(self, hint):
        """页面状态 dump（URL/标题/可见文本片段），供前端改版时人工介入。"""
        from web_drivers.browser_pool import WorkflowCancelled
        page = self._page_instance()
        state = {}
        try:
            state["url"] = page.url
        except Exception:
            pass
        try:
            state["title"] = page.title()
        except Exception:
            pass
        try:
            state["body_text"] = (self._safe_evaluate(
                "() => document.body.innerText.slice(0, 200)") or "")[:200]
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        log.error("web_drivers: %s。页面状态：url=%s title=%s body=%s",
                  hint, state.get("url"), state.get("title"),
                  state.get("body_text", "")[:80])
        raise RuntimeError(
            f"{hint}。DeepSeek 前端可能改版，上方日志中的页面状态"
            f"可协助修复 web_drivers/deepseek.py 选择器")

    # ---------------- 生命周期（子类实现） ----------------

    def setup(self):
        raise NotImplementedError

    def input(self, prompt):
        raise NotImplementedError

    def send(self):
        raise NotImplementedError

    def wait_complete(self, max_wait=None):
        raise NotImplementedError

    def read_result(self):
        raise NotImplementedError

    def generate(self, prompt):
        """完整生成流程（生命周期固定，子类复用）。"""
        self.open_session()
        self.setup()
        self.input(prompt)
        self.send()
        self.wait_complete(max_wait=self.config.get("max_wait"))
        return self.read_result()
