# ============================================================
# web_drivers/doubao.py — 豆包网页版驱动（DOM 语义化）
#
# 目标站点：https://www.doubao.com/chat/
# 用户约定（2026-09）：网页端模式只用默认的「豆包快速」模型。
#
# ── 抓故事的完整路径（核心就这三件事）──────────────────────────
#   1. 读：_msg_state() 一次 evaluate 拿全部事实
#        · 正文 = [class*=md-box-root] 里「最后一条用户消息之后的第一条
#          助手消息」，逐块重建 markdown（<h2>N</h2> → ## **N**，否则
#          格式校验「章节 0 个」扣 4 分）
#        · card = 该消息之后是否出现 flow-product-card（正文被放进交付界面）
#        · generating = 发送按钮 #flow-end-msg-send 是否 disabled
#   2. 等：wait_complete() 正文长度稳定 + UI 空闲 → 完成
#        进度上报走日志行（webui/log_capture 解析成前端进度条）：
#        「故事生成中… 已生成 N 字」百分比、「任务进度：…」阶段提示
#   3. 兜底：正文过短且检测到交付卡片 → 同会话补问「直接输出全文正文」
#        （_recover_card_delivery，最多 2 次；JSON 类短回复直接跳过）
#
# ── 实测要点（2026-09，真实登录态 E2E）─────────────────────────
#   - 输入框 div[contenteditable='true']；发送 Enter 即可
#   - 生成期间发送按钮 disabled，此时新消息会被吞掉 → 发送前等 UI 空闲
#   - 会话 ID 在 URL：https://www.doubao.com/chat/{id}
#   - 豆包会在回复后追加「需要我帮你…吗？」追问建议条 → 读取时剥离
#   - 删除会话（2026-09-12 实测）：内部接口全是 401（网关拦截），可用链路
#     在侧栏 —— 会话项悬停 → ⋯ 按钮（同排另一个是「归档对话」）→ 菜单
#     [role=menuitem]「删除」→ 弹窗「确定删除对话?」→ 按钮「删除」。
#     操作按钮 hover 才显示，必须用 Playwright 真 hover（JS mouseover 无效）
#
# 生命周期与 DeepSeek 同构：open_session → setup → input → send →
# wait_complete → read_result（_after_wait_before_read 为站点钩子）。
# 会话纪律：单条完整链路只开一个会话（同会话续问）。
# ============================================================

import logging
import re
import time

from web_drivers.base import MARKDOWN_REBUILD_JS, WebLLMDriver

log = logging.getLogger(__name__)

# 输入框候选（豆包网页版各版本 textarea/contenteditable）
_INPUT_SELECTORS = (
    "textarea[placeholder]",
    "textarea",
    "div[contenteditable='true']",
    "[data-testid*='chat-input']",
    "[id*='chat-input']",
    "[class*='chat-input'] textarea",
)

# 发送按钮精确候选（点击兜底用）：[class*='send'] 会命中用户消息气泡、
# 隐藏的 send-btn-wrapper 等多个元素 → Playwright strict mode violation
# （2026-09-09 真实日志）。#flow-end-msg-send 是豆包输入区的发送按钮 id。
_SEND_BUTTON_SELECTORS = (
    "#flow-end-msg-send",
    "button[aria-label*='发送']",
    "div[role=button][aria-label*='发送']",
)

# 新对话按钮候选
_NEW_CHAT_SELECTORS = (
    "button[aria-label*='新对话']",
    "div[role=button][aria-label*='新对话']",
    "button[class*='new-chat']",
    "[title*='新对话']",
)

# 稳定判定后的重读验证窗口（毫秒）：LLM 流式输出可能中途停顿
_READBACK_MS = 3000

# ---- 卡片式交付检测（2026-09-08 真实会话 DOM 实测）----
# 豆包对长写作请求会走「写作/文档/工作任务交付」：正文进独立交付界面，
# 对话里只留一句引言 + 一张产品卡片（标题 + 「创建时间：MM-DD HH:MM」）。
# 实测对比（同一账号历史会话）：
#   - 卡片交付会话：flow-product-card 命中 1，对话正文 86-91 字
#   - 内联输出会话（8022 字全文）：flow-product-card 命中 0
# 因此用卡片容器作为「正文不在对话里」的判据，比原先的正文文案正则可靠。
_CARD_SELECTORS = (
    "[class*='flow-product-card']",
    "[class*='product-card']",
    "[class*='card-snapshot']",
)

# 「已拿到正文」的最小字数：低于此值且检测到交付卡片 → 走补问兜底
_INLINE_MIN_CHARS = 300

# 补问前等卡片渲染的窗口（秒）：卡片随助手消息一起渲染，正常立刻可见，
# 这里只留一点渲染余量；结构化回复（JSON）不会走卡片交付，直接跳过等待
_CARD_WAIT_SEC = 15

# 发送后确认「消息真的被接收」的等待窗口（秒）
_SEND_ACCEPT_SEC = 25

# 等「上一轮生成结束、输入区可发送」的窗口（秒）：豆包生成期间发送按钮
# #flow-end-msg-send 处于 disabled 状态，此时新消息发不出去
_SEND_READY_SEC = 240

