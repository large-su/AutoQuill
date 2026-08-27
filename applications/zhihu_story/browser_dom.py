# ============================================================
# applications/zhihu_story/browser_dom.py
# DOM 只读通道：推荐页解析/问题页导航与就绪/首答提取/可答性检测
# P0 拆分自 browser_adapter.ZhihuBrowser；方法体逐字搬运未改动，
# 行为由 test_browser_adapter 的源码锚点断言守护。
# ============================================================

import json
import logging
import os
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path
from config.story import ZHIHU_RECOMMEND_URL

from web_drivers.browser_pool import WorkflowCancelled, _check_cancel  # noqa: F401

from .browser_utils import (
    _NAV_TIMEOUT,
    _RECOMMEND_QUESTIONS_JS,
    _AUTHOR_LINKS_JS,
    _EXPAND_FIRST_COLLAPSED_JS,
    _PRIMARY_ANSWER_JS,
    normalize_question_url,
    normalize_author_url,
    extract_answer_id,
)


class DomReadMixin:

    def open_recommend_page(self, url=None):
        """打开选题候选页：默认创作中心「推荐问题」（原 workflow 入口，
        候选池为「等你来答」的优质问题，对写作选题对口；首页推荐流
        为全品类大杂烩，已弃用为默认）。

        也可传创作中心「邀请回答」页 URL（选题来源 QUESTION_SOURCE
        切换为 invited 时传入），两页同构（.ToolsQuestion 行卡片）。"""
        if url is None:
            from applications.zhihu_story.config import ZHIHU_RECOMMEND_URL
            url = ZHIHU_RECOMMEND_URL
        _check_cancel()
        self.page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        time.sleep(1.5)
        # 瀑布流首屏常在滚动后补充渲染，滚动一次触发
        try:
            self._safe_evaluate("() => window.scrollBy(0, 800)")
            time.sleep(0.8)
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        return self

    def get_recommend_questions(self, max_cards=30):
        """返回推荐页候选：[{title, href, likes, comments}]，href 为纯问题 URL"""
        items = self._safe_evaluate(_RECOMMEND_QUESTIONS_JS) or []
        cleaned = []
        for it in items:
            url = normalize_question_url(it.get("href"))
            if url:
                it["href"] = url
                cleaned.append(it)
        return cleaned[:max_cards]

    # ----------------------------------------------------------
    # 语义接口：问题页提取
    # ----------------------------------------------------------

    def open_question(self, url, force=False):
        """进入问题页；已在同一问题页时默认跳过重载（真幂等）。

        goto 同一 URL 会整页重载（空白加载 + 触发风控的概率），提取
        流程会多次重进同一问题，幂等跳过避免重复导航。force=True
        强制重新导航——发布前页面已闲置数分钟（生成耗时），强制
        一次定位到目标 URL 更可靠，也符合「发布只跳一次」的预期。"""
        target = normalize_question_url(url) or url
        current = normalize_question_url(self.page.url)
        if not force and current and target and current == target:
            try:
                # 同页幂等：滚动触发懒加载等正文容器（不整页重载）。
                # 固定 8s 干等对冷加载必失败（正文不滚动不渲染）
                self._wait_answer_container(timeout=8)
            except WorkflowCancelled:
                raise
            except Exception:
                pass
            return self
        _check_cancel()
        self.page.goto(target, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        try:
            if not self._wait_answer_container(timeout=8):
                log.warning("browser_adapter: 问题页正文容器未在 8s 内出现，继续")
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        time.sleep(0.5)
        return self

    def _wait_answer_container(self, timeout=15):
        """轮询等待首答容器出现；每次轮询前向下滚动触发懒加载。

        页面冷加载/慢网络时容器延迟渲染，单次 8s 等待经常落空，
        导致首答被误判为过短而降级 OCR；且知乎问题页不滚动就不
        渲染首答（实测：刚进入只有骨架，下滑后才加载）。

        循环：检测 → 无则下滑触发渲染 → 等渲染完成（轮询检测，
        最多 ~2s）→ 滑回原位 → 再检测。回位是关键：一直下滑会
        触发无限滚动不断加载更多回答（页面越拖越长、首答 scope
        漂移），回位后只保留首屏已渲染的内容。

        渲染窗口：曾固定下滑后 1s 即回位——知乎懒加载渲染需要
        更久，回位时内容还没渲染出来，检测永远落空、15s 超时。
        现在下滑后轮询等容器出现（快的页面几百 ms 即返回），
        渲染成功再回位（已渲染的 DOM 不因回位消失）。"""
        selector = ("'.QuestionAnswer-content, .AnswerItem, "
                    ".RichContent-inner'")
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        while time.time() < deadline:
            _check_cancel()
            if self._safe_evaluate(
                    f"() => !!document.querySelector({selector})"):
                return True
            # 下滑触发懒加载：分段小步滚动 + 间隔（模拟人手滚轮）。
            # 一次性 scrollBy(0,600) 是瞬间大跳，知乎懒加载有时不
            # 触发（快速滚动被跳过/防抖）；连续小段滚动产生多次
            # scroll 事件，渲染更可靠。6×100px，每段间隔 60ms，
            # 总耗时 ~360ms 的连续下滑过程。
            self._safe_evaluate(
                "async () => {"
                "  const steps = 6, stepPx = 100, delayMs = 60;"
                "  for (let i = 0; i < steps; i++) {"
                "    window.scrollBy(0, stepPx);"
                "    await new Promise(r => setTimeout(r, delayMs));"
                "  }"
                "  return true;"
                "}"
            )
            # 渲染窗口：轮询等容器出现，最多 ~2s（间隔 500ms×4）
            rendered = False
            for _ in range(4):
                _check_cancel()
                if self._safe_evaluate(
                        f"() => !!document.querySelector({selector})"):
                    rendered = True
                    break
                self.page.wait_for_timeout(500)
            # 滑回原位：避免触发无限加载更多回答
            self._safe_evaluate("() => { window.scrollTo(0, 0); return true; }")
            self.page.wait_for_timeout(400)
            if rendered:
                return True
            # 进度日志：此循环可能长达 15s，无日志会让用户误以为卡住
            now = time.time()
            if now - last_log >= 5:
                last_log = now
                log.info("browser_adapter: 等待首答渲染… 已等 %.0fs/%ds"
                         "（下滑触发懒加载）", now - start, timeout)
        return False

    def _answer_text_len(self):
        """首答容器当前正文长度（就绪流程的稳定判据）。"""
        n = self._safe_evaluate(
            "() => { const el = document.querySelector("
            "'.QuestionAnswer-content .RichContent-inner, "
            ".AnswerItem .RichContent-inner, .RichContent-inner');"
            " return el ? el.innerText.length : 0; }")
        return n or 0

    def _settle_answer_page(self, timeout=15):
        """首答就绪流程：展开第一个回答的「阅读全文」→ 等正文稳定。

        首答已由 _wait_answer_container 确认渲染；长回答默认折叠，
        展开后还会渐进加载。循环：只点开第一个回答容器内的展开
        按钮，轮询正文长度连续两轮不变视为就绪。不做任何滚动。
        返回就绪时的正文长度（0 = 始终未见）。"""
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        last_len, stable = 0, 0
        while time.time() < deadline:
            _check_cancel()
            self._safe_evaluate(_EXPAND_FIRST_COLLAPSED_JS)
            self.page.wait_for_timeout(700)
            cur = self._answer_text_len()
            if cur > 0 and cur == last_len:
                stable += 1
                if stable >= 2:
                    return cur
            else:
                stable = 0
            last_len = cur
            # 进度日志：展开后正文渐进加载可能拖满 15s，无日志易误判卡住
            now = time.time()
            if now - last_log >= 5:
                last_log = now
                log.info("browser_adapter: 首答就绪中… 已等 %.0fs/%ds"
                         "（展开阅读全文，正文稳定检测）", now - start, timeout)
        return last_len

    def get_primary_answer(self, url=None, min_length=100, retries=2):
        """提取问题页首答。返回 {title, answer, footer}；不合格返回 None。

        容器缺失或首答过短时重试（含页面 reload 兜底）：首屏可能
        渲染失败或加载慢，单次判定会把 DOM 主通道误判为不可用而
        降级 OCR。"""
        if url:
            self.open_question(url)
        for attempt in range(retries + 1):
            if self._wait_answer_container(timeout=15):
                # 就绪流程：展开首答阅读全文 → 正文稳定后再提取
                self._settle_answer_page(timeout=15)
                data = self._safe_evaluate(_PRIMARY_ANSWER_JS) or {}
                answer = (data.get("answer") or "").strip()
                if len(answer) >= min_length:
                    return {
                        "title": (data.get("title") or "").strip(),
                        "answer": answer,
                        "footer": data.get("footer") or {},
                    }
                log.warning("browser_adapter: 首答过短（%d 字），重试",
                            len(answer))
            if attempt < retries:
                self.page.reload(wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                self.page.wait_for_timeout(1500)
        log.warning("browser_adapter: 重试 %d 次后仍无合格首答，放弃",
                    retries)
        return None

    # ----------------------------------------------------------
    # 语义接口：作者页采集
    # ----------------------------------------------------------

    def get_author_answer_links(self, author_page_url):
        """作者主页 → 全部答案链接：[{title, href, likes, comments}]

        列表轮询等待：渲染慢时（无头模式/慢网络）一次固定等待常
        读空——V4.2.2 用户反馈无头采集 0 篇（「答案列表未出现」×5
        后列表读尽，切前台同作者立即可采）。轮询直到链接非空。"""
        author_page_url = normalize_author_url(author_page_url)
        self.page.goto(author_page_url, wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            _check_cancel()
            links = self._safe_evaluate(_AUTHOR_LINKS_JS) or []
            if links:
                return links
            time.sleep(1)
        log.warning("browser_adapter: 作者页答案列表 20s 内未出现，继续")
        return []

    def get_author_answer(self, answer_url, author, min_length=100):
        """打开该作者某篇答案的独立回答页，提取回答全文。

        链接形如 /question/{qid}/answer/{aid}；独立回答页 /answer/{aid}
        只渲染该作者的回答——不存在问题页「排名第一」问题，正文也
        立即在 DOM（不触发问题页懒加载）。无法识别 aid 时退回原链接。
        返回 {title, answer, footer}；不合格返回 None。"""
        aid = extract_answer_id(answer_url)
        target = f"https://www.zhihu.com/answer/{aid}" if aid else answer_url
        _check_cancel()
        self.page.goto(target, wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT)
        try:
            if not self._wait_answer_container(timeout=15):
                log.warning("browser_adapter: 回答页正文容器未出现，继续")
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        self._settle_answer_page(timeout=15)
        data = self._safe_evaluate(_PRIMARY_ANSWER_JS) or {}
        answer = (data.get("answer") or "").strip()
        if len(answer) < min_length:
            log.warning("browser_adapter: 答案过短（%d 字），跳过", len(answer))
            return None
        return {
            "title": (data.get("title") or "").strip(),
            "answer": answer,
            "footer": data.get("footer") or {},
        }

    # ----------------------------------------------------------
    # 语义接口：可回答性检测（替代 OCR 查「撤销删除」）
    # ----------------------------------------------------------

    def check_answerable(self):
        """DOM 检测当前问题是否可回答（替代 OCR 找「撤销删除」）。

        硬信号1：页面出现「撤销删除」→ 曾删过回答，绝不能回答。
        硬信号2：页面出现「查看我的回答」→ 本账号已发布过回答，
                无写回答入口，不能重复发布。
        软信号：「写回答」按钮存在 → 可回答。
        两者都无 → 默认可回答（与旧 OCR 语义一致，宁采后弃不前置挡）。
        返回 (can_answer, reason)。
        """
        has_undo = self._safe_evaluate(
            "() => document.body && document.body.innerText.includes('撤销删除')")
        if has_undo:
            return False, "检测到「撤销删除」——此问题下曾删除过回答，跳过"
        has_answered = self._safe_evaluate(
            "() => document.body && document.body.innerText.includes('查看我的回答')")
        if has_answered:
            return False, "检测到「查看我的回答」——此问题下已发布过回答，跳过"
        # ★ 关键修复：已有「编辑回答」（而非「写回答」）说明本账号已答过此题。
        #   继续当新题会导致"反复回答同一问题"死循环，必须跳过。
        has_edit = self._button_with_text("编辑回答")
        if has_edit:
            return False, "检测到「编辑回答」——此问题下已答过回答，不能重复发布，跳过"
        has_write = self._safe_evaluate("""(texts) =>
          Array.from(document.querySelectorAll('button'))
            .some(e => texts.includes(e.textContent
                        .replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim()))""",
            list(self._WRITE_BUTTON_TEXTS))
        if has_write:
            return True, "检测到可写入口（写回答/编辑回答），可回答"
        return True, "未检测到禁止信号，默认可回答"
