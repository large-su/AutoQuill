# ============================================================
# web_drivers/deepseek.py — DeepSeek 网页版驱动（DOM 语义化）
#
# 重写自 v2.1 的 OCR/坐标实现：现在全部通过 DOM 指令操作
# chat.deepseek.com，与物理鼠标/分辨率/OCR 解绑。
#
# 流程（基类生命周期固定）：
#   open_session → setup（可选模式开关）→ input（fill 输入框）
#   → send（点发送/Enter）→ wait_complete（停止按钮消失 + 文本稳定）
#   → read_result（最后一条助手回复全文）
#
# selector 稳定性：所有关键元素走候选列表 _probe_selectors，
# 前端改版时扩展候选即可；全失败走 _dump_page_state 人工介入。
#
# 运行：python -m web_drivers.deepseek --probe 真实浏览器探测 selector
# ============================================================

import logging
import time

from web_drivers.base import WebLLMDriver

log = logging.getLogger(__name__)

# 输入框候选（chat.deepseek.com 各版本的 textarea/contenteditable）
_INPUT_SELECTORS = (
    "textarea#chat-input",
    "textarea[data-testid='chat_input_input']",
    "textarea[placeholder*='给 DeepSeek']",
    "div[contenteditable='true']",
)

# 发送按钮候选（优先按钮，兜底键盘 Enter）
_SEND_SELECTORS = (
    "button[type='submit']",
    "button[aria-label*='发送']",
    "div[class*='send']",
)

# 生成完成标志：停止按钮消失（DeepSeek 生成时显示停止按钮）。
# 新版 UI 是纯图标 role=button，无 aria-label；生成中动态探测兜底
_STOP_SELECTORS = (
    "button[aria-label*='停止']",
    "button[data-testid*='stop']",
    "button[class*='stop']",
    "div[role=button][class*='stop']",
    "div[aria-label*='停止']",
)

# 回复容器候选：最后一条助手消息
# ★ 首个必须是正文容器（ds-assistant-message-main-content）：
#   深度思考开启时页面有思考容器（ds-think-content）排在正文前，
#   querySelector 只取第一个匹配——若正文不是首位会被思考过程顶掉，
#   造成「文本稳定」误判完成 + 读回思考文本（2026-08-15 实测根因）
_RESULT_SELECTORS = (
    "div[class*='ds-assistant-message-main-content']",
    "div[class*='message'] div[class*='markdown']",
    "div[class*='ds-markdown']",
    "div[class*='assistant'] div[class*='markdown']",
)

# 深度思考容器（实测 2026-08-15：思考中文本持续增长；结束后容器保留，
# 长度不再变化——阶段判定用「长度是否在增长」，不能用容器是否存在）
_THINK_SELECTORS = (
    "div[class*='ds-think-content']",
    "div[class*='ds-think']",
)

# 配置键 → 页面模式 tab 的真实文本（实测：radio 的 innerText 含「模式」）
_MODE_TEXT = {"fast": "快速模式", "expert": "专家模式", "image": "识图模式"}