# 消息状态探针：区分用户消息 / 助手消息，定位「最后一条用户消息之后的
# 第一条助手消息」（= 本次提问的回复）并重建 markdown；同时给出交付卡片
# 与「是否仍在生成」状态。
#   用户消息：md-box-root 的祖先链上带 send-msg（右对齐发送气泡）
#   助手消息：祖先链无 send-msg（左对齐回复）
#   ★ 正文用「逐块重建」而不是 innerText：豆包把 markdown 渲染成 DOM
#     （章节标题 ## **N** → <h2>N</h2>），innerText 会丢掉 "## **" 语法，
#     格式校验「章节 0 个」扣 4 分（2026-09-09 真实全链路：3/10 判废）。
#     逐块重建把 h1-h6 还原成 ## **N**，同一篇实测 10/10 通过。
#     2026-09-19：实现抽到 base.MARKDOWN_REBUILD_JS，DeepSeek 通道共用。
#   ★ generating：发送按钮 disabled = 上一轮仍在生成（实测：生成中 disabled，
#     空闲时无论输入框空否都是 enabled）。文本「稳定」不等于生成结束，
#     生成未结束时发下一条会被吞掉（2026-09-09 重试失败根因）。
_MSG_STATE_JS = (
    "(cardSel) => {"
    "  const isUser = e => {"
    "    let p = e;"
    "    for (let i = 0; i < 6 && p; i++) {"
    "      const cls = String(p.className || '');"
    "      if (cls.indexOf('send-msg') >= 0) return true;"
    "      p = p.parentElement;"
    "    }"
    "    return false;"
    "  };"
    "  const nodes = Array.from(document.querySelectorAll("
    "      '[class*=md-box-root]'))"
    "      .filter(e => (e.textContent || '').trim().length > 0);"
    "  let lastUser = null;"
    "  for (const e of nodes) if (isUser(e)) lastUser = e;"
    "  const after = lastUser"
    "      ? nodes.slice(nodes.indexOf(lastUser) + 1) : nodes;"
    "  const answers = after.filter(e => !isUser(e));"
    "  let card = false;"
    "  for (const s of cardSel) {"
    "    for (const c of document.querySelectorAll(s)) {"
    "      if (!lastUser || (lastUser.compareDocumentPosition(c)"
    "          & Node.DOCUMENT_POSITION_FOLLOWING)) { card = true; break; }"
    "    }"
    "    if (card) break;"
    "  }"
    + MARKDOWN_REBUILD_JS +
    "  let text = '';"
    "  if (answers.length) {"
    "    const el = answers[0];"
    "    text = toMarkdown(el)"
    "        || (el.innerText || el.textContent || '').trim();"
    "  }"
    "  const btn = document.querySelector('#flow-end-msg-send');"
    "  const inp = document.querySelector(\"div[contenteditable='true']\");"
    "  return {user_count: nodes.filter(isUser).length,"
    "          answer_count: answers.length,"
    "          text: text,"
    "          card: card,"
    "          generating: btn ? !!btn.disabled : false,"
    "          input_len: inp ? (inp.innerText || '').length : -1};"
    "}"
)

# 豆包回复末尾的「追问建议」条（如「需要我帮你微调每章甜度节奏吗？」）：
# 2026-09-08 实测它有时是独立一条消息，有时被塞进正文那条消息的末尾——
# 独立消息由「取第一条助手消息」自然排除，塞进正文的靠这里剥掉。
_TRAILING_SUGGEST_RE = re.compile(
    r"^(?:需要我帮你|要我帮你|要不要|是否需要|需不需要|想不想|我可以帮你"
    r"|要不要我|还能帮你).{0,80}[？?]$"
)


def _looks_structured(text):
    """回复是否像 JSON/数组（选题筛选、评分、原创审核等结构化请求）。

    这类请求本来就该短，且豆包不会把它们放进写作交付界面——直接跳过
    卡片兜底，避免给它们平白加等待。"""
    s = (text or "").lstrip()
    return s.startswith(("{", "["))


def _strip_trailing_suggestion(text):
    """剥掉正文末尾的豆包追问建议条（只从末尾连续剥，正文内部不动）。"""
    lines = (text or "").split("\n")
    while lines:
        last = lines[-1].strip()
        if not last or _TRAILING_SUGGEST_RE.match(last):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()

# 正文末尾若停在章节标题上，说明只抓到半截（2026-09-19 真实事故：豆包一轮
# 3 次尝试都被「文本稳定 2 轮且 UI 空闲」判完成，存盘 597 字——引言 + 第 1 章
# 正文 + ## **2**…## **6** 五个空壳标题，格式校验 5/10 判废）。
# 只认无歧义的章节标题形态（## **N** / 第N章）：裸数字行可能是正文里的普通
# 数字，误判会让好稿一直等到超时，宁可漏判不可误杀。
_TAIL_HEADING_RE = re.compile(
    r'^(?:##\s*\*\*\d+\*\*'
    r'|#{1,6}\s*第\s*[0-9一二三四五六七八九十百千万零两]+\s*[章节回]'
    r'|第\s*[0-9一二三四五六七八九十百千万零两]+\s*[章节回])\s*$'
)


