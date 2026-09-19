# ============================================================
# web_drivers/deepseek.py — DeepSeek 网页版驱动（DOM 语义化）
#
# 重写自 v2.1 的 OCR/坐标实现：现在全部通过 DOM 指令操作
# chat.deepseek.com，与物理鼠标/分辨率/OCR 解绑。
#
# 2026-09 官网改版适配：
#   - 取消「快速/专家/识图」三大模式，只留「深度思考/智能搜索」两个开关
#     → setup() 空操作，一律用账号默认状态（不再点任何模式/开关）
#   - 消息列表改为虚拟列表：每条消息一个 div.ds-message，助手正文在
#     div[class*=ds-assistant-message-main-content]。文档顺序 = 时间顺序，
#     同一时刻可能挂着上一轮回复 → 读取改为「发送前给最后一条消息打锚点，
#     只读锚点之后的新消息」（旧实现取 querySelector 第一个正文容器，
#     多轮会话会把旧回复当成本次结果）
#   - 会话删除：POST /api/v0/chat_session/delete（Bearer = 页面
#     localStorage 的 userToken）；DOM 兜底走侧栏「⋯ → 删除 → 删除该对话」
#
# 流程（基类生命周期固定）：
#   open_session → setup（空操作）→ input（fill 输入框）
#   → send（打锚点 + Enter）→ wait_complete（停止按钮消失 + 新回复稳定）
#   → read_result（锚点之后的最新助手回复全文）
#
# selector 稳定性：所有关键元素走候选列表 _probe_selectors，
# 前端改版时扩展候选即可；全失败走 _dump_page_state 人工介入。
#
# 运行：python -m web_drivers.deepseek --probe 真实浏览器探测 selector
# ============================================================

import json
import logging
import re
import sys
import time

