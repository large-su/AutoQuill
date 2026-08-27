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