def reply_tail_is_chapter_heading(text):
    """回复最后一个非空行是否只是章节标题（= 正文还没写完）。

    真故事的最后一行必然是正文句子或【ps】收尾，不会停在章节标题上；
    停在标题上只有一个解释：读的时候模型还没写完（或页面只渲染了半截）。
    """
    for line in reversed((text or "").split("\n")):
        s = line.strip()
        if s:
            return bool(_TAIL_HEADING_RE.match(s))
    return False


# 卡片式交付时补问「直接输出全文」的指令（2026-09-08 实测有效措辞：
# 明确「不要卡片/文档/任务交付界面」后，豆包把 8022 字全文直接输出到对话）
_INLINE_ASK_PROMPT = (
    "请把刚才那篇内容的完整正文直接输出到当前对话里："
    "直接输出全文正文，不要使用卡片 / 文档 / 任务交付界面，"
    "不要任何前后缀说明，一次写完，不要分多次输出。"
)


class DoubaoDriver(WebLLMDriver):
    """豆包网页版（www.doubao.com/chat/）DOM 驱动。

    与 DeepSeek 同构生命周期；setup() 负责把模型选择到「豆包快速」。
    selectors 目前是候选值，务必先跑 --probe 实测后校准。
    """

    def __init__(self, config):
        super().__init__(config)
        # 读取前兜底是否已执行（串行链路在 _after_wait_before_read 做，
        # 并行链路直接调 read_result → 由 read_result 做，避免重复补问）
        self._recovery_ran = False

    def new_chat(self):
        """重置为全新对话：导航 + 显式点「新对话」+ 等待输入框。"""
        self._reset_session_state()
        self.open_session()
        if self._click_new_chat_button():
            log.info("web_drivers[doubao]: 已点击「新对话」")
            self._page_instance().wait_for_timeout(1200)
        for _ in range(12):
            if self._probe_selectors(_INPUT_SELECTORS, attr="tagName")[0]:
                self._session_id = self._detect_session_id()
                return self
            self._page_instance().wait_for_timeout(500)
        log.warning("web_drivers[doubao]: new_chat 后未等到输入框渲染，"
                    "交给 input 兜底")
        return self

    def _click_new_chat_button(self):
        """尽力点「新对话」：选区器 → 文本匹配 → 快捷键 Ctrl+Shift+K。"""
        js = (
            "async function() {"
            "  const sels = arguments[0];"
            "  for (const s of sels) {"
            "    const el = document.querySelector(s);"
            "    if (el && el.offsetParent !== null) { el.click(); return true; }"
            "  }"
            "  const all = Array.from(document.querySelectorAll("
            "      'button,div,a,[role=button],[role=tab],[role=menuitem]'));"
            "  const leaf = all.find(el =>"
            "      /新对话|新建聊天|New Chat/.test(el.textContent || '') &&"
            "      !Array.from(el.children).some(c =>"
            "          /新对话|新建聊天|New Chat/.test(c.textContent || '')) &&"
            "      el.offsetParent !== null);"
            "  if (leaf) { leaf.click(); return true; }"
            "  return false;"
            "}"
        )
        try:
            if self._safe_evaluate(js, list(_NEW_CHAT_SELECTORS)):
                return True
        except Exception:
            pass
        # 实测页面「新对话 ⏎ Ctrl Shift K」：快捷键兜底
        try:
            self._page_instance().keyboard.press("Control+Shift+K")
            return True
        except Exception:
            return False

    def setup(self):
        """确认模型为「豆包快速」（用户约定：只用这一个，默认即是）。

        正常情况什么都不做（豆包默认就是快速）；只有明确探到「未选中」
        才点一下。日志走 DEBUG——每条链路都会调它，不值得占终端一行。
        """
        candidates = ("豆包 快速", "豆包快速", "快速")
        for label in candidates:
            if self._model_selected(label) is True:
                log.debug("web_drivers[doubao]: 模型已是「%s」", label)
                return self
        for label in candidates:
            if self._click_text(label):
                log.info("web_drivers[doubao]: 已切换模型到「%s」", label)
                self._page_instance().wait_for_timeout(800)
                return self
        log.warning("web_drivers[doubao]: 未找到「豆包快速」模型选项"
                    "（前端改版？可 --probe 校准）")
        return self

    def _model_selected(self, label):
        """读当前模型选中状态：True=已选中 / False=未选中 / None=未探测。"""
        js = (
            "async function() {"
            "  const all = Array.from(document.querySelectorAll("
            "      'div,button,span,li,[role=tab],[role=radio]'));"
            "  const leaf = all.find(el =>"
            "      (el.textContent || '').includes(arguments[0]) &&"
            "      !Array.from(el.children).some(c =>"
            "          (c.textContent || '').includes(arguments[0])) &&"
            "      el.offsetParent !== null);"
            "  if (!leaf) return null;"
            "  let p = leaf;"
            "  while (p) {"
            "    const cls = p.className ? String(p.className) : '';"
            "    if (cls.includes('selected') || cls.includes('active')"
            "        || (p.getAttribute && (p.getAttribute('aria-selected') === 'true'"
            "        || p.getAttribute('aria-checked') === 'true'))) {"
            "      return true;"
            "    }"
            "    p = p.parentElement;"
            "  }"
            "  return false;"
            "}"
        )
        try:
            r = self._safe_evaluate(js, label)
            return bool(r) if r is not None else None
        except Exception:
            return None

    def _click_text(self, label):
        """点击页面中包含指定文本的最像控件的元素（豆包 selectors 校准用）。"""
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
            "        || /toggle|select|model/.test(String(p.className))) {"
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

    def input(self, prompt):
        """向输入框写入 prompt（fill 纯文本）。"""
        sel, _ = self._probe_selectors(_INPUT_SELECTORS, attr="tagName")
        if not sel:
            self._dump_page_state("找不到豆包输入框（未登录或前端改版）")
        page = self._page_instance()
        try:
            page.locator(sel).fill(prompt, timeout=120000)
        except Exception:
            self._dump_page_state("豆包输入框写入失败")
        log.info("web_drivers[doubao]: prompt 已写入（%d 字符）", len(prompt))
        return self

    def send(self, accept_timeout=None):
        """发送：先等 UI 空闲 → Enter → 确认接收（兜底点精确发送按钮）。

        ★ 2026-09-08/09 真实日志两个坑：
        1) 上一轮写作任务仍在跑时豆包会吞掉新输入——重试的 prompt 根本没
           发出去，随后 read_result 读回上一条旧回复（3 次重试都读到同一段
           91 字引言）。故发送前先等「发送按钮可用」（生成期间它 disabled）。
        2) 只数用户消息会被长对话虚拟化/渲染延迟骗过——改为双判据：用户
           消息条数增加 **或** 输入框从有内容变空。
        仍未接收则点精确发送按钮兜底；再失败标记会话损坏并抛错，让调用方
        重开新会话，而不是把旧回复当成本次结果。
        """
        accept_timeout = accept_timeout or _SEND_ACCEPT_SEC
        # ★ 先等上一轮生成结束：生成期间豆包会吞掉新消息（发送按钮 disabled）
        self._wait_send_ready()
        st0 = self._msg_state()
        before = int(st0.get("user_count", 0))
        before_input = int(st0.get("input_len", -1))
        page = self._page_instance()
        try:
            page.keyboard.press("Enter")
            log.info("web_drivers[doubao]: 已按 Enter 发送")
        except Exception as exc:
            log.warning("web_drivers[doubao]: Enter 发送失败：%s", exc)
        if self._wait_sent(before, accept_timeout, before_input):
            return self
        # 兜底点发送按钮：用精确 selector（[class*='send'] 会命中消息气泡等
        # 多个元素 → strict mode violation，2026-09-09 真实日志）
        for sel in _SEND_BUTTON_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                loc.first.click()
                log.info("web_drivers[doubao]: 已点击发送按钮（%s）", sel)
                break
            except Exception as exc:
                log.warning("web_drivers[doubao]: 发送按钮点击失败（%s）：%s",
                            sel, exc)
        if self._wait_sent(before, accept_timeout, before_input):
            return self
        self._mark_session_broken()
        raise RuntimeError(
            "豆包未接收本次消息（发送后用户消息条数未增加、输入框未清空，"
            f"等待 {accept_timeout}s）——上一轮生成可能仍在进行，"
            "已标记会话损坏，请重开新会话重试")

    def _wait_sent(self, before_count, timeout, before_input_len=-1):
        """确认消息已被豆包接收：用户消息条数增加，或输入框从有内容变空。

        双判据（2026-09-09）：只数用户消息会被长对话虚拟化/DOM 渲染延迟
        骗过；输入框被清空是「已提交」的直接信号。两者任一成立即算接收。
        """
        deadline = time.time() + max(float(timeout), 1.0)
        while True:
            st = self._msg_state()
            try:
                now = int(st.get("user_count", 0))
            except Exception:
                now = before_count
            cleared = (before_input_len > 0
                       and st.get("input_len") == 0)
            if now > before_count or cleared:
                return True
            if time.time() >= deadline:
                return False
            try:
                self._page_instance().wait_for_timeout(1000)
            except Exception:
                return False

    def wait_complete(self, max_wait=None):
        """轮询等待生成完成：正文长度稳定 + UI 空闲（发送按钮恢复可用）。

        完成判据（四条同时满足）：正文长度连续 stable_count 轮不变、
        read-back 重读仍不变、豆包 UI 空闲（生成期间发送按钮 disabled）、
        正文末尾不停在章节标题上（reply_tail_is_chapter_heading）。
        第三条是 2026-09-09 补上的关键判据——文本稳定不等于生成结束；
        第四条是 2026-09-19 补的残稿判据——那天的真实事故里前三条都成立，
        但读到的是「引言 + 第 1 章正文 + 5 个空壳标题」的 597 字半截稿。

        进度上报（与 DeepSeek 同构，webui/log_capture 解析成前端进度条）：
        - 「故事生成中… 已生成 N 字」→ 百分比进度条
        - 「任务进度：…」→ 阶段提示（等待响应 / 写作任务交付）
        """
        from web_drivers.browser_pool import _check_cancel
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        cfg = WEB_DRIVERS.get(WEB_DRIVER_NAME, {})
        max_wait = max_wait or cfg.get("max_wait", 600)
        poll_interval = cfg.get("poll_interval", 4)
        stable_count = cfg.get("stable_count", 2)

        deadline = time.time() + max_wait
        last_len = 0
        stable = 0
        start = time.time()
        wait_logged = False      # 「等待响应」阶段提示只打一次
        card_logged = False      # 「写作任务交付」阶段提示只打一次
        busy_logged = False      # 「仍在收尾」只提示一次，其余降 DEBUG
        tail_logged = False      # 「末尾停在章节标题」只提示一次
        tail_blocked = False     # 是否因残稿判据没敢判完成
        while time.time() < deadline:
            _check_cancel()
            cur_len = len(self._extract_reply_text())
            if cur_len != last_len:
                stable = 0
                last_len = cur_len
                if cur_len:
                    # 与 DeepSeek 同一文案：log_capture._PROGRESS_RE 据此
                    # 推前端进度条（旧文案带「豆包」前缀不匹配 → 无进度条）
                    log.info("故事生成中… 已生成 %d 字", cur_len)
            else:
                stable += 1
                # 正文还没出来（< 兜底阈值）时给阶段提示：交付卡片优先，
                # 否则是「等待响应」。有卡片意味着豆包在跑写作任务，可能
                # 好几分钟——不能只让进度条停在 1% 不动。
                if cur_len < _INLINE_MIN_CHARS:
                    if not card_logged and self._delivery_card_present():
                        card_logged = True
                        log.info("任务进度：豆包正在用写作任务撰写正文…")
                    elif not wait_logged:
                        wait_logged = True
                        log.info("任务进度：等待豆包响应…")
            if stable >= stable_count and cur_len:
                page = self._page_instance()
                page.wait_for_timeout(_READBACK_MS)
                re_text = self._extract_reply_text()
                re_len = len(re_text)
                if re_len != cur_len:
                    stable = 0
                    last_len = re_len
                    continue
                if self._generating():
                    if not busy_logged:
                        busy_logged = True
                        log.info("web_drivers[doubao]: 正文已稳定，等豆包收尾…")
                    else:
                        log.debug("web_drivers[doubao]: 仍在生成中，继续等待")
                    stable = 0
                    continue
                # 残稿判据：末尾停在章节标题 = 只抓到半截（2026-09-19 事故），
                # 不判完成、继续等；等到超时走「未完成」分支交给重试，
                # 绝不把半截稿当成品交给下游（不然就是 597 字废稿）
                if reply_tail_is_chapter_heading(re_text):
                    tail_blocked = True
                    if not tail_logged:
                        tail_logged = True
                        log.warning("web_drivers[doubao]: 正文末尾仍停在章节标题"
                                    "（当前 %d 字，疑似只读到半截），继续等待…",
                                    re_len)
                    stable = 0
                    continue
                log.info("web_drivers[doubao]: 文本稳定 %d 轮且 UI 空闲，"
                         "判定完成（%.1fs，%d 字符）", stable_count,
                         time.time() - start, re_len)
                return True
            # 卡片式交付：对话里没有正文（只有交付卡片），正文永远不会再
            # 增长——不必空等到 max_wait，直接交给读取前兜底补问
            if stable >= stable_count and not cur_len \
                    and self._delivery_card_present():
                log.info("web_drivers[doubao]: 对话无正文且检测到交付卡片，"
                         "交给读取前兜底（%.1fs）", time.time() - start)
                return True
            self._page_instance().wait_for_timeout(poll_interval * 1000)
        if tail_blocked:
            log.warning("web_drivers[doubao]: 生成超时（%ds）——末次正文（%d 字）"
                        "末尾仍停在章节标题，判定为半截稿，不当作完成",
                        max_wait, last_len)
        else:
            log.warning("web_drivers[doubao]: 生成超时（%ds）", max_wait)
        return False

    def _msg_state(self):
        """页面消息状态：{user_count, answer_count, text, card}。

        一次 evaluate 同时拿到「本次提问的回复正文」与「是否出现交付卡片」。
        探测失败返回 {}（调用方按空值兜底，不阻塞流程）。
        """
        try:
            r = self._safe_evaluate(_MSG_STATE_JS, list(_CARD_SELECTORS))
        except Exception:
            r = None
        return r if isinstance(r, dict) else {}

    def _delivery_card_present(self):
        """本次提问之后是否出现「卡片式交付」容器（正文被放进交付界面）。"""
        return bool(self._msg_state().get("card"))

    def _generating(self):
        """豆包是否仍在生成（发送按钮 disabled）。

        实测 2026-09-09：生成期间 #flow-end-msg-send 为 disabled，空闲时
        无论输入框空否都 enabled——是比「文本长度稳定」可靠的完成判据。
        按钮不存在（前端改版/非对话页）时返回 False，退回文本稳定判定。
        """
        return bool(self._msg_state().get("generating"))

    def _wait_send_ready(self, timeout=_SEND_READY_SEC):
        """等上一轮生成结束、输入区可发送（豆包生成期间会吞掉新消息）。"""
        deadline = time.time() + max(float(timeout), 1.0)
        if self._generating():
            log.info("任务进度：等豆包完成上一轮生成后继续…")
        while self._generating():
            if time.time() >= deadline:
                log.warning("web_drivers[doubao]: 等待上一轮生成结束超时"
                            "（%ds），仍尝试发送", timeout)
                return False
            try:
                self._page_instance().wait_for_timeout(2000)
            except Exception:
                return False
        return True

    def _after_wait_before_read(self):
        """读取前兜底：卡片式交付时要求把全文直接输出到对话。"""
        self._recover_card_delivery()
        self._recovery_ran = True

    def _recover_card_delivery(self):
        """卡片式交付兜底：同会话内补问「直接输出全文正文」（最多 2 次）。

        ★ 2026-09-08 真实日志根因：豆包对长写作请求走「写作/文档交付」，
        对话里只剩一句引言（86-91 字），正文在独立交付界面（不在对话 DOM
        里）→ 故事被判「过短」连废 3 次。此处读到短文本且检测到交付卡片
        时，在同一会话补问一次「直接输出全文正文」，把正文拉回对话再读。

        幂等：已拿到正文（≥ _INLINE_MIN_CHARS）、结构化回复（选题筛选/评分/
        原创审核等 JSON，本来就该短）、或没有交付卡片时立即返回——不让
        兜底给正常短回复增加等待。
        """
        try:
            for attempt in range(2):
                text = self._extract_reply_text()
                if len(text) >= _INLINE_MIN_CHARS:
                    return  # 正文已在对话里
                if _looks_structured(text):
                    return  # JSON/数组等结构化回复，不会走卡片交付
                if not self._wait_for_delivery_card():
                    return  # 没有交付卡片：正常短回复，不折腾
                log.info("web_drivers[doubao]: 检测到卡片式交付"
                         "（对话仅 %d 字），第 %d/2 次要求直接输出全文",
                         len(text), attempt + 1)
                log.info("任务进度：正文在交付卡片里，正在要求豆包直接输出全文…")
                if not self._ask_inline():
                    log.warning("web_drivers[doubao]: 补问未被接收，"
                                "放弃卡片兜底")
                    return
                self.wait_complete(max_wait=self.config.get("max_wait"))
            final = self._extract_reply_text()
            if len(final) < _INLINE_MIN_CHARS:
                log.warning("web_drivers[doubao]: 卡片兜底后对话仍只有 %d 字"
                            "（正文可能仍在交付界面）", len(final))
        except Exception as exc:
            log.warning("web_drivers[doubao]: 卡片兜底补问失败：%s", exc)

    def _wait_for_delivery_card(self, timeout=_CARD_WAIT_SEC):
        """等交付卡片出现（卡片晚于对话文本稳定，异步渲染）。"""
        deadline = time.time() + max(float(timeout), 0.0)
        while True:
            if self._delivery_card_present():
                return True
            if time.time() >= deadline:
                return False
            try:
                self._page_instance().wait_for_timeout(3000)
            except Exception:
                return False

    def _ask_inline(self, timeout=180):
        """补问「直接输出全文正文」，返回消息是否真的被接收。"""
        self.input(_INLINE_ASK_PROMPT)
        try:
            self.send(accept_timeout=timeout)
            return True
        except Exception as exc:
            log.warning("web_drivers[doubao]: 补问发送失败：%s", exc)
            return False

    def read_result(self):
        """读取本次提问的助手回复全文。

        ★ 卡片式交付兜底（幂等）：串行链路里 base.generate 已调用
        _after_wait_before_read 完成兜底，此处直接短路；并行链路
        （web_drivers/parallel.py 直接调 read_result）靠这里兜底——
        因此本方法可能阻塞数十秒（等卡片 + 补问 + 等生成），
        只在确实检测到交付卡片时才会发生。
        """
        if not getattr(self, "_recovery_ran", False):
            self._recover_card_delivery()
        self._recovery_ran = False
        text = self._extract_reply_text()
        if not text:
            self._dump_page_state("找不到豆包回复内容（未登录/生成失败/改版）")
        return text

    def _extract_reply_text(self):
        """取「最后一条用户消息之后的第一条助手消息」正文。

        实测 2026-09-08（真实会话 DOM）：每条消息正文放在 [class*=md-box-root]
        里，用户消息与助手消息同结构；用户消息的祖先链带 send-msg（右对齐
        发送气泡），助手消息没有。
        ★ 不能取「最后一个 md-box-root」：豆包回复结束后还会追加一条建议追问
        条（同样是 md-box，如「需要我帮你微调每章字数吗？」），取最后一个会
        把建议条当成正文；发送后助手未开始输出时，最后一个 md-box 还是用户
        自己刚发的 prompt（曾被读成「已生成 10058 字」）。

        正文取 innerText 而非 textContent：豆包每段是一个 block 元素，
        textContent 会把整篇故事压成一行（段落/章节行全丢，2026-09-08
        E2E 实测 6305 字挤成一行），innerText 保留换行。
        """
        text = str(self._msg_state().get("text", "") or "").strip()
        return _strip_trailing_suggestion(text)

    def _detect_session_id(self):
        """豆包会话 ID：URL 路径 /chat/{id}（实测格式）。"""
        import re
        try:
            page = self._page_instance()
            url = page.url or ""
            m = re.search(r"/chat/(\d{6,})", url)
            if m:
                return m.group(1)
            # 新建会话的初始 URL 是 /chat/local_<id>（服务端 ID 还没分配）：
            # 本地 ID 在侧栏里匹配不到（侧栏 href 用服务端 ID），
            # 返回 None 让调用方改用「侧栏当前打开项」兜底
            if re.search(r"/chat/local_", url):
                return None
            m = re.search(r"[?&](?:id|session_id|conversation_id)=([0-9a-zA-Z_-]{6,})", url)
            if m:
                return m.group(1)
            m = re.search(r"/chat/?([0-9a-zA-Z_-]{16,})", url)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    # 豆包删除：内部接口实测全是 401（网关拦截），真正的删除入口在侧栏
    # 会话项的 hover 菜单里（2026-09-12 实测）：
    #   侧栏 a[class*=conversation-item] 悬停 → ⋯ 按钮（无 aria-label，
    #   同排另一个带 aria-label="归档对话"）→ 菜单 [role=menuitem]「删除」
    #   → 弹窗「确定删除对话?」→ 按钮「删除」
    # ★ 操作按钮是 hover 才显示的（tailwind hidden group-hover:block），
    #   必须用 Playwright 真 hover——JS dispatchEvent('mouseover') 不触发。
    def _delete_current_session_impl(self):
        """先试内部接口（历史实测 401，保留以防站点恢复），再走侧栏 DOM。"""
        sid = self._session_id
        if not sid or str(sid).startswith("local_"):
            sid = self._detect_session_id()
            self._session_id = sid
        if sid and self._delete_session_via_api():
            return True
        return self._delete_session_via_dom()

    def _click_js(self, js):
        """执行一段返回 bool 的页面 JS（异常按 False 处理）。"""
        try:
            return bool(self._safe_evaluate(js))
        except Exception:
            return False

    def _delete_session_via_dom(self):
        """侧栏会话项 → ⋯ → 菜单「删除」→ 确认弹窗「删除」。"""
        sid = self._session_id
        title = self._session_title
        if not sid and not title:
            return False
        page = self._page_instance()
        try:
            # 侧栏会话列表是异步渲染的（刚 open_session 完可能还是空的）：
            # 先等第一批会话项出现，最多 8s
            for _ in range(16):
                if page.locator("[class*='conversation-item']").count():
                    break
                page.wait_for_timeout(500)
            if sid:
                items = page.locator(
                    f"[class*='conversation-item'][href*='{sid}']")
            else:
                items = page.locator("[class*='conversation-item']",
                                     has_text=title)
            if not items.count():
                # 兜底 1：侧栏当前打开项（aria-current=page）——驱动刚在用
                items = page.locator(
                    "[class*='conversation-item'][aria-current='page']")
            if not items.count() and title:
                # 兜底 2：按首条消息标题文本匹配
                items = page.locator("[class*='conversation-item']",
                                     has_text=title)
            if not items.count():
                log.info("web_drivers[doubao]: 侧栏未找到本次会话项，跳过 DOM 删除")
                return False
            item = items.first
            item.scroll_into_view_if_needed(timeout=5000)
            item.hover(timeout=5000)          # 真 hover 才会露出操作按钮
            page.wait_for_timeout(500)
            more = None
            btns = item.locator("button")
            for i in range(btns.count()):
                aria = btns.nth(i).get_attribute("aria-label") or ""
                if aria != "归档对话":        # 另一个按钮是「⋯」
                    more = btns.nth(i)
                    break
            if more is None:
                log.info("web_drivers[doubao]: 未找到会话项「⋯」按钮")
                return False
            more.click(timeout=5000)
            page.wait_for_timeout(800)
            if not self._click_js(
                    "() => {"
                    "  const el = Array.from(document.querySelectorAll("
                    "      '[role=menuitem]')).find(e =>"
                    "      (e.innerText || '').trim() === '删除');"
                    "  if (!el) return false; el.click(); return true;"
                    "}"):
                log.info("web_drivers[doubao]: 会话菜单里没有「删除」项")
                return False
            page.wait_for_timeout(1000)
            if not self._click_js(
                    "() => {"
                    "  const btns = Array.from(document.querySelectorAll("
                    "      'button,[role=button]')).filter(e =>"
                    "      e.offsetParent !== null &&"
                    "      /^(删除|删除对话)$/.test((e.innerText || '').trim()));"
                    "  if (!btns.length) return false;"
                    "  btns[btns.length - 1].click();"
                    "  return true;"
                    "}"):
                log.info("web_drivers[doubao]: 删除确认弹窗未命中")
                return False
            page.wait_for_timeout(2000)
            log.info("web_drivers[doubao]: 已在侧栏删除本次会话%s",
                     (f"（id={sid}）" if sid else ""))
            return True
        except Exception as exc:
            log.info("web_drivers[doubao]: DOM 删除未完成：%s", exc)
            return False


    # 豆包删除接口候选（会话 ID 直连，带同一登录态；未命中不报错）。
    # ★ 实测 2026-09 全部 401（网关拦截）：真正可用的是上面的侧栏 DOM 链路，
    #   这里保留两个最可能的端点做尽力而为（万一站点恢复接口），日志降 DEBUG。
    _DELETE_API_ENDPOINTS = (
        "/api/v1/doubao/conversation/delete",
        "/api/chat/conversation/delete",
    )

    def _delete_session_via_api(self):
        """用当前页面同源登录态尝试删除会话接口（候选端点，日志降 DEBUG）。"""
        from urllib.parse import urlsplit
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        try:
            site_url = WEB_DRIVERS[WEB_DRIVER_NAME]["url"]
        except Exception:
            site_url = "https://www.doubao.com/chat/"
        sp = urlsplit(site_url)
        if not sp.netloc:
            return False
        origin = f"{sp.scheme}://{sp.netloc}"
        page = self._page_instance()
        for path in self._DELETE_API_ENDPOINTS:
            import json as _json
            body = _json.dumps(
                {"conversation_id": self._session_id,
                 "chat_session_id": self._session_id},
                ensure_ascii=False)
            try:
                resp = page.request.post(
                    origin + path, data=body,
                    headers={"Content-Type": "application/json"})
                log.debug("web_drivers[doubao]: 删除接口 %s → HTTP %d",
                          path, resp.status)
                if resp.ok:
                    return True
            except Exception as exc:
                log.debug("web_drivers[doubao]: 删除接口 %s 失败：%s",
                          path, exc)
        return False