from web_drivers.base import MARKDOWN_REBUILD_JS, WebLLMDriver

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
# 新版 UI（2026-08-15 实测）：发送/停止是同一个圆形 primary DIV，
# 无 aria-label、class 不含 "stop"——生成中移除 ds-button--disabled
# 类（停止态），完成即恢复。「圆形 primary 且非 disabled」= 停止按钮。
# 旧候选（button[aria-label*=停止] 等）保留兼容旧版 UI。
_STOP_SELECTORS = (
    "div[class*='ds-button--circle'][class*='ds-button--primary']"
    ":not([class*='ds-button--disabled'])",
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

# 消息容器 + 回复锚点（2026-09 改版：消息列表是虚拟列表，每条消息
# 一个 div.ds-message；文档顺序 = 时间顺序，上一轮回复可能仍挂在 DOM 里）
_MESSAGE_SELECTOR = "div.ds-message"
_ANCHOR_ATTR = "data-autoquill-anchor"

# 稳定判定后的重读验证窗口（毫秒）：LLM 流式输出可能中途停顿（长 JSON
# 间歇停顿可 >8s），「文本连续 N 轮不变」可能是暂停而非完成——判定前
# 再等 READBACK 毫秒重读一次，内容增长则继续等待（2026-08-15 线上剖析
# 两次失败均走「文本稳定」兜底，读回残缺 JSON 解析失败）
_READBACK_MS = 3000


class DeepSeekDriver(WebLLMDriver):
    """DeepSeek 网页版（chat.deepseek.com）DOM 驱动。"""

    # 「开启新对话」按钮候选（2026-09 改版文案 = 开启新对话，无 aria-label，
    # 文本兜底见 _click_new_chat_button）。仅重新导航会恢复上次会话——
    # 单链路一会话要求首问必须落在真正的新会话上。
    _NEW_CHAT_SELECTORS = (
        "button[aria-label*='新对话']",
        "div[role=button][aria-label*='新对话']",
        "button[class*='new-chat']",
        "div[class*='new-chat']",
        "[title*='新对话']",
        "a[aria-label*='新对话']",
    )

    def new_chat(self):
        """重置为全新对话：重新导航 + 显式点「新对话」+ 等输入框渲染。

        并行调度每派发一个任务前调用（就是这里保证「每个新任务一个新
        会话」，串行链路里只有首问走到这里，之后的提问走 continue_chat）。
        输入框未在 5s 内渲染不 raise——交给 input() 的 _dump_page_state
        带页面状态 loud-fail。
        """
        self._reset_session_state()
        self.open_session()
        if self._click_new_chat_button():
            log.info("web_drivers: 已点击「新对话」，确认落在全新会话")
            self._page_instance().wait_for_timeout(1000)
        for _ in range(10):
            if self._probe_selectors(_INPUT_SELECTORS, attr="tagName")[0]:
                self._session_id = self._detect_session_id()
                return self
            self._page_instance().wait_for_timeout(500)
        log.warning("web_drivers: new_chat 后未等到输入框渲染，交给 input 兜底")
        return self

    def _click_new_chat_button(self):
        """尽力点击「开启新对话」；未命中返回 False（不报错）。

        2026-09 改版把按钮文案改成「开启新对话」（span 文本、无 aria-label），
        所以除候选选择器外还要按文本找叶子节点，再 click 它最近的可点击
        祖先（span 自己不是按钮）。
        """
        js = (
            "async function() {"
            "  const sels = arguments[0];"
            "  for (const s of sels) {"
            "    const el = document.querySelector(s);"
            "    if (el && el.offsetParent !== null) { el.click(); return true; }"
            "  }"
            "  const re = /开启新对话|新对话|新建聊天|New Chat/;"
            "  const all = Array.from(document.querySelectorAll("
            "      'button,div,a,span,[role=button]'));"
            "  const leaf = all.find(el =>"
            "      re.test(el.textContent || '') &&"
            "      !Array.from(el.children).some(c =>"
            "          re.test(c.textContent || '')) &&"
            "      el.offsetParent !== null);"
            "  if (!leaf) return false;"
            "  const clickable = leaf.closest("
            "      'button,[role=button],a,[role=tab]') || leaf;"
            "  clickable.click();"
            "  return true;"
            "}"
        )
        try:
            return bool(self._safe_evaluate(js, list(self._NEW_CHAT_SELECTORS)))
        except Exception:
            return False

    def setup(self):
        """空操作：改版后网页端没有需要预设的模式或开关。

        2026-09 官网取消「快速模式 / 专家模式 / 识图模式」三大模式，只剩
        「深度思考 / 智能搜索」两个开关，默认状态即账号上次的选择。
        用户约定：登录后直接用默认状态，驱动不点任何模式或开关。
        （改版前这里会读 radiogroup + 两个 toggle 再按目标点击；现在
        radiogroup 已不存在，留着只会打出误导性的「未检测到模式 tab」。）
        """
        log.info("web_drivers: 使用网页端默认模式（改版后无模式可切）")
        return self

    def input(self, prompt):
        """向输入框写入 prompt（textarea fill 纯文本，不需要剪贴板）。

        超时放宽到 120s：DeepSeek SPA 对输入做逐行处理，实测换行数
        决定 fill 耗时（2026-08-15 实测：95KB+6316 换行 = 36.8s；
        同文本去掉换行 = 0.28s）。文风剖析类 prompt 含数十篇样本，
        换行必达数千行，默认 30s 超时必失败——放宽后首次 fill 即完成，
        不会留下半写入内容让后续任务重复踩坑。
        """
        sel, _ = self._probe_selectors(_INPUT_SELECTORS, attr="tagName")
        if not sel:
            self._dump_page_state("找不到 DeepSeek 输入框")
        page = self._page_instance()
        try:
            page.locator(sel).fill(prompt, timeout=120000)
        except Exception:
            self._dump_page_state("输入框写入失败")
        log.info("web_drivers: prompt 已写入（%d 字符）", len(prompt))
        return self

    def send(self):
        """发送：先给当前最后一条消息打锚点，再 Enter（兜底点发送按钮）。

        锚点必须在发送前打：改版后消息列表是虚拟列表，发送后 DOM 里会同时
        挂着上一轮回复，读取时靠锚点区分「本次新回复」与「旧回复」。
        """
        self._mark_reply_anchor()
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
        """轮询等待生成完成：停止按钮消失 + 锚点之后的新回复文本稳定。

        与 API 模式观感一致：心跳日志「生成中… 已生成 N 字」由
        webui/log_capture 识别为进度条事件（前端零改动）。
        取消检查点每轮执行——Web 控制台「停止」按钮直接生效。
        ★ 只看锚点之后的新消息（_read_probe）：虚拟列表里上一轮回复仍挂在
        DOM 中，读旧回复会造成「11s 就稳定」的误判（2026-09-09 线上故障：
        生成 prompt 发出 11s 后读回上一步筛选的 191 字）。
        """
        from web_drivers.browser_pool import _check_cancel
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
        last_beat = time.time()  # 兜底心跳：无长度信号超时后仍打日志
        while time.time() < deadline:
            _check_cancel()
            probe = self._read_probe() or {}
            cur_len = len(probe.get("main") or "")
            think_len = int(probe.get("think_len") or 0)
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
                last_beat = time.time()
                if cur_len:
                    body_started = True
                    log.info("故事生成中… 已生成 %d 字", cur_len)
            elif not body_started and think_len != last_think:
                stable = 0
                last_think = think_len
                last_beat = time.time()
                if think_len:
                    log.info("模型思考中… 已思考 %d 字符", think_len)
            else:
                stable += 1
            # 兜底心跳：正文/思考长度选器在特定页面态下可能一直匹配不到，
            # 导致长时间零日志（观感"卡死"）。超时后打进度，不静默。
            if time.time() - last_beat >= 10:
                log.info("模型响应中… 已用时 %.0fs（内容仍在生成/思考）",
                         time.time() - start)
                last_beat = time.time()
            if stable >= stable_count and cur_len:
                # read-back 验证：稳定可能是停顿（LLM 输出间歇），等
                # READBACK 毫秒重读，长度变化则说明仍在生成、继续等待
                page = self._page_instance()
                page.wait_for_timeout(_READBACK_MS)
                re_probe = self._read_probe() or {}
                re_len = len(re_probe.get("main") or "")
                if re_len != cur_len:
                    log.info("web_drivers: 稳定判定后输出仍增长"
                             "（%d→%d），继续等待", cur_len, re_len)
                    stable = 0
                    last_len = re_len
                    continue
                log.info("web_drivers: 文本稳定 %d 轮，判定完成"
                         "（%.1fs，%d 字符）",
                         stable_count, time.time() - start, re_len)
                return True
            self._page_instance().wait_for_timeout(poll_interval * 1000)
        log.warning("web_drivers: 生成超时（%ds）", max_wait)
        return False

    def read_result(self):
        """读取本次生成结果：锚点之后的最新助手回复全文（innerText）。

        改版后不能取「第一个正文容器」——虚拟列表里第一条正文可能是上一轮
        的旧回复。这里只认发送前打的锚点（_mark_reply_anchor）之后的新消息；
        一条都没读到就直接报错（绝不把旧回复当结果返回）。
        """
        probe = self._read_probe()
        if probe is not None:
            text = (probe.get("main") or "").strip()
            if text:
                return text
            self._dump_page_state(
                "锚点之后没有读到回复内容（可能未登录、生成失败或前端改版）")
        # JS 层探测失败（页面结构大改）→ 退回老选择器，让日志带上页面状态
        sel, text = self._probe_selectors(_RESULT_SELECTORS, attr="innerText")
        if not sel or not text:
            self._dump_page_state("找不到回复内容（可能未登录或前端改版）")
        text = (text or "").strip()
        if not text:
            self._dump_page_state("回复内容为空（可能未登录或生成失败）")
        return text

    # ---------------- 内部工具 ----------------

    def _stop_button_present(self):
        """停止按钮当前是否存在（生成中显示，完成即消失）。"""
        return bool(self._probe_selectors(_STOP_SELECTORS, attr="tagName")[0])

    # ---------------- 新回复锚定（虚拟列表适配） ----------------
    # 2026-09 改版后消息列表是虚拟列表：每条消息一个 div.ds-message，
    # 文档顺序 = 时间顺序，同一时刻 DOM 里可能同时挂着上一轮的回复。
    # 因此「读第一条正文容器」会把旧回复当成本次结果（2026-09-09 线上：
    # 生成 prompt 发出 11s 后读回上一步筛选的 191 字，误判故事过短重试）。
    # 对策：发送前给「当前最后一条消息」打锚点，之后只读锚点之后的新消息；
    # 锚点被虚拟列表回收时退化为「只看最后一条消息」（绝不会读到更早的）。

    def _mark_reply_anchor(self):
        """发送前打锚点：给当前最后一条消息加 data 属性。"""
        js = (
            "() => {"
            "  document.querySelectorAll('[%(attr)s]').forEach("
            "      e => e.removeAttribute('%(attr)s'));"
            "  const msgs = Array.from(document.querySelectorAll('%(msg)s'));"
            "  if (msgs.length) msgs[msgs.length - 1]"
            "      .setAttribute('%(attr)s', '1');"
            "  return msgs.length;"
            "}"
        ) % {"attr": _ANCHOR_ATTR, "msg": _MESSAGE_SELECTOR}
        try:
            n = self._safe_evaluate(js)
            log.debug("web_drivers: 回复锚点已设置（当前消息 %s 条）", n)
            return n
        except Exception as exc:
            log.debug("web_drivers: 锚点设置失败（按最后一条消息读取）：%s", exc)
            return None

    def _read_probe(self):
        """读取锚点之后的新回复：正文全文 + 思考长度。

        返回 {"main": 正文, "think_len": 思考字符数, "fresh": 新消息条数,
              "anchored": 锚点是否还在页面上}；页面不可用/JS 失败返回 None。
        main 为空 = 本次回复还没出现（绝不能拿旧回复顶上）。

        ★ 正文用「逐块重建 markdown」而不是直接 innerText（2026-09-19 修）：
        DeepSeek 把 ## **N** 渲染成 h2 元素，innerText 只剩裸章节号，
        validate_story_format 的「章节 0 个」必扣 4 分 → 通道满分只剩
        6/10，字数略欠（<4000）的合规稿直接判废（真实事故：5 轮里 4 篇
        完整稿被丢，其中一篇差 24 字）。与豆包共用 base.MARKDOWN_REBUILD_JS。
        """
        js = (
            "() => {"
            "%(walker)s"
            "  const msgs = Array.from(document.querySelectorAll('%(msg)s'));"
            "  const anchor = document.querySelector('[%(attr)s]');"
            "  let start = 0;"
            "  if (anchor) {"
            "    const i = msgs.indexOf(anchor);"
            "    start = i >= 0 ? i + 1 : msgs.length;"
            "  } else if (msgs.length) {"
            "    start = msgs.length - 1;"
            "  }"
            "  const fresh = msgs.slice(start);"
            "  let main = '';"
            "  let think = 0;"
            "  for (const m of fresh) {"
            "    const c = m.querySelector("
            "        \"div[class*='ds-assistant-message-main-content']\");"
            "    if (c) main = toMarkdown(c) || (c.innerText || '').trim();"
            "    const t = m.querySelector(\"div[class*='ds-think-content']\");"
            "    if (t) think = (t.innerText || '').length;"
            "  }"
            "  return {main: main, think_len: think, fresh: fresh.length,"
            "          anchored: !!anchor};"
            "}"
        ) % {"attr": _ANCHOR_ATTR, "msg": _MESSAGE_SELECTOR,
             "walker": MARKDOWN_REBUILD_JS}
        try:
            return self._safe_evaluate(js)
        except Exception:
            return None

    # ---------------- 会话 ID 探测 & 完成后删除（风控缓解） ----------------
    # 任务完成后把本次网页会话删除，避免大量聊天记录堆积触发平台风控
    # 警告/封禁（用户 2026-09 实测：生成多了被 DeepSeek 警告乃至封号）。
    # 只删本驱动自己创建/使用过的会话；接口与 DOM 都是尽力而为，
    # 失败仅记日志，绝不阻断任务。

    # 改版后（2026-09 实测）的删除链路：
    #   接口：POST /api/v0/chat_session/delete
    #         body {"chat_session_ids": ["<uuid>"]}
    #         header Authorization: Bearer <localStorage userToken.value>
    #   DOM ：侧栏 a[href='/a/chat/s/<uuid>'] → 悬停出现「⋯」按钮
    #         → 菜单「删除」→ 弹窗「删除该对话」
    _DELETE_API_PATH = "/api/v0/chat_session/delete"

    def _detect_session_id(self):
        """探测当前会话 ID。

        改版后 URL 形如 https://chat.deepseek.com/a/chat/s/<uuid>
        （首条消息发出后才带上）；旧版 ?id= / 路径段一并保留兜底。
        """
        try:
            page = self._page_instance()
            url = page.url or ""
            m = re.search(r"/a/chat/s/([0-9a-zA-Z_-]{16,})", url)
            if m:
                return m.group(1)
            m = re.search(r"[?&]id=([0-9a-zA-Z_-]{6,})", url)
            if m:
                return m.group(1)
            m = re.search(r"/(?:chat|s)/[^/?#]{0,64}?([0-9a-zA-Z_-]{16,})", url)
            if m:
                return m.group(1)
            stored = self._safe_evaluate(
                "() => {"
                "  try {"
                "    const ks = Object.keys(localStorage);"
                "    const k = ks.find(k => /chat.*(id|session)|session.*id/i.test(k));"
                "    if (!k) return '';"
                "    const v = localStorage.getItem(k) || '';"
                "    const m = v.match(/[0-9a-zA-Z_-]{16,}/);"
                "    return m ? m[0] : (v.slice(0, 64));"
                "  } catch (e) { return ''; }"
                "}"
            )
            if stored and re.search(r"[0-9a-zA-Z_-]{16,}", stored):
                m = re.search(r"([0-9a-zA-Z_-]{16,})", stored)
                return m.group(1)
        except Exception:
            pass
        return None

    def _delete_current_session_impl(self):
        """先走站点接口（快、无 UI 依赖），失败再走侧栏 DOM 兜底。"""
        sid = self._session_id
        if not sid:
            sid = self._detect_session_id()
            self._session_id = sid
        if sid and self._delete_session_via_api(sid):
            return True
        if self._delete_session_via_dom():
            return True
        return False

    def _auth_token(self):
        """页面 localStorage 的 userToken（JSON：{"value": ...}）。

        改版后删除接口要 Bearer 鉴权，只带 cookie 会 401（2026-09-12 实测）。
        """
        try:
            raw = self._safe_evaluate(
                "() => { try { return localStorage.getItem('userToken') || ''; }"
                " catch (e) { return ''; } }")
        except Exception:
            return ""
        if not raw:
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return str(data.get("value") or "")
        except Exception:
            pass
        return str(raw)

    def _site_origin(self):
        """站点 origin（删除接口与页面同源）。"""
        from config import WEB_DRIVERS, WEB_DRIVER_NAME
        from urllib.parse import urlsplit
        try:
            site_url = WEB_DRIVERS[WEB_DRIVER_NAME]["url"]
        except Exception:
            site_url = "https://chat.deepseek.com/"
        sp = urlsplit(site_url)
        return f"{sp.scheme}://{sp.netloc}" if sp.netloc else ""

    def _delete_session_via_api(self, sid):
        """POST /api/v0/chat_session/delete（2026-09 实测端点）。

        改版前枚举的 /api/v0/chat/* 系列候选已随站点改版全部下线（404），
        这里只留实测通过的端点 + 同源登录态 Bearer。
        """
        token = self._auth_token()
        origin = self._site_origin()
        if not token or not origin:
            log.info("web_drivers: 未取到 userToken/origin，删除改走 DOM 兜底")
            return False
        page = self._page_instance()
        try:
            resp = page.request.post(
                origin + self._DELETE_API_PATH,
                data=json.dumps({"chat_session_ids": [sid]},
                                ensure_ascii=False),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"})
        except Exception as exc:
            log.debug("web_drivers: 删除 API 调用失败：%s", exc)
            return False
        if resp.ok:
            # 站点成功返回 {"code":0,"data":{"biz_code":0,...}}；
            # HTTP 200 但业务码非 0 说明没删掉，不能当成功
            try:
                body = resp.json() or {}
            except Exception:
                body = {}
            code = body.get("code")
            biz = (body.get("data") or {}).get("biz_code")
            if code in (None, 0) and biz in (None, 0):
                log.info("web_drivers: 会话删除 API 命中 %s（HTTP %d）",
                         self._DELETE_API_PATH, resp.status)
                return True
            log.info("web_drivers: 会话删除 API 业务失败（code=%s biz=%s）",
                     code, biz)
            return False
        log.info("web_drivers: 会话删除 API 未命中（HTTP %d）", resp.status)
        return False

    def _delete_session_via_dom(self):
        """DOM 兜底：侧栏「⋯ → 删除 → 删除该对话」（2026-09 实测结构）。

        a[href='/a/chat/s/<id>'] 悬停 → 项内 [role=button]（⋯ 更多）
        → .ds-dropdown-menu-option 文本「删除」
        → [role=dialog].ds-modal-content 的「删除该对话」。
        会话 ID 探测不到时退回按首条消息标题文本匹配侧栏项。
        """
        sid = self._session_id
        title = self._session_title
        if not sid and not title:
            return False
        js = (
            "async function() {"
            "  const sid = arguments[0] || '';"
            "  const title = arguments[1] || '';"
            "  const items = Array.from(document.querySelectorAll("
            "      \"a[href*='/a/chat/s/'], a[href*='/chat/s/']\"));"
            "  let item = sid ? items.find(a =>"
            "      (a.getAttribute('href') || '').includes(sid)) : null;"
            "  if (!item && title) item = items.find(a =>"
            "      (a.innerText || '').includes(title));"
            "  if (!item) return 'no-item';"
            "  item.scrollIntoView({block: 'center'});"
            "  item.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));"
            "  await new Promise(r => setTimeout(r, 400));"
            "  const more = item.querySelector('[role=button],button');"
            "  if (!more) return 'no-more-button';"
            "  more.click();"
            "  await new Promise(r => setTimeout(r, 600));"
            "  const opt = Array.from(document.querySelectorAll("
            "      '.ds-dropdown-menu-option,[role=menuitem]')).find("
            "      e => (e.innerText || '').trim() === '删除');"
            "  if (!opt) return 'no-delete-option';"
            "  opt.click();"
            "  await new Promise(r => setTimeout(r, 700));"
            "  const dlg = document.querySelector("
            "      \"[role=dialog].ds-modal-content\")"
            "      || document.querySelector('[role=dialog]');"
            "  if (!dlg) return 'no-dialog';"
            "  const btn = Array.from(dlg.querySelectorAll("
            "      '[role=button],button')).find(b =>"
            "      /删除该对话|^删除$|^确认/.test((b.innerText || '').trim()));"
            "  if (!btn) return 'no-confirm-button';"
            "  btn.click();"
            "  return 'ok';"
            "}"
        )
        try:
            res = self._safe_evaluate(js, sid, title)
        except Exception as exc:
            log.debug("web_drivers: DOM 删除异常：%s", exc)
            return False
        if res != "ok":
            log.info("web_drivers: DOM 删除未完成（%s）", res)
            return False
        # 点完确认后侧栏项应消失，据此判定（1.5s 内）
        page = self._page_instance()
        page.wait_for_timeout(1500)
        if sid:
            try:
                gone = self._safe_evaluate(
                    "(sid) => !Array.from(document.querySelectorAll("
                    "\"a[href*='/a/chat/s/']\")).some(a =>"
                    " (a.getAttribute('href') || '').includes(sid))", sid)
            except Exception:
                gone = None
            if gone is False:
                log.info("web_drivers: DOM 删除已确认，但侧栏项仍在（未删成功）")
                return False
        return True


# ---------------- DeepSeek 登录判定 / 登录引导 ----------------
# 登录态检查与手动登录引导（Web 控制台首启引导 + /api/setup/web-login）。
# 经 pool.create_browser 创建独立实例（工厂由 browser_adapter 注册，
# 浏览器本体仍属知乎域，pool 不依赖 applications）；与共享浏览器共用
# _browser_lock 串行启动——两者都依赖 USER_DATA_DIR 持久化 cookie，
# Chromium 单例锁禁止同目录并发。

_DEEPSEEK_COOKIE_DOMAINS = ("deepseek.com",)


def _has_deepseek_cookies(context):
    try:
        cookies = context.cookies()
    except Exception:
        return False
    return any(
        c.get("domain") and any(
            d in c["domain"] for d in _DEEPSEEK_COOKIE_DOMAINS)
        and c.get("value")
        for c in cookies
    )


def web_llm_logged_in():
    """网页版 LLM（chat.deepseek.com）是否真实可登录。

    判定 = deepseek.com cookie 存在 + 加载 chat.deepseek.com 未停在
    登录页（URL 无 sign_in）。仅查 cookie 会假阳性：过期/无效 cookie
    残留时预检放行，运行才撞登录页（线上：切 Web 成功但运行报
    「找不到 DeepSeek 输入框」，页面停在 chat.deepseek.com/sign_in）。

    用独立无头实例检查，不碰共享浏览器（get_browser）——首启引导轮询
    setup/status 时不会反复弹出可见 Edge，也不影响任务浏览器的无头模式。
    与共享浏览器用同一把 _browser_lock 串行启动：两者都依赖
    USER_DATA_DIR 的持久化 cookie（临时目录读不到登录态），而
    Chromium 单例锁禁止同目录并发——串行化后登录引导不再被此
    检查挤掉（线上：Target page, context or browser has been closed）。
    返回 True/False；浏览器无法启动等异常返回 False（不阻塞引导）。"""
    from web_drivers.browser_pool import _browser_lock, create_browser
    try:
        with _browser_lock:
            with create_browser(headless=True) as browser:
                if not _has_deepseek_cookies(browser.context):
                    return False
                page = browser.context.new_page()
                try:
                    page.goto("https://chat.deepseek.com",
                              wait_until="domcontentloaded", timeout=20000)
                    # 等 SPA 跳转定局：已登录 → URL 稳定即返回（省 1.2s
                    # 固定等待）；未登录 → 一旦跳到 /sign_in 立即判定
                    deadline = time.time() + 1.5
                    prev = page.url
                    if "sign_in" in prev:
                        return False
                    while time.time() < deadline:
                        page.wait_for_timeout(250)
                        cur = page.url
                        if "sign_in" in cur:
                            return False
                        if cur == prev:
                            return True
                        prev = cur
                    return "sign_in" not in page.url
                finally:
                    page.close()
    except Exception:
        return False


def login_deepseek_web_flow(timeout=300):
    """打开可见 Edge 到 chat.deepseek.com，等待用户登录网页版 LLM。

    供首启引导（/api/setup/web-login）使用；登录后 cookie 写入持久化
    profile，web_llm_logged_in() 即可判定。返回 (是否成功, 提示信息)。
    登录完成判定 = cookie 存在 + 页面不在登录页（仅 cookie 会因残留
    假阳性，导致引导秒过但实际未登录）。
    独立可见实例 + 全程持 _browser_lock：
      - 共享浏览器（get_browser）归任务线程创建/使用，登录线程跨线程
        复用会触发 Playwright「cannot switch to a different thread」
        （线上：登录线程退出后再次点击登录即报错）
      - 锁内独占持久化 profile，避免与其他浏览器实例并发互杀"""
    from web_drivers.browser_pool import _browser_lock, create_browser
    try:
        with _browser_lock:
            with create_browser(headless=False) as browser:
                page = browser.context.new_page()
                try:
                    page.goto("https://chat.deepseek.com",
                              wait_until="domcontentloaded",
                              timeout=30000)
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        time.sleep(3)
                        page.wait_for_timeout(500)  # 等 SPA 跳回主页
                        if (_has_deepseek_cookies(browser.context)
                                and "sign_in" not in page.url):
                            return True, "检测到登录成功"
                    return False, f"超时（{timeout // 60} 分钟）未检测到登录"
                finally:
                    page.close()
    except Exception as exc:
        return False, f"登录引导失败：{exc}"


# 统一登录引导入口：web_drivers 分发器按 driver 调用（旧名保留兼容）
def login_web_flow(timeout=300):
    return login_deepseek_web_flow(timeout=timeout)


# ---------------- --probe CLI ----------------
# 真实浏览器探测 chat.deepseek.com 的关键 selector，打印命中结果。
# 用法：python -m web_drivers.deepseek --probe
# （需 Edge 持久化 profile 已登录 DeepSeek，或先在页面手动登录）

def _probe_session():
    """真实浏览器探测当前会话 ID 的来路：URL query / localStorage。

    校准删除会话用：跑一次真实页面，观察 URL 与 localStorage 里哪个
    字段承载会话 ID，据此调整 _detect_session_id（2026-09 骨架版）。
    用法：python -m web_drivers.deepseek --probe-session
    """
    import applications.zhihu_story.browser_adapter  # noqa: F401
    from web_drivers.browser_pool import get_browser
    browser = get_browser()
    page = browser.context.new_page()
    page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded",
              timeout=20000)
    page.wait_for_timeout(3000)
    print("\n=== DeepSeek 会话 ID 探测 ===")
    print("URL:", page.url)
    m = re.search(r"[?&]id=([0-9a-zA-Z_-]{6,})", page.url)
    print("URL query id:", m.group(1) if m else None)
    try:
        keys = page.evaluate(
            "() => Object.keys(localStorage).filter("
            "k => /chat|session|id/i.test(k))")
        print("localStorage 候选 key：", keys)
        for k in (keys or [])[:10]:
            v = page.evaluate(
                "(k) => { const s = localStorage.getItem(k)||'';"
                " const m = s.match(/[0-9a-zA-Z_-]{16,}/);"
                " return m ? m[0] : s.slice(0, 80); }", k)
            print(f"  {k} = {v}")
    except Exception as exc:
        print("localStorage 读取失败：", exc)
    page.close()
    browser.close()


def _probe():
    # 组合根：CLI 工具自己负责组装——导入应用层以注册浏览器工厂
    # （browser_pool 不依赖 applications，工厂由应用层注册）
    import applications.zhihu_story.browser_adapter  # noqa: F401
    from web_drivers.browser_pool import get_browser
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
        ("思考容器", _THINK_SELECTORS),
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
    print("\n  --- 2026-09 版式结构 ---")
    try:
        info = page.evaluate(
            "() => ({"
            "  msg: document.querySelectorAll('div.ds-message').length,"
            "  main: document.querySelectorAll("
            "      \"div[class*='ds-assistant-message-main-content']\").length,"
            "  sessions: document.querySelectorAll("
            "      \"a[href*='/a/chat/s/']\").length,"
            "  newChat: Array.from(document.querySelectorAll('*')).some("
            "      e => e.childElementCount === 0 &&"
            "           /开启新对话|新对话/.test(e.textContent || '')),"
            "  radiogroup: document.querySelectorAll('[role=radiogroup]').length,"
            "  toggles: Array.from(document.querySelectorAll("
            "      '[class*=ds-toggle-button]')).filter("
            "      e => !/__icon/.test(String(e.className))).length,"
            "  moreBtn: document.querySelectorAll("
            "      \"a[href*='/a/chat/s/'] [role=button]\").length"
            "})")
        print("  消息容器 div.ds-message：%s（正文容器 %s）"
              % (info["msg"], info["main"]))
        print("  侧栏会话链接：%s（项内「⋯」按钮 %s）"
              % (info["sessions"], info["moreBtn"]))
        print("  开启新对话：%s | 旧模式 tab radiogroup：%s | 开关：%s"
              % (info["newChat"], info["radiogroup"], info["toggles"]))
    except Exception as exc:
        print("  结构探测失败：", exc)
    print("\n  --- 页面文本片段 ---")
    try:
        body = page.evaluate("() => document.body.innerText.slice(0, 200)")
        print("  " + (body or "")[:200].replace("\n", " | "))
    except Exception:
        pass
    page.close()
    browser.close()


if __name__ == "__main__":
    if "--probe-session" in sys.argv:
        _probe_session()
    else:
        _probe()