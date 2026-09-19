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

# ============================================================
# 回复正文的 markdown 逐块重建（DeepSeek / 豆包共用同一实现）
# ============================================================
# 站点把 markdown 渲染成 DOM（章节标题 ## **N** 变成 h2 元素），只读容器
# innerText 会把标题语法整体丢掉——故事正文里只剩一个裸的章节号，于是
# validate_story_format 的「章节 0 个」必扣 4 分：通道满分只剩 6/10，
# 任何一项再扣分（哪怕只差几十字）就直接判废稿，合规稿也被误杀。
# （2026-09-19 真实事故：DeepSeek 通道 5 轮里 4 篇完整稿就是这么丢的）
# 逐块遍历块级子元素：h1-h6 还原成 ## **N**，其余块按段落拼接（空行分隔，
# 与知乎编辑器分段一致）。豆包 2026-09-09 实测同一篇 3/10 → 10/10；
# DeepSeek 2026-09-19 接入同一实现。
# 用法（JS 片段，需放在使用它的 evaluate 函数体开头）：
#   toMarkdown(el) → 重建后的全文；失败时调用方回落 el.innerText。
MARKDOWN_REBUILD_JS = (
    "const NL = String.fromCharCode(10);"
    "const toMarkdown = el => {"
    "  const parts = [];"
    "  const walk = n => {"
    "    const tag = n.tagName;"
    "    if (/^H[1-6]$/.test(tag)) {"
    "      const t = (n.innerText || '').trim();"
    "      if (t) parts.push('#'.repeat(Number(tag[1])) + ' **' + t + '**');"
    "      return;"
    "    }"
    "    const kids = Array.from(n.children).filter(c =>"
    "        /^(DIV|P|H[1-6]|LI|BLOCKQUOTE|PRE)$/.test(c.tagName));"
    "    if (kids.length) { kids.forEach(walk); return; }"
    "    const t = (n.innerText || '').trim();"
    "    if (t) parts.push(t);"
    "  };"
    "  Array.from(el.children).forEach(walk);"
    "  return parts.join(NL + NL);"
    "};"
)