# ---------------- 豆包登录判定 / 登录引导 ----------------
# 与 DeepSeek 同构：独立实例 + 锁内独占 profile；cookie 域 doubao.com

_DOUBAO_COOKIE_DOMAINS = ("doubao.com",)

# 字节跳动 passport 登录态 cookie 名（登录后才会出现；匿名/跟踪 cookie
# 如 ttwid/s_v_web_id/passport_csrf_token 不能作为登录判定——2026-09 实测
# 未登录访问 /chat/ 就会种下这些，误判导致登录引导秒过不弹窗）
_DOUBAO_AUTH_COOKIE_NAMES = (
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_token",
    "passport_auth",
)


def _has_doubao_cookies(context):
    """是否具备豆包真实登录态（仅认 passport 登录 cookie）。"""
    try:
        cookies = context.cookies()
    except Exception:
        return False
    names = {c.get("name") for c in cookies
             if c.get("domain") and c.get("value")
             and any(d in (c["domain"] or "") for d in _DOUBAO_COOKIE_DOMAINS)}
    return bool(names & set(_DOUBAO_AUTH_COOKIE_NAMES))


def web_llm_logged_in():
    """豆包网页版是否真实可登录：cookie + 加载未停在登录页。"""
    from web_drivers.browser_pool import _browser_lock, create_browser
    site_url = "https://www.doubao.com/chat/"
    try:
        with _browser_lock:
            with create_browser(headless=True) as browser:
                if not _has_doubao_cookies(browser.context):
                    return False
                page = browser.context.new_page()
                try:
                    page.goto(site_url, wait_until="domcontentloaded",
                              timeout=20000)
                    deadline = time.time() + 3.0
                    prev = page.url
                    while time.time() < deadline:
                        page.wait_for_timeout(250)
                        cur = page.url
                        if any(k in cur.lower() for k in
                               ("login", "signin", "passport")):
                            return False
                        if cur == prev:
                            # URL 稳定且带登录域 cookie → 视为已登录
                            return _has_doubao_cookies(browser.context)
                        prev = cur
                    return _has_doubao_cookies(browser.context)
                finally:
                    page.close()
    except Exception:
        return False