class DeepSeekDriver(WebLLMDriver):
    """DeepSeek 网页版（chat.deepseek.com）DOM 驱动。"""

    def new_chat(self):
        """重置为全新对话：重新导航 + 等待输入框渲染（SPA 挂载）。

        并行调度每派发一个任务前调用。输入框未在 5s 内渲染不 raise
        ——交给 input() 的 _dump_page_state 带页面状态 loud-fail。
        """
        self.open_session()
        for _ in range(10):
            if self._probe_selectors(_INPUT_SELECTORS, attr="tagName")[0]:
                return self
            self._page_instance().wait_for_timeout(500)
        log.warning("web_drivers: new_chat 后未等到输入框渲染，交给 input 兜底")
        return self

    def setup(self):
        """按目标状态设置模式 tab 与开关（先读后点，不破坏手动状态）。

        目标来自 config：mode（快速/专家）、deep_think、smart_search。
        状态读取：模式 tab 看 radiogroup 里 aria-checked；开关看
        ds-toggle-button 的 --selected 类。与目标不一致才点击。
        """
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        target_mode = cfg.get("mode", "fast")
        target_think = bool(cfg.get("deep_think"))
        target_search = bool(cfg.get("smart_search"))

        # 1. 大模式 tab（快速/专家/识图）
        # 注意：radiogroup 返回的是完整文本（如「快速模式」），而配置是
        # 英文键（fast/expert），必须经 _MODE_TEXT 映射后再比对/查找。
        target_text = _MODE_TEXT.get(target_mode, f"{target_mode}模式")
        current = self._radio_group_selected()
        if current == target_text:
            log.info("web_drivers: 当前已是%s，不动", target_text)
        elif current:
            if not self._click_text(target_text):
                log.warning("web_drivers: 未找到模式 tab「%s」，继续",
                            target_text)
            else:
                log.info("web_drivers: 已切换到%s（原：%s）",
                         target_text, current)
                self._page_instance().wait_for_timeout(1000)
        else:
            log.warning("web_drivers: 未检测到模式 tab（radiogroup 缺失），"
                        "跳过模式切换，继续")

        # 2. 深度思考开关
        self._set_toggle("深度思考", target_think)
        # 3. 智能搜索开关（仅快速模式存在；专家模式下自动忽略）
        if cfg.get("mode") == "fast":
            self._set_toggle("智能搜索", target_search)
        return self

    # ---------------- 模式/开关 DOM 工具 ----------------

    def _radio_group_selected(self):
        """当前选中的大模式（radiogroup 里 aria-checked=true 的文本）。"""
        js = (
            "() => {"
            "  const group = document.querySelector('[role=radiogroup]');"
            "  if (!group) return null;"
            "  const sel = group.querySelector('[aria-checked=true]');"
            "  return sel ? sel.innerText.trim() : null;"
            "}"
        )
        try:
            return self._safe_evaluate(js) or None
        except Exception:
            return None

    def _click_text(self, label):
        """点击页面中指定文本对应的最像控件的祖先元素。"""
        js = (
            "async function() {"
            "  const all = Array.from(document.querySelectorAll("
            "      'div,button,span,li,label,a,[role=tab],[role=button]'));"
            "  const leaf = all.find(el =>"
            "      (el.textContent || '').includes(arguments[0]) &&"
            "      !Array.from(el.children).some(c =>"
            "          (c.textContent || '').includes(arguments[0])) &&"
            "      el.offsetParent !== null);"
            "  if (!leaf) return false;"
            "  let target = leaf;"
            "  let p = leaf.parentElement;"
            "  while (p) {"
            "    const t = p.tagName.toLowerCase();"
            "    if (t === 'button' || p.getAttribute('role') === 'tab'"
            "        || p.getAttribute('role') === 'radio'"
            "        || p.getAttribute('role') === 'switch'"
            "        || /toggle/.test(String(p.className))) {"
            "      target = p; break;"
            "    }"
            "    p = p.parentElement;"
            "  }"
            "  target.click();"
            "  return true;"
            "}"
        )
        try:
            return bool(self._safe_evaluate(js, label))
        except Exception:
            return False

    def _toggle_state(self, label):
        """读取开关状态：True=开 / False=关 / None=未找到。"""
        js = (
            "async function() {"
            "  const all = Array.from(document.querySelectorAll("
            "      'div,button,span,li,label,a'));"
            "  const leaf = all.find(el =>"
            "      (el.textContent || '').includes(arguments[0]) &&"
            "      !Array.from(el.children).some(c =>"
            "          (c.textContent || '').includes(arguments[0])) &&"
            "      el.offsetParent !== null);"
            "  if (!leaf) return null;"
            "  let p = leaf;"
            "  while (p) {"
            "    const cls = p.className ? String(p.className) : '';"
            "    if (cls.includes('ds-toggle-button')"
            "        && !cls.includes('__icon')) {"
            "      return cls.includes('--selected');"
            "    }"
            "    p = p.parentElement;"
            "  }"
            "  return null;"
            "}"
        )
        try:
            r = self._safe_evaluate(js, label)
            return bool(r) if r is not None else None
        except Exception:
            return None

    def _set_toggle(self, label, target):
        """把开关设置到目标状态；未找到或已达标则不动。"""
        current = self._toggle_state(label)
        if current is None:
            log.info("web_drivers: 未找到开关「%s」（可能已开启/改版），继续",
                     label)
            return
        if current == target:
            log.info("web_drivers: 开关「%s」已%s，不动",
                     label, "开启" if target else "关闭")
            return
        if self._click_text(label):
            log.info("web_drivers: 开关「%s」已%s", label,
                     "开启" if target else "关闭")
            self._page_instance().wait_for_timeout(600)
        else:
            log.warning("web_drivers: 开关「%s」点击失败", label)

    def input(self, prompt):
        """向输入框写入 prompt（textarea fill 纯文本，不需要剪贴板）。"""
        sel, _ = self._probe_selectors(_INPUT_SELECTORS, attr="tagName")
        if not sel:
            self._dump_page_state("找不到 DeepSeek 输入框")
        page = self._page_instance()
        try:
            page.locator(sel).fill(prompt)
        except Exception as exc:
            log.warning("web_drivers: 输入框 fill 失败：%s", exc)
            self._dump_page_state("输入框写入失败")
        log.info("web_drivers: prompt 已写入（%d 字符）", len(prompt))
        return self

    def send(self):
        """发送：优先 Enter（新版 DeepSeek 发送按钮输入前不渲染），
        输入后若有发送按钮再点击兜底。"""
        page = self._page_instance()
        # fill 已聚焦 textarea，Enter 即发送（DeepSeek 默认 Enter 发送）
        try:
            page.keyboard.press("Enter")
            log.info("web_drivers: 已按 Enter 发送")
            return self
        except Exception as exc:
            log.warning("web_drivers: Enter 发送失败：%s", exc)
        sel, _ = self._probe_selectors(_SEND_SELECTORS, attr="tagName")
        if sel:
            try:
                page.locator(sel).click()
                log.info("web_drivers: 已点击发送按钮（%s）", sel)
                return self
            except Exception as exc:
                log.warning("web_drivers: 发送按钮点击失败：%s", exc)
        self._dump_page_state("发送失败（Enter 与按钮均不可用）")
        return self

    def wait_complete(self, max_wait=None):
        """轮询等待生成完成：停止按钮消失 + 文本长度连续稳定。

        与 API 模式观感一致：心跳日志「生成中… 累计输出 N 字符」
        由 webui/log_capture 识别为进度条事件（前端零改动）。
        取消检查点每轮执行——Web 控制台「停止」按钮直接生效。
        """
        from applications.zhihu_story.browser_adapter import _check_cancel
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        max_wait = max_wait or cfg.get("max_wait", 600)
        poll_interval = cfg.get("poll_interval", 4)
        stable_count = cfg.get("stable_count", 2)

        deadline = time.time() + max_wait
        last_len = 0
        last_think = 0
        stable = 0
        stop_seen = False  # 停止按钮曾出现（生成中）→ 消失才算完成
        body_started = False  # 正文已出现 → 之后只打生成心跳（两阶段）
        start = time.time()
        while time.time() < deadline:
            _check_cancel()
            cur_len = self._current_reply_len()
            think_len = self._think_len()
            # 停止按钮只在生成中出现：探测到过且现在消失 → 完成。
            # 从未探测到（selector 改版等）→ 只用文本稳定判定，绝不误判完成。
            if self._stop_button_present():
                stop_seen = True
            elif stop_seen:
                log.info("web_drivers: 停止按钮已消失，生成完成（%.1fs，%d 字符）",
                         time.time() - start, cur_len)
                return True
            # 双阶段心跳：正文增长→生成中；正文未出现且思考增长→思考中。
            # 正文一旦出现（body_started）思考尾巴继续增长也不再打思考心跳，
            # 保持「先思考后生成」的两阶段观感；无深度思考时 think_len 恒 0。
            if cur_len != last_len:
                stable = 0
                last_len = cur_len
                if cur_len:
                    body_started = True
                    log.info("故事生成中… 已生成 %d 字", cur_len)
            elif not body_started and think_len != last_think:
                stable = 0
                last_think = think_len
                if think_len:
                    log.info("模型思考中… 已思考 %d 字符", think_len)
            else:
                stable += 1
            if stable >= stable_count and cur_len:
                log.info("web_drivers: 文本稳定 %d 轮，判定完成（%.1fs，%d 字符）",
                         stable_count, time.time() - start, cur_len)
                return True
            self._page_instance().wait_for_timeout(poll_interval * 1000)
        log.warning("web_drivers: 生成超时（%ds）", max_wait)
        return False

    def read_result(self):
        """读取最后一条助手回复全文（innerText）。"""
        sel, text = self._probe_selectors(_RESULT_SELECTORS, attr="innerText")
        if not sel or not text:
            self._dump_page_state("找不到回复内容（可能未登录或前端改版）")
        text = text.strip()
        if not text:
            self._dump_page_state("回复内容为空（可能未登录或生成失败）")
        return text

    # ---------------- 内部工具 ----------------

    def _stop_button_present(self):
        """停止按钮当前是否存在（生成中显示，完成即消失）。"""
        return bool(self._probe_selectors(_STOP_SELECTORS, attr="tagName")[0])

    def _current_reply_len(self):
        """当前正文长度（进度心跳用）。

        思考阶段正文容器（ds-assistant-message-main-content）尚未出现，
        ds-markdown 兜底选择器会误匹配到思考容器——主内容容器未命中
        且存在思考容器时视为思考中，正文长度返回 0。
        """
        sel, text = self._probe_selectors(_RESULT_SELECTORS, attr="innerText")
        if sel and "ds-assistant-message-main-content" not in sel \
                and self._think_exists():
            return 0
        return len(text or "")

    def _think_len(self):
        """当前思考容器文本长度（思考阶段心跳用，0 表示无思考/已结束）。"""
        sel, text = self._probe_selectors(_THINK_SELECTORS, attr="innerText")
        return len(text or "")

    def _think_exists(self):
        """思考容器是否存在（深度思考开启时思考中/结束后均存在）。"""
        return bool(self._probe_selectors(_THINK_SELECTORS, attr="tagName")[0])

    def _click_button_by_text(self, label):
        """按可见文本点击开关（模式开关用）。

        DeepSeek 新版 UI 开关是 ds-toggle-button（div 元素带可见文本），
        不是 <button> 标签——查找所有可见元素而非仅 button。
        """
        js = (
            "async function() {"
            "  const els = Array.from(document.querySelectorAll("
            "      'button, [role=button], .ds-toggle-button'));"
            "  const el = els.find(b => b.innerText.trim() === arguments[0]);"
            "  if (el) { el.click(); return true; }"
            "  return false;"
            "}"
        )
        try:
            return bool(self._safe_evaluate(js, label))
        except Exception:
            return False