class WebLLMDriver:
    """网页版大模型驱动基类：浏览器会话 + 有界页面交互。

    子类只需实现：setup()、input(prompt)、send()、wait_complete()、
    read_result()；基类提供会话管理与 DOM 探测工具。
    """

    def __init__(self, config):
        self.config = config or {}
        self._page = None
        self._browser = None
        # ---- 会话生命周期状态（2026-09 新增：单链路一会话 + 完成后删除）----
        self._session_owned = False   # 本驱动是否在本会话里发过 prompt（只删自己用过的）
        self._session_broken = False  # 页面/会话损坏 → 下次开新会话
        self._session_id = None       # 站点侧会话 ID（探测得到时记录，删除用）
        self._session_title = ""      # 本会话首条用户消息（DOM 侧栏匹配删除用）

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
        """打开网页版 LLM 站点（导航到配置 URL）。

        ★ 导航内置重试：重试链路里每次 generate 都会重新 goto，网络
        瞬时抖动曾让第 2 次尝试直接击穿整个任务（2026-08-27 日志：
        首发 1456 字正常、重试时 goto 超时 → 全盘报错）。现在首试
        20s + 重试 35s（慢站点放宽），两次都失败才抛错。
        """
        from web_drivers.browser_pool import _check_cancel
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        url = WEB_DRIVERS[WEB_DRIVER_NAME]["url"]
        attempts = [(20000, "domcontentloaded"), (35000, "domcontentloaded")]
        page = self._page_instance()
        last_exc = None
        for i, (timeout_ms, wait_until) in enumerate(attempts, 1):
            _check_cancel()
            try:
                page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                log.info("web_drivers: 已打开 %s", url)
                return self
            except Exception as exc:
                last_exc = exc
                log.warning("web_drivers: 打开 %s 失败（第 %d/%d 次，%ds 超时）：%s",
                            url, i, len(attempts), timeout_ms // 1000, exc)
        raise RuntimeError(f"网页版 LLM 站点打开失败：{url}") from last_exc

    def close_session(self):
        """关闭本 driver 的独立页面（不关共享浏览器）。"""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        self._reset_session_state()

    def new_chat(self):
        """重置当前页为全新对话（重新导航到站点 URL，丢弃历史上下文）。

        并行调度每派发一个任务前调用，防止多轮对话历史污染。
        默认实现 = open_session()；子类可覆盖以等待 SPA 渲染。
        同时清空本驱动的会话归属记录（新会话未用过，删除钩子不会误删）。
        """
        self._reset_session_state()
        return self.open_session()

    def continue_chat(self, prompt):
        """在同一会话中继续提问（不重置对话历史，问题连贯）。

        默认实现：直接写入输入框并发送（会话延续）。
        用于同一链路的连续 LLM 请求 / 同一生成任务的修正追问，
        让上下文连贯且不再新开页面。
        """
        self.input(prompt)
        self.send()

    # ---------------- 会话生命周期（单链路一会话 + 完成后删除） ----------------

    def _can_reuse_session(self):
        """当前是否存在本驱动创建且健康、可继续提问的会话。"""
        return (self._page is not None
                and not self._page.is_closed()
                and self._session_owned
                and not self._session_broken)

    def _mark_session_used(self, prompt):
        """记录本会话已被本驱动使用（发过 prompt）——删除钩子只删这种。"""
        if not self._session_owned:
            self._session_owned = True
            self._session_title = (str(prompt) or "").strip()[:24]
            self._session_id = self._detect_session_id()

    def _mark_session_broken(self):
        """标记当前会话损坏：下次 generate 自动新开会话（除非坏才开新）。"""
        if not self._session_broken:
            log.warning("web_drivers: 当前会话标记为损坏，"
                        "下次提问将新开会话")
        self._session_broken = True

    def _reset_session_state(self):
        self._session_owned = False
        self._session_broken = False
        self._session_id = None
        self._session_title = ""

    def _detect_session_id(self):
        """探测站点侧当前会话 ID（子类覆写；默认无）。"""
        return None

    def delete_current_session(self):
        """删除本驱动本次「创建/使用过」的网页版会话（完成后清理）。

        只删除 _session_owned 的会话——绝不触碰用户网页里已有的其他
        会话。删除失败只记日志不抛异常（不影响任务结果）。
        返回是否删除成功（尽力而为）。
        """
        if not self._session_owned:
            log.info("web_drivers: 无可删除会话（本驱动本次未使用网页会话）")
            return False
        if self._page is None or self._page.is_closed():
            log.info("web_drivers: 页面已关闭，跳过会话删除")
            self._reset_session_state()
            return False
        deleted = False
        try:
            deleted = bool(self._delete_current_session_impl())
        except Exception as exc:
            log.warning("web_drivers: 删除会话异常（不阻断）：%s", exc)
        if deleted:
            log.info("web_drivers: 已删除本次网页会话%s",
                     (f"（id={self._session_id}）" if self._session_id else ""))
        else:
            log.warning("web_drivers: 本次网页会话未能自动删除"
                        "（站点接口/DOM 未命中，可稍后在网页端手动清理）")
        self._reset_session_state()
        return bool(deleted)

    def _delete_current_session_impl(self):
        """站点内删除实现（子类覆写）；完成返回 True。"""
        return False

    def _after_wait_before_read(self):
        """wait_complete 之后、read_result 之前的站点钩子（默认无操作）。

        供站点驱动在读取前做补救：如豆包把故事放到卡片/文档交付界面时，
        在同一会话内补问一句让模型把全文直接输出到对话，再等它完成。
        """
        return None

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
        self._mark_session_broken()
        # 站点名/驱动模块按当前驱动动态给出（豆包/DeepSeek 共用本兜底，
        # 写死 DeepSeek 会把豆包故障指向错误文件）
        from config import WEB_DRIVER_NAME
        from web_drivers import _DRIVER_REGISTRY
        module_path = _DRIVER_REGISTRY.get(WEB_DRIVER_NAME, ("", ""))[0]
        raise RuntimeError(
            f"{hint}。{WEB_DRIVER_NAME} 前端可能改版，上方日志中的页面状态"
            f"可协助修复 {module_path or 'web_drivers/*'}.py 选择器")

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

    def generate(self, prompt, reuse_session=True):
        """完整生成流程（生命周期固定，子类复用）。

        reuse_session=True（默认）：若已有本驱动创建的健康会话，在
        同一会话内继续提问（不重新导航、不新开会话、上下文连贯）——
        单次完整链路从始至终只开一个聊天会话；仅在会话损坏时新开。
        reuse_session=False：总是新开会话（并行调度各 slot 用）。
        生成超时时标记会话损坏，下次自动新开。
        """
        if reuse_session and self._can_reuse_session():
            log.info("web_drivers: 复用当前会话继续提问（单链路一会话）")
            self.setup()
            self.continue_chat(prompt)
        else:
            if self._session_broken:
                log.info("web_drivers: 上一会话已损坏，新开会话")
            self.new_chat()
            self.setup()
            self.input(prompt)
            self.send()
        self._mark_session_used(prompt)
        completed = self.wait_complete(max_wait=self.config.get("max_wait"))
        if not completed:
            self._mark_session_broken()
            log.warning("web_drivers: 生成未在期限内完成，会话标记损坏")
        # 会话 ID 名额在首轮回复后页面 URL 才带上（如豆包 /chat/{id}），
        # 发送瞬间探测不到——完成后补一次，供 delete_current_session 使用
        if not self._session_id:
            self._session_id = self._detect_session_id()
        # 站点钩子：读取前补救机会（豆包卡片式交付 → 要求全文内联等）
        self._after_wait_before_read()
        return self.read_result()