def login_web_flow(timeout=300):
    """打开可见 Edge 到豆包网页版，等待用户登录（供引导窗口调用）。

    登录完成后写回持久化 profile；web_llm_logged_in() 判定为 True。"""
    from web_drivers.browser_pool import _browser_lock, create_browser
    site_url = "https://www.doubao.com/chat/"
    try:
        with _browser_lock:
            with create_browser(headless=False) as browser:
                page = browser.context.new_page()
                try:
                    page.goto(site_url, wait_until="domcontentloaded",
                              timeout=30000)
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        time.sleep(3)
                        page.wait_for_timeout(500)
                        if (_has_doubao_cookies(browser.context)
                                and "login" not in page.url.lower()
                                and "passport" not in page.url.lower()):
                            return True, "检测到豆包登录成功"
                    return False, f"超时（{timeout // 60} 分钟）未检测到登录"
                finally:
                    page.close()
    except Exception as exc:
        return False, f"豆包登录引导失败：{exc}"


# ---------------- --probe CLI ----------------
# 真实浏览器探测豆包页面关键 selector（需 Edge 持久化 profile 已登录）。

def _probe():
    import applications.zhihu_story.browser_adapter  # noqa: F401
    from web_drivers.browser_pool import get_browser, safe_evaluate
    from config import WEB_DRIVERS
    browser = get_browser()
    page = browser.context.new_page()
    url = WEB_DRIVERS.get("Doubao", {}).get("url") or "https://www.doubao.com/chat/"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(4000)
    print(f"\n=== 豆包 selector 探测: {page.title()} ===")
    groups = [
        ("输入框", _INPUT_SELECTORS),
        ("发送按钮", _SEND_BUTTON_SELECTORS),
        ("新对话", _NEW_CHAT_SELECTORS),
        ("交付卡片", _CARD_SELECTORS),
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
    # 消息状态探针（正文/卡片/生成中）——故事读取的核心，一条命令看清
    state = safe_evaluate(page, _MSG_STATE_JS, list(_CARD_SELECTORS))
    if isinstance(state, dict):
        print(f"  消息: 用户 {state.get('user_count')} 条 / 助手 "
              f"{state.get('answer_count')} 条 / 正文 {len(state.get('text') or '')} 字"
              f" / 交付卡片 {state.get('card')} / 生成中 {state.get('generating')}")
    page.close()
    browser.close()


if __name__ == "__main__":
    _probe()