# ---------------- --probe CLI ----------------
# 真实浏览器探测 chat.deepseek.com 的关键 selector，打印命中结果。
# 用法：python -m web_drivers.deepseek --probe
# （需 Edge 持久化 profile 已登录 DeepSeek，或先在页面手动登录）

def _probe():
    from applications.zhihu_story.browser_adapter import get_browser
    browser = get_browser()
    page = browser.context.new_page()
    url = "https://chat.deepseek.com/"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(3000)
    print(f"\n=== DeepSeek selector 探测: {page.title()} ===")
    groups = [
        ("输入框", _INPUT_SELECTORS),
        ("发送按钮", _SEND_SELECTORS),
        ("停止按钮", _STOP_SELECTORS),
        ("回复容器", _RESULT_SELECTORS),
    ]
    for name, candidates in groups:
        hit = None
        for s in candidates:
            try:
                if page.query_selector(s):
                    hit = s
                    break
            except Exception:
                pass
        print(f"  {name}: {hit or '（未命中）'}")
    print("\n  --- 页面文本片段 ---")
    try:
        body = page.evaluate("() => document.body.innerText.slice(0, 200)")
        print("  " + (body or "")[:200].replace("\n", " | "))
    except Exception:
        pass
    page.close()
    browser.close()


if __name__ == "__main__":
    _probe()
