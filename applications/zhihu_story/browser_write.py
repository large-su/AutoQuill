# ============================================================
# applications/zhihu_story/browser_write.py
# 写操作通道：草稿API读写/写回答按钮/编辑器富文本粘贴与发布确认
# P0 拆分自 browser_adapter.ZhihuBrowser；方法体逐字搬运未改动，
# 行为由 test_browser_adapter 的源码锚点断言守护。
# ============================================================

import json
import logging
import re
import os
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path

from .browser_utils import (
    _NAV_TIMEOUT,
    build_draft_marker,
    clean_story_markdown,
    story_markdown_to_html,
)


class WriteActionsMixin:

    def get_draft_content(self, question_id=None):
        """拉取服务端草稿正文（content 字段在响应顶层）。

        前端「草稿已保存」toast 在程序化上传后可能不出现、导入面板
        ModalLoading 也可能卡住（知乎前端缺陷），服务端草稿是否落盘
        以本 API 为准——发布成功判定都走这里。"""
        qid = question_id or self._extract_question_id()
        if not qid:
            return ""
        return self._safe_evaluate(
            """(qid) => fetch('/api/v4/questions/' + qid + '/draft',
                            {credentials: 'include'})
                .then(r => r.ok ? r.json() : null)
                .then(d => (d && d.content) || '')""", qid) or ""

    def wait_draft_content(self, marker, timeout=30):
        """轮询草稿 API 直到服务端草稿包含 marker 片段（保存确认）。

        marker 由 build_draft_marker 生成（剥空白）。服务端草稿是 HTML
        （段落 \n\n 渲染为 <br><br>），匹配前剥标签+空白，否则跨段
        marker 永远匹配不上。"""
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        while time.time() < deadline:
            html = self.get_draft_content()
            plain = re.sub(r"<[^>]+>", "", html)
            if marker in re.sub(r"\s+", "", plain):
                return True
            # 进度日志：草稿确认最长等 60s，全程无日志会让用户干等
            now = time.time()
            if now - last_log >= 10:
                last_log = now
                log.info("browser_adapter: 等待服务端草稿确认… 已等 %.0fs/%ds"
                         "（草稿 API 轮询）", now - start, timeout)
            self.page.wait_for_timeout(2000)
        return False

    def _find_write_button(self, timeout=12):
        """查找并点击「写回答/编辑回答」按钮（DOM 直点）。带轮询重试：
        长耗时阶段（如生成故事）后页面 reload 可能较慢，单次
        evaluate 容易落在未就绪状态。

        ★ 关键：该问题下已有草稿时，知乎显示「编辑回答」而非
        「写回答」——两者都是打开编辑器的入口，必须都接受。"""
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        while time.time() < deadline:
            clicked = self._safe_evaluate("""(texts) => {
              const clean = s => s.replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
              const btn = Array.from(document.querySelectorAll('button'))
                .find(e => texts.includes(clean(e.textContent || '')));
              if (!btn) return false;
              btn.click();
              return true;
            }""", list(self._WRITE_BUTTON_TEXTS))
            if clicked:
                return True
            # 进度日志：生成长耗时后页面可能渲染慢，等待窗口可达 20s
            now = time.time()
            if now - last_log >= 5:
                last_log = now
                log.info("browser_adapter: 定位「写回答/编辑回答」按钮…"
                         " 已等 %.0fs/%ds", now - start, timeout)
            self.page.wait_for_timeout(1000)
        return False

    def _dump_page_state(self, tag):
        """失败诊断：把当前页面状态写进日志（URL/标题/按钮/正文开头）。

        发布偶发「找不到写回答按钮」——原因可能是 SPA 漂移、会话弹窗
        或风控空壳页。没有现场信息只能盲猜，dump 让下一次失败可诊断。"""
        try:
            state = self._safe_evaluate(
                """() => {
                  const clean = s => (s||'').replace(
                    /[\\u200b-\\u200d\\ufeff]/g,'').trim();
                  return {
                    url: location.href,
                    title: (document.title || '').slice(0, 80),
                    buttons: Array.from(document.querySelectorAll('button'))
                      .map(e => clean(e.textContent)).filter(Boolean).slice(0, 15),
                    bodyHead: (document.body ? document.body.innerText : '')
                      .replace(/\\n+/g, ' | ').slice(0, 160)
                  };
                }""")
            log.warning("browser_adapter: 页面状态[%s] url=%s title=%s",
                        tag, state.get("url"), state.get("title"))
            log.warning("browser_adapter: 页面状态[%s] buttons=%s",
                        tag, state.get("buttons"))
            log.warning("browser_adapter: 页面状态[%s] body=%s",
                        tag, state.get("bodyHead"))
        except Exception as e:
            log.warning("browser_adapter: 页面状态 dump 失败[%s]: %s", tag, e)

    # ---------------- 发布草稿（自动化 M2；2026-09-19 真机探针确认的 DOM） ----------------
    # 真实链路：草稿箱列表（DOM 顺序 = 「编辑于」倒序，**最旧的在最后一张**）
    #   → 打开该草稿的编辑页（/question/<qid>#write，编辑器内已带草稿正文）
    #   → 点编辑器主按钮「发布回答」（Button--primary Button--blue）
    #   → 若弹出发布设置/确认弹窗，点其中的「发布/确认发布/确定」
    #   → 校验：URL 变成 /question/<qid>/answer/<aid>，或服务端草稿 API 已无内容
    # 探针结论（tools/archive/probes/probe_draft_publish.py）：列表卡片 12 张、
    # 编辑页 has_editor=True、按钮文案「发布回答」、旁边有「发布设置」。
    _DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

    _DRAFT_CARDS_JS = r"""() => Array.from(
        document.querySelectorAll('.CreationManage-CreationCard')).map((c, i) => {
      const a = c.querySelector('a[href*="/question/"][href*="#write"]');
      const t = c.querySelector('.CreationCardTitle-wrapper');
      const m = a ? a.href.match(/question\/(\d+)/) : null;
      return {index: i, qid: m ? m[1] : '', href: a ? a.href : '',
              title: t ? (t.innerText || '').trim() : ''};
    })"""

    # click=False 用于演练：只确认按钮在，不点（发布不可逆，演练绝不点）
    _PUBLISH_BTN_JS = """(click) => {
      const clean = s => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').trim();
      const hit = Array.from(document.querySelectorAll('button'))
          .find(b => clean(b.innerText) === '发布回答' && b.offsetParent !== null);
      if (!hit) return false;
      if (click) hit.click();
      return true;
    }"""

    _PUBLISH_CONFIRM_JS = r"""() => {
      const clean = s => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '')
          .replace(/\s+/g, '').trim();
      const modals = Array.from(document.querySelectorAll(
          '[class*=Modal],[role=dialog]')).filter(m => m.offsetParent !== null);
      for (const m of modals) {
        const hit = Array.from(m.querySelectorAll('button')).find(b =>
            /^(确认发布|确定发布|发布|确认|确定)$/.test(clean(b.innerText))
            && b.offsetParent !== null);
        if (hit) { hit.click(); return clean(hit.innerText); }
      }
      return '';
    }"""

    def list_draft_cards(self):
        """打开草稿箱并返回卡片列表（DOM 顺序 = 「编辑于」倒序，最后一张最旧）。"""
        self.page.goto(self._DRAFT_URL, wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT * 1000)
        time.sleep(5)
        for _ in range(6):          # 滚动加载（列表分页/懒加载）
            self._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.2)
        return self._safe_evaluate(self._DRAFT_CARDS_JS) or []

    def publish_draft(self, qid="", verify_timeout=90, progress=None, dry_run=False):
        """发布草稿箱里的一篇草稿（qid 为空 = 发布**最旧**的一篇）。

        返回 {"ok", "qid", "title", "url", "detail"}；草稿箱为空时
        额外带 "reason": "empty"，便于调用方与真正的发布失败区分。

        dry_run=True 只做演练：定位草稿 → 打开编辑页 → 确认「发布回答」按钮存在，
        **不点击**，返回 reason="dry_run"（供首次验证链路，不会真的公开）。

        ★ 不可逆（草稿变公开回答）：调用方必须先取得用户授权（自动化模块只在
          计划里声明的数量/顺序下调用，且失败不盲目重试）。
        """
        def _say(text):
            """进度回调只传文本：调度器/界面各自决定怎么呈现（本层不碰 UI 结构）。"""
            log.info("browser_adapter: %s", text)
            if progress:
                try:
                    progress(text)
                except Exception:      # noqa: BLE001
                    pass

        _say("打开草稿箱…")
        cards = self.list_draft_cards()
        _say("草稿箱可见 %d 篇草稿" % len(cards))
        target = None
        if qid:
            target = next((c for c in cards if str(c.get("qid")) == str(qid)), None)
        elif cards:
            target = cards[-1]        # 倒序列表的最后一张 = 最旧
        if not target:
            # reason=empty：调度器据此记「跳过」而不是「失败」——
            # 草稿箱空是正常状态（今天还没写、或已发完），不该触发熔断。
            return {"ok": False, "reason": "empty", "qid": "", "title": "",
                    "url": "",
                    "detail": "草稿箱里没有可发布的草稿" + ("（找不到 qid=%s）" % qid if qid else "")}
        _say("准备发布最旧的一篇：《%s》" % (target.get("title") or target.get("qid")))
        self.page.goto(target["href"], wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT * 1000)
        time.sleep(6)
        # 等编辑器就绪（草稿正文可能还在异步填充）
        for _ in range(10):
            ready = self._safe_evaluate(
                "() => !!document.querySelector('.public-DraftEditor-content')")
            if ready:
                break
            time.sleep(1.5)
        if not self._safe_evaluate(self._PUBLISH_BTN_JS, not dry_run):
            detail = "编辑器里没找到「发布回答」按钮（可能草稿还在加载/页面改版）"
            if dry_run:
                return {"ok": False, "reason": "dry_run", "rehearsed": False,
                        "qid": target.get("qid", ""),
                        "title": target.get("title", ""),
                        "url": self.page.url or "", "detail": "演练未通过：" + detail}
            return {"ok": False, "qid": target.get("qid", ""),
                    "title": target.get("title", ""), "url": self.page.url or "",
                    "detail": detail}
        if dry_run:
            _say("演练通过：已定位《%s》与「发布回答」按钮（未点击）"
                 % (target.get("title") or target.get("qid")))
            return {"ok": False, "reason": "dry_run", "rehearsed": True,
                    "qid": target.get("qid", ""),
                    "title": target.get("title", ""),
                    "url": self.page.url or "",
                    "detail": "演练通过：草稿箱 %d 篇，将发最旧的一篇《%s》，未点击发布"
                              % (len(cards), target.get("title") or target.get("qid"))}
        _say("已点「发布回答」，等待确认…")
        time.sleep(2)
        confirm = self._safe_evaluate(self._PUBLISH_CONFIRM_JS) or ""
        if confirm:
            _say("已确认发布弹窗（%s）" % confirm)
        deadline = time.time() + verify_timeout
        while time.time() < deadline:
            url = self.page.url or ""
            m = re.search(r"/answer/(\d+)", url)
            if m:
                _say("发布成功：%s" % url)
                return {"ok": True, "qid": target.get("qid", ""),
                        "title": target.get("title", ""), "url": url,
                        "detail": "页面已跳到回答页"}
            if not self.get_draft_content():
                _say("发布成功（服务端草稿已清空）")
                return {"ok": True, "qid": target.get("qid", ""),
                        "title": target.get("title", ""), "url": url,
                        "detail": "服务端草稿已清空（已发布）"}
            time.sleep(2)
        return {"ok": False, "qid": target.get("qid", ""),
                "title": target.get("title", ""), "url": self.page.url or "",
                "detail": "已点发布，但 %ds 内未确认到结果（请人工核对）" % verify_timeout}

    def publish_story(self, story, question_url=None, max_wait=60):
        """发布（编辑器写回答通道）：打开编辑器 → 清空旧草稿 → 富文本粘贴。

        写入通道：md → HTML 转换 + 剪贴板富文本 + 真实 Ctrl+V。知乎
        编辑器是 Draft.js，粘贴富文本时按块解析，`<b>`/`<p>` 能真实
        落盘（实测确认）；fill 纯文本会把 `## **1**` 符号原样写进草稿。

        成功判定：轮询服务端草稿 API（前端保存提示 toast 在程序化
        写入后可能不出现，以服务端草稿内容为准——可验证）。

        ★ 不采用「导入文档 → 文件上传」路径：上传 API 全 200 但服务端
        草稿不更新（知乎程序化导入落盘不可靠，仅空草稿时偶发成功），
        且导入同样不转换 md 符号。

        返回 True 表示服务端草稿已确认包含故事全文，False 表示超时。
        """
        if not self._find_write_button(timeout=20):
            # 生成长耗时后重新导航，页面可能渲染慢/空壳：
            # 先 dump 现场再兜底。★ reload 只重载「当前 URL」——若
            # SPA 已漂移到别处等于重载错误页面；有目标 URL 时优先
            # goto 强制回到问题页，仍失败才报错
            self._dump_page_state("button-not-found")
            log.warning("browser_adapter: 首次未定位「写回答」按钮，"
                        "重新导航重试")
            if question_url:
                self.page.goto(question_url, wait_until="domcontentloaded",
                               timeout=_NAV_TIMEOUT)
            else:
                self.page.reload(wait_until="domcontentloaded",
                                 timeout=_NAV_TIMEOUT)
            self.page.wait_for_timeout(2000)
            if not self._find_write_button(timeout=15):
                self._dump_page_state("button-not-found-retry")
                raise RuntimeError(
                    "未定位「写回答」按钮（页面可能已发布过回答，"
                    "或无写回答入口）")
        try:
            self.page.wait_for_selector(
                '[contenteditable="true"], .AnswerForm-editor', timeout=10000)
        except Exception:
            raise RuntimeError("编辑器未出现")

        # 清空旧草稿：编辑器打开时自动加载已有草稿，先全选删除，
        # 避免新故事与旧内容拼接
        self._safe_evaluate("() => { document.execCommand('selectAll'); }")
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(400)

        editor = self.page.locator(
            '.AnswerForm-editor [contenteditable="true"], '
            '[contenteditable="true"]').first
        plain = clean_story_markdown(story)
        self._paste_rich(editor, story_markdown_to_html(story), plain)

        marker = build_draft_marker(plain or "")
        if not marker:
            raise RuntimeError("故事内容为空，拒绝发布")
        return self.wait_draft_content(marker, timeout=max_wait)

    def _paste_rich(self, editor, html, plain):
        """剪贴板富文本 + 真实 Ctrl+V 写入编辑器。

        Draft.js 编辑器只把「粘贴事件」当富文本处理（fill 纯文本写入
        不会解析格式）。先经 navigator.clipboard 写入 text/html +
        text/plain，再派发真实粘贴键事件；权限按当前站点授予。"""
        origin = re.match(r"^(https?://[^/]+)", self.page.url)
        try:
            if origin:
                self.page.context.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=origin.group(1))
            self._safe_evaluate(
                """([h, p]) => navigator.clipboard.write([
                    new ClipboardItem({
                      'text/html': new Blob([h], {type: 'text/html'}),
                      'text/plain': new Blob([p], {type: 'text/plain'})
                    })
                  ]).then(() => true)""", [html, plain])
            self.page.wait_for_timeout(800)
        except Exception:
            # 剪贴板不可用（权限/环境）时降级纯文本，保证流程不断
            log.warning("browser_adapter: 剪贴板富文本写入失败，"
                        "降级纯文本写入")
            editor.fill(plain)
            return
        editor.focus()
        self.page.keyboard.press("Control+V")

    # ----------------------------------------------------------
    # 语义接口：批量采集
    # ----------------------------------------------------------